"""Caller authentication and the identity each request acts for.

One ASGI middleware authenticates every HTTP request, REST and MCP alike, so
a new router or mount cannot skip it. Handlers read the resolved Caller and
apply the per-Execution rule through ``authorize_execution``.
"""

import hashlib
import hmac
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from executor_service.application.execution_queries import (
    ExecutionQueryService,
)
from executor_service.domain.errors import (
    DomainError,
    ExecutionNotFoundError,
    ForbiddenError,
    InvalidIdentityError,
    UnauthenticatedError,
)
from executor_service.domain.tenants import (
    RESERVED_TENANT_IDS,
    is_valid_tenant_id,
)
from executor_service.settings import AuthClient, Settings

# Kubelet probes call these without a key.
PUBLIC_PATHS = frozenset({"/healthz", "/readyz", "/workerz"})
# Tenant-independent operations reserved for admin keys.
ADMIN_PATH_PREFIXES = (
    "/api/v1/runtime-pools",
    "/api/v1/runtime-targets",
    "/api/v1/maintenance",
)
HEADER_MODE_TENANT = "dev"
USER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# Project and session IDs become Runtime workspace path segments.
SCOPE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
READ_METHODS = frozenset({"GET", "HEAD"})

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Caller:
    client: str | None
    admin: bool
    tenant_id: str | None
    user_id: str | None
    project_id: str | None
    session_id: str | None


_caller: ContextVar[Caller] = ContextVar("executor_caller")


def current_caller() -> Caller:
    return _caller.get()


@contextmanager
def acting_as(caller: Caller) -> Iterator[None]:
    token = _caller.set(caller)
    try:
        yield
    finally:
        _caller.reset(token)


def require_admin(caller: Caller) -> None:
    if not caller.admin:
        raise ForbiddenError("This operation requires an admin key.")


def require_tenant(caller: Caller) -> str:
    if caller.tenant_id is None:
        raise InvalidIdentityError("This key has no tenant; send X-Tenant-Id.")
    return caller.tenant_id


def require_user(caller: Caller) -> str:
    if caller.user_id is None:
        raise InvalidIdentityError("X-User-Id is required.")
    return caller.user_id


async def authorize_execution(
    queries: ExecutionQueryService,
    caller: Caller,
    execution_id: UUID,
    *,
    write: bool,
) -> None:
    """Hide an Execution outside the caller's tenant or, with a user, outside that user."""

    tenant_id = require_tenant(caller)
    user_id = require_user(caller) if write else caller.user_id
    execution = await queries.execution(execution_id)
    if execution.tenant_id != tenant_id or (
        user_id is not None and execution.user_id != user_id
    ):
        raise ExecutionNotFoundError(
            f"Execution {execution_id} was not found."
        )


class CallerAuthenticationMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self._app = app
        self._header_mode = settings.auth_mode == "header"
        self._clients = settings.auth_clients

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self._app(scope, receive, send)
            return
        try:
            caller = self._caller(Headers(scope=scope))
            if scope["path"].startswith(ADMIN_PATH_PREFIXES):
                require_admin(caller)
        except DomainError as exc:
            response = JSONResponse(
                status_code=exc.http_status,
                content={"error": {"code": exc.code, "message": str(exc)}},
            )
            await response(scope, receive, send)
            return
        if scope["method"] not in READ_METHODS:
            logger.info(
                "Caller write request",
                extra={
                    "client": caller.client,
                    "tenant_id": caller.tenant_id,
                    "user_id": caller.user_id,
                },
            )
        with acting_as(caller):
            await self._app(scope, receive, send)

    def _caller(self, headers: Headers) -> Caller:
        key = headers.get("X-Api-Key")
        client = None if key is None else self._client(key)
        if client is None and (key is not None or not self._header_mode):
            raise UnauthenticatedError("A valid X-Api-Key is required.")
        requested_tenant = headers.get("X-Tenant-Id")
        if requested_tenant is not None and (
            not is_valid_tenant_id(requested_tenant)
            or requested_tenant in RESERVED_TENANT_IDS
        ):
            raise InvalidIdentityError("X-Tenant-Id is not a valid tenant.")
        if client is None:
            # Header mode trusts the identity headers as sent.
            tenant_id = requested_tenant or HEADER_MODE_TENANT
        elif requested_tenant is None:
            tenant_id = client.tenant
        elif client.relay and requested_tenant in client.allowed_tenants:
            tenant_id = requested_tenant
        else:
            raise ForbiddenError("This key may not act for that X-Tenant-Id.")
        user_id = headers.get("X-User-Id")
        if user_id is not None:
            user_id = user_id.lower()
            if not USER_ID.fullmatch(user_id):
                raise InvalidIdentityError(
                    f"X-User-Id must match {USER_ID.pattern}."
                )
        project_id = _scope_id(headers, "X-Project-Id")
        session_id = _scope_id(headers, "X-Session-Id")
        if session_id is not None and project_id is None:
            raise InvalidIdentityError("X-Session-Id requires X-Project-Id.")
        return Caller(
            client=None if client is None else client.name,
            admin=client is None or client.admin,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
        )

    def _client(self, key: str) -> AuthClient | None:
        digest = hashlib.sha256(key.encode()).hexdigest()
        matched = None
        # Compare every hash so the time taken does not depend on the match.
        for client in self._clients:
            for expected in client.key_sha256:
                if hmac.compare_digest(digest, expected):
                    matched = client
        return matched


def _scope_id(headers: Headers, name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        return None
    if not SCOPE_ID.fullmatch(value) or value == "unscoped":
        raise InvalidIdentityError(
            f"{name} must match {SCOPE_ID.pattern} and must not be 'unscoped'."
        )
    return value
