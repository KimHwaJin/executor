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
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
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
    assert "migrate" not in services["executor"]["depends_on"]
    assert "DB_AUTO_MIGRATE" in services["executor"]["environment"]
