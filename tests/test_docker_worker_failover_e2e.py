import runpy
import sys
from pathlib import Path

import pytest

SCRIPT_DIRECTORY = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))
try:
    SCRIPT = runpy.run_path(
        str(SCRIPT_DIRECTORY / "docker_worker_failover_e2e.py"),
        run_name="docker_worker_failover_e2e_test",
    )
finally:
    sys.path.remove(str(SCRIPT_DIRECTORY))

Config = SCRIPT["Config"]
Compose = SCRIPT["Compose"]
FailoverValidationError = SCRIPT["FailoverValidationError"]
owner_target = SCRIPT["_owner_target"]


def _config() -> object:
    return Config(
        compose_file=Path("compose.worker-failover.yaml"),
        project_name="executor-worker-failover-test",
        primary_port=8010,
        secondary_port=8011,
        runtime_profile="basic",
        step_duration_seconds=30,
        lease_timeout_seconds=180,
        completion_timeout_seconds=300,
        report_path=Path("test-results/failover.json"),
        jupyter_token="test-token",
        allow_container_kill=True,
        keep_stack=False,
        build_jupyter_image=False,
    )


def test_owner_target_kills_only_owner_and_selects_survivor_api() -> None:
    config = _config()
    assert owner_target("docker-failover-primary", config) == (
        "executor-primary",
        "http://127.0.0.1:8011",
    )
    assert owner_target("docker-failover-secondary", config) == (
        "executor-secondary",
        "http://127.0.0.1:8010",
    )


def test_owner_target_rejects_unmanaged_consumer() -> None:
    with pytest.raises(FailoverValidationError, match="Unexpected"):
        owner_target("unmanaged-worker", _config())


def test_compose_command_is_scoped_to_unique_project_and_file() -> None:
    config = _config()
    command = Compose(config)._command("kill", "executor-primary")
    assert command == [
        "docker",
        "compose",
        "--project-name",
        "executor-worker-failover-test",
        "--file",
        "compose.worker-failover.yaml",
        "kill",
        "executor-primary",
    ]


def test_compose_environment_does_not_change_parent_environment() -> None:
    config = _config()
    environment = Compose(config)._environment()
    assert environment["DOCKER_FAILOVER_PRIMARY_PORT"] == "8010"
    assert environment["DOCKER_FAILOVER_SECONDARY_PORT"] == "8011"
    assert environment["DOCKER_FAILOVER_JUPYTER_TOKEN"] == "test-token"
