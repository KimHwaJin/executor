"""Startup ordering and Deployment-only configuration contracts."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from executor_service.container import ApplicationContainer
from executor_service.infrastructure.db.migrations import (
    DatabaseMigrationError,
)
from executor_service.interfaces.http.app import create_app
from executor_service.settings import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_auto_migrate_is_opt_in_without_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DB_AUTO_MIGRATE", raising=False)
    assert Settings(_env_file=None).db_auto_migrate is False
    monkeypatch.setenv("DB_AUTO_MIGRATE", "true")
    assert Settings(_env_file=None).db_auto_migrate is True


@pytest.mark.parametrize("enabled", [False, True])
async def test_migration_precedes_any_db_initialization_or_background_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    container = ApplicationContainer(
        Settings(
            _env_file=None,
            shared_storage_root=tmp_path,
            db_auto_migrate=enabled,
            runtime_enabled=False,
        )
    )
    calls: list[str] = []
    migrate = AsyncMock(side_effect=lambda *_: calls.append("migration"))
    monkeypatch.setattr("executor_service.container.upgrade_database", migrate)
    monkeypatch.setattr(
        "executor_service.container.check_redis_compatibility",
        AsyncMock(side_effect=lambda *_: calls.append("redis-compatibility")),
    )
    monkeypatch.setattr(
        container.maintenance,
        "initialize",
        AsyncMock(side_effect=lambda: calls.append("maintenance")),
    )
    monkeypatch.setattr(
        container.event_retention,
        "initialize",
        AsyncMock(side_effect=lambda: calls.append("retention-init")),
    )
    monkeypatch.setattr(
        container.outbox_publisher, "start", lambda: calls.append("outbox")
    )
    monkeypatch.setattr(
        container.event_retention,
        "start",
        lambda: calls.append("retention-start"),
    )
    try:
        await container.start()
        expected = [
            "redis-compatibility",
            "maintenance",
            "retention-init",
            "outbox",
            "retention-start",
        ]
        assert calls == (["migration"] if enabled else []) + expected
    finally:
        await container.stop()


async def test_failed_migration_prevents_serving_and_closes_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = ApplicationContainer(
        Settings(
            _env_file=None,
            shared_storage_root=tmp_path,
            db_auto_migrate=True,
        )
    )
    monkeypatch.setattr(
        "executor_service.container.upgrade_database",
        AsyncMock(side_effect=DatabaseMigrationError("migration rejected")),
    )
    init = AsyncMock()
    start_worker = AsyncMock()
    stop = AsyncMock(side_effect=container.stop)
    monkeypatch.setattr(container.maintenance, "initialize", init)
    monkeypatch.setattr(container.execution_worker, "start", start_worker)
    monkeypatch.setattr(container, "stop", stop)
    app = create_app(container)
    with pytest.raises(DatabaseMigrationError):
        async with app.router.lifespan_context(app):
            raise AssertionError(
                "Application must not serve after migration failure"
            )
    init.assert_not_awaited()
    start_worker.assert_not_awaited()
    stop.assert_awaited_once()


def test_deployment_has_inline_settings_and_safe_update_strategy() -> None:
    deployment = yaml.safe_load(
        (ROOT / "deploy/kubernetes/deployment.yaml").read_text()
    )
    assert deployment["kind"] == "Deployment"
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
    }
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert "envFrom" not in container
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert len(env) == len(container["env"])
    assert env["DB_AUTO_MIGRATE"] == "true"
    assert env["DB_MIGRATIONS_PATH"] == "/app/migrations"
    for key in ("DATABASE_URL", "REDIS_URL", "RUNTIME_CREDENTIAL_KEY"):
        assert "REPLACE" in env[key]
    assert "secretKeyRef" not in str(deployment)
    assert "configMapKeyRef" not in str(deployment)
    assert container["startupProbe"]["failureThreshold"] * (
        container["startupProbe"]["periodSeconds"]
    ) > int(env["DB_MIGRATION_LOCK_TIMEOUT_SECONDS"]) + int(
        env["DB_MIGRATION_STATEMENT_TIMEOUT_SECONDS"]
    )
    assert env["SHARED_STORAGE_ROOT"] in {
        mount["mountPath"] for mount in container["volumeMounts"]
    }
    for old in ("configmap.yaml", "secret.example.yaml", "migration-job.yaml"):
        assert not (ROOT / "deploy/kubernetes" / old).exists()


def test_compose_uses_app_startup_migration_without_separate_job() -> None:
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())[
        "services"
    ]
    assert "migrate" not in services
    assert "migrate" not in services["executor"].get("depends_on", {})
    assert "DB_AUTO_MIGRATE" in services["executor"]["environment"]


def test_compose_separates_shared_infrastructure_and_preserves_volumes() -> (
    None
):
    app = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    infra = yaml.safe_load((ROOT / "docker-compose.infra.yml").read_text())
    assert "postgres" not in app["services"]
    assert "redis" not in app["services"]
    assert set(infra["services"]) == {"postgres", "redis"}
    assert infra["name"] == "shared-infra"
    assert set(app["services"]["executor"]["networks"]) == {
        "default",
        "shared-infra",
        "runtimes",
    }
    assert app["networks"]["shared-infra"]["external"] is True
    assert (
        app["networks"]["shared-infra"]["name"]
        == (infra["networks"]["default"]["name"])
    )
    assert infra["services"]["postgres"]["image"] == "pgvector/pgvector:pg17"
    assert infra["volumes"]["postgres-data"]["external"] is True
    assert infra["volumes"]["postgres-data"]["name"] == (
        "${SHARED_POSTGRES_VOLUME:-executor_executor-postgres}"
    )


def test_runtime_compose_preserves_fleet_and_storage() -> None:
    app = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    runtime = yaml.safe_load((ROOT / "docker-compose.runtime.yml").read_text())
    assert set(app["services"]) == {"executor"}
    assert runtime["name"] == "executor-runtimes"
    assert app["networks"]["runtimes"]["external"] is True
    assert (
        app["networks"]["runtimes"]["name"]
        == (runtime["networks"]["default"]["name"])
    )
    expected_ports = {
        "jupyter": "8888:8888",
        "jupyter-secondary": "8889:8888",
        "jupyter-batch-primary": "8890:8888",
        "jupyter-batch-secondary": "8891:8888",
    }
    assert set(runtime["services"]) == set(expected_ports)
    for service, port in expected_ports.items():
        definition = runtime["services"][service]
        assert definition["image"] == "executor-jupyter:local"
        assert definition["ports"] == [port]
        assert definition["volumes"] == [
            "./test_harness/jupyter/workspace:/workspace/pv"
        ]
        assert "profiles" not in definition
    app_overlay = yaml.safe_load(
        (ROOT / "docker-compose.test.yml").read_text()
    )
    runtime_overlay = yaml.safe_load(
        (ROOT / "docker-compose.runtime.test.yml").read_text()
    )
    assert set(app_overlay["services"]) == {"executor"}
    assert set(runtime_overlay["services"]) == set(expected_ports)
