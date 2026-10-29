"""API key authentication and the tenant and user rules for every caller."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.routing import Mount

from executor_service.container import ApplicationContainer
from executor_service.infrastructure.db.base import Base
from executor_service.interfaces.http.app import create_app
from executor_service.settings import AuthClient, Settings
from tests.test_rest_execution_api import _submit_payload

AGENT_KEY = "agent-key"
BATCH_KEY = "batch-key"
OPS_KEY = "ops-key"
PLAIN_KEY = "plain-key"


def _sha256(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


CLIENTS = (
    AuthClient(
        name="agent",
        key_sha256=(_sha256(AGENT_KEY),),
        tenant="agent-tenant",
        relay=True,
        allowed_tenants=("agent-tenant", "other-tenant"),
    ),
    AuthClient(
        name="batch",
        key_sha256=(_sha256(BATCH_KEY),),
        relay=True,
        allowed_tenants=("agent-tenant",),
    ),
    AuthClient(name="ops", key_sha256=(_sha256(OPS_KEY),), admin=True),
    AuthClient(
        name="plain", key_sha256=(_sha256(PLAIN_KEY),), tenant="plain-tenant"
    ),
)


@asynccontextmanager
async def _app(
    tmp_path: Path, **overrides: Any
) -> AsyncIterator[tuple[httpx.AsyncClient, ApplicationContainer]]:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6399/15",
        runtime_enabled=False,
        shared_storage_root=tmp_path,
        **overrides,
    )
    container = ApplicationContainer(settings)
    async with container.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    transport = httpx.ASGITransport(app=create_app(container))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client, container
    finally:
        await container.redis.aclose()
        await container.engine.dispose()


@pytest.fixture
async def api_key_app(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, ApplicationContainer]]:
    async with _app(
        tmp_path, auth_mode="api_key", auth_clients=CLIENTS
    ) as app:
        yield app


def _headers(key: str, user: str | None = "rest-user", **extra: str) -> dict:
    headers = {"X-Api-Key": key, **extra}
    if user is not None:
        headers["X-User-Id"] = user
    return headers


async def _submit(
    client: httpx.AsyncClient, headers: dict[str, str], key: str
) -> httpx.Response:
    return await client.post(
        "/api/v1/executions", json=_submit_payload(key=key), headers=headers
    )


async def test_missing_or_unknown_key_is_unauthenticated(api_key_app) -> None:
    client, _ = api_key_app
    for headers in ({}, {"X-Api-Key": "wrong"}):
        response = await client.get("/api/v1/executions", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    response = await client.post("/mcp", json={}, headers={})
    assert response.status_code == 401


async def test_probes_need_no_key(api_key_app) -> None:
    client, _ = api_key_app
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/workerz")).status_code == 200


async def test_runtime_and_maintenance_routes_require_admin(
    api_key_app,
) -> None:
    client, _ = api_key_app
    for path in (
        "/api/v1/runtime-targets",
        "/api/v1/runtime-pools",
        "/api/v1/maintenance/runs",
    ):
        denied = await client.get(path, headers=_headers(AGENT_KEY))
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "FORBIDDEN"
    allowed = await client.get(
        "/api/v1/runtime-targets", headers=_headers(OPS_KEY, None)
    )
    assert allowed.status_code == 200


async def test_tenant_comes_from_key_or_allowed_override(api_key_app) -> None:
    client, _ = api_key_app
    default = await _submit(client, _headers(AGENT_KEY), "tenant-default")
    assert default.status_code == 202
    detail = await client.get(
        default.headers["location"], headers=_headers(AGENT_KEY)
    )
    assert detail.json()["tenant_id"] == "agent-tenant"

    other = _headers(AGENT_KEY, **{"X-Tenant-Id": "other-tenant"})
    overridden = await _submit(client, other, "tenant-override")
    detail = await client.get(overridden.headers["location"], headers=other)
    assert detail.json()["tenant_id"] == "other-tenant"

    for key, tenant in (
        (AGENT_KEY, "unlisted-tenant"),
        (PLAIN_KEY, "agent-tenant"),
    ):
        response = await _submit(
            client, _headers(key, **{"X-Tenant-Id": tenant}), "forbidden"
        )
        assert response.status_code == 403

    # A relay key without a default tenant must name one.
    no_tenant = await _submit(client, _headers(BATCH_KEY), "no-tenant")
    assert no_tenant.status_code == 400
    assert no_tenant.json()["error"]["code"] == "INVALID_IDENTITY"


async def test_executions_are_scoped_to_tenant_and_user(api_key_app) -> None:
    client, _ = api_key_app
    owner = _headers(AGENT_KEY, "rest-user")
    submitted = await _submit(client, owner, "scoped")
    location = submitted.headers["location"]

    assert (await client.get(location, headers=owner)).status_code == 200
    other_user = await client.get(location, headers=_headers(AGENT_KEY, "b"))
    assert other_user.status_code == 404
    other_tenant = await client.get(
        location,
        headers=_headers(
            AGENT_KEY, "rest-user", **{"X-Tenant-Id": "other-tenant"}
        ),
    )
    assert other_tenant.status_code == 404
    # The same tenant through another key is still the same tenant.
    relayed = _headers(
        BATCH_KEY, "rest-user", **{"X-Tenant-Id": "agent-tenant"}
    )
    assert (await client.get(location, headers=relayed)).status_code == 200

    # A system read without a user sees the whole tenant.
    system = _headers(AGENT_KEY, None)
    assert (await client.get(location, headers=system)).status_code == 200
    listed = await client.get("/api/v1/executions", headers=system)
    assert len(listed.json()["items"]) == 1
    hidden = await client.get(
        "/api/v1/executions", headers=_headers(AGENT_KEY, "b")
    )
    assert hidden.json()["items"] == []

    cancel = await client.post(
        f"{location}/cancel",
        json={
            "idempotency_key": "cancel-other",
            "actor": {"type": "USER", "id": "b"},
        },
        headers=_headers(AGENT_KEY, "b"),
    )
    assert cancel.status_code == 404


async def test_writes_require_a_user(api_key_app) -> None:
    client, _ = api_key_app
    submitted = await _submit(client, _headers(AGENT_KEY), "owned")
    location = submitted.headers["location"]

    response = await _submit(client, _headers(AGENT_KEY, None), "no-user")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_IDENTITY"
    cancel = await client.post(
        f"{location}/cancel",
        json={
            "idempotency_key": "cancel-system",
            "actor": {"type": "USER", "id": "system"},
        },
        headers=_headers(AGENT_KEY, None),
    )
    assert cancel.status_code == 400


async def test_user_id_is_lowercased_and_validated(api_key_app) -> None:
    client, _ = api_key_app
    submitted = await _submit(
        client, _headers(AGENT_KEY, "Rest-User"), "lower"
    )
    detail = await client.get(
        submitted.headers["location"], headers=_headers(AGENT_KEY, "rest-user")
    )
    assert detail.json()["context"]["user_id"] == "rest-user"

    for user in ("-bad", "a/b", "a" * 65):
        response = await client.get(
            "/api/v1/executions", headers=_headers(AGENT_KEY, user)
        )
        assert response.status_code == 400


async def test_body_identity_fields_are_rejected(api_key_app) -> None:
    client, _ = api_key_app
    for field, value in (
        ("context", {"user_id": "user-a"}),
        ("tenant_id", "agent-tenant"),
    ):
        response = await client.post(
            "/api/v1/executions",
            json={**_submit_payload(key=f"body-{field}"), field: value},
            headers=_headers(AGENT_KEY),
        )
        assert response.status_code == 422


async def test_mcp_tools_use_the_request_caller(tmp_path: Path) -> None:
    async with _app(
        tmp_path,
        auth_mode="api_key",
        auth_clients=CLIENTS,
        mcp_allowed_hosts=("testserver",),
    ) as (_, container):
        app = create_app(container)
        [mount] = [route for route in app.routes if isinstance(route, Mount)]
        # The mount's lifespan starts the MCP session manager.
        mcp_app = cast(Starlette, mount.app)
        async with mcp_app.router.lifespan_context(mcp_app):

            def mcp(headers: dict[str, str]) -> Client:
                http = httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    headers=headers,
                )
                return Client(
                    streamable_http_client(
                        "http://testserver/mcp", http_client=http
                    )
                )

            async with mcp(_headers(AGENT_KEY, "rest-user")) as owner:
                submitted = await owner.call_tool(
                    "execution_submit",
                    {"request": _submit_payload(key="mcp-http")},
                )
                assert not submitted.is_error, submitted
                execution_id = submitted.structured_content["execution_id"]
                listed = await owner.call_tool("execution_list", {})
                assert len(listed.structured_content["items"]) == 1
                denied = await owner.call_tool(
                    "runtime_target_list", {"limit": 10}
                )
                assert denied.is_error

            async with mcp(_headers(AGENT_KEY, "other-user")) as other:
                hidden = await other.call_tool(
                    "execution_get", {"execution_id": execution_id}
                )
                assert hidden.is_error
                assert "EXECUTION_NOT_FOUND" in str(hidden.content)


async def test_mcp_is_not_mounted_when_disabled(tmp_path: Path) -> None:
    async with _app(tmp_path, mcp_enabled=False) as (client, _):
        response = await client.post(
            "/mcp", json={}, headers={"X-User-Id": "user-a"}
        )
    assert response.status_code == 404


async def test_header_mode_trusts_headers_with_a_dev_default(
    tmp_path: Path,
) -> None:
    async with _app(tmp_path) as (client, _):
        user = {"X-User-Id": "rest-user"}
        submitted = await _submit(client, user, "header-mode")
        detail = await client.get(submitted.headers["location"], headers=user)
        assert detail.json()["tenant_id"] == "dev"
        assert (await client.get("/api/v1/runtime-targets")).status_code == 200


@pytest.mark.parametrize("app_env", ["stg", "prd"])
def test_header_mode_is_refused_outside_local_and_dev(
    monkeypatch: pytest.MonkeyPatch, app_env: str
) -> None:
    monkeypatch.setenv("APP_ENV", app_env)
    with pytest.raises(ValidationError, match="AUTH_MODE=header"):
        Settings(
            _env_file=None,
            auth_mode="header",
            runtime_credential_key="byMtLPvlNfS4KWGj7k0Rzt53Gl9NABlCqfbn8dSF1Ts=",
        )


def test_relay_only_settings_and_unique_keys() -> None:
    with pytest.raises(ValidationError, match="relay"):
        AuthClient(
            name="x",
            key_sha256=(_sha256("x"),),
            allowed_tenants=("agent-tenant",),
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            auth_mode="api_key",
            auth_clients=(CLIENTS[0], CLIENTS[0]),
        )


async def test_openapi_documents_the_identity_headers(api_key_app) -> None:
    client, _ = api_key_app
    schema = (await client.get("/openapi.json")).json()
    assert schema["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Api-Key",
        "description": (
            "Caller API key. Required when AUTH_MODE=api_key (stg, prd)."
        ),
    }
    submit = schema["paths"]["/api/v1/executions"]["post"]
    headers = {
        parameter["name"]
        for parameter in submit["parameters"]
        if parameter["in"] == "header"
    }
    assert headers == {
        "X-Tenant-Id",
        "X-User-Id",
        "X-Project-Id",
        "X-Session-Id",
    }
    assert submit["security"] == [{"APIKeyHeader": []}]
    targets = schema["paths"]["/api/v1/runtime-targets"]["get"]
    assert targets["security"] == [{"APIKeyHeader": []}]
