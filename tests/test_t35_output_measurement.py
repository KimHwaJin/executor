import argparse
import base64
import contextlib
import io
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    T35 = runpy.run_path(
        str(SCRIPTS / "t35_output_measurement.py"),
        run_name="t35_output_measurement_test",
    )
finally:
    sys.path.pop(0)

DatabaseSnapshot = T35["DatabaseSnapshot"]
Scenario = T35["Scenario"]
database_delta = T35["database_delta"]
parse_scenario = T35["parse_scenario"]
require_safe_executor_url = T35["_require_safe_executor_url"]
scenario_matrix = T35["scenario_matrix"]
workload_code = T35["workload_code"]
validate_workload_output = T35["validate_workload_output"]


def test_full_matrix_has_every_required_size_and_concurrency() -> None:
    scenarios = scenario_matrix("full")

    assert len(scenarios) == 40
    assert {
        item.size_mib for item in scenarios if item.output_type == "TEXT"
    } == {1, 5, 10, 25, 50, 100}
    assert {
        item.size_mib for item in scenarios if item.output_type == "IMAGE"
    } == {1, 10, 25, 50}
    assert {item.concurrency for item in scenarios} == {1, 5, 10, 20}


def test_scenario_parsing_is_strict() -> None:
    scenario = parse_scenario("text:5:10")
    assert scenario.name == "text-5mib-concurrency-10"

    with pytest.raises(argparse.ArgumentTypeError):
        parse_scenario("TABLE:1:1")


def test_text_workload_emits_the_requested_number_of_bytes() -> None:
    scenario = Scenario("TEXT", 1, 1)
    output = io.StringIO()

    with contextlib.redirect_stdout(output):
        exec(
            workload_code(scenario, run_id="text-run", index=0),
            {},
        )

    assert len(output.getvalue().encode()) == 1024 * 1024
    assert output.getvalue().startswith("T35:text-run:")
    validation = validate_workload_output(
        [
            {
                "output_type": "stream",
                "name": "stdout",
                "text": output.getvalue(),
            }
        ],
        scenario,
        run_id="text-run",
        index=0,
    )
    assert validation["status"] == "PASSED"


def test_image_workload_emits_an_exact_valid_png_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}
    ipython = ModuleType("IPython")
    display_module = ModuleType("IPython.display")

    def display(value: dict[str, str], *, raw: bool) -> None:
        assert raw is True
        captured.update(value)

    display_module.__dict__["display"] = display
    monkeypatch.setitem(sys.modules, "IPython", ipython)
    monkeypatch.setitem(sys.modules, "IPython.display", display_module)

    exec(
        workload_code(Scenario("IMAGE", 1, 1), run_id="image-run", index=0),
        {},
    )
    image = base64.b64decode(captured["image/png"], validate=True)

    assert len(image) == 1024 * 1024
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"T35:image-run:" in image
    validation = validate_workload_output(
        [{"output_type": "display_data", "data": captured}],
        Scenario("IMAGE", 1, 1),
        run_id="image-run",
        index=0,
    )
    assert validation["status"] == "PASSED"


def test_successful_execution_with_only_rate_warning_is_not_a_pass() -> None:
    validation = validate_workload_output(
        [
            {
                "output_type": "stream",
                "name": "stderr",
                "text": "IOPub data rate exceeded.",
            }
        ],
        Scenario("TEXT", 5, 1),
        run_id="rate-limited",
        index=0,
    )
    assert validation["status"] == "FAILED"
    assert validation["retained_bytes"] == 0
    assert validation["expected_bytes"] == 5 * 1024 * 1024


def test_truncated_text_with_correct_marker_is_not_a_pass() -> None:
    validation = validate_workload_output(
        [
            {
                "output_type": "stream",
                "name": "stdout",
                "text": "T35:short:text-5mib-concurrency-1:0:x",
            }
        ],
        Scenario("TEXT", 5, 1),
        run_id="short",
        index=0,
    )
    assert validation["status"] == "FAILED"
    assert any("retained" in error for error in validation["errors"])


def test_invalid_png_is_not_a_pass() -> None:
    validation = validate_workload_output(
        [
            {
                "output_type": "display_data",
                "data": {"image/png": "!not-base64!"},
            }
        ],
        Scenario("IMAGE", 5, 1),
        run_id="invalid-image",
        index=0,
    )
    assert validation["status"] == "FAILED"


def test_database_delta_reports_storage_and_row_growth() -> None:
    before = DatabaseSnapshot(
        database_bytes=100,
        table_bytes={name: 10 for name in T35["MEASURED_TABLES"]},
        table_rows={name: 1 for name in T35["MEASURED_TABLES"]},
    )
    after = DatabaseSnapshot(
        database_bytes=160,
        table_bytes={name: 20 for name in T35["MEASURED_TABLES"]},
        table_rows={name: 3 for name in T35["MEASURED_TABLES"]},
    )

    delta = database_delta(before, after)

    assert delta["database_bytes"] == 60
    assert set(delta["table_bytes"].values()) == {10}
    assert set(delta["table_rows"].values()) == {2}


def test_measurement_includes_durable_events_and_outbox() -> None:
    assert {
        "execution_events",
        "execution_event_sequences",
        "outbox_events",
    }.issubset(T35["MEASURED_TABLES"])


def test_t35_rejects_remote_executor_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("T35_ALLOW_REMOTE", raising=False)
    require_safe_executor_url("http://127.0.0.1:8000")

    with pytest.raises(RuntimeError, match="non-loopback"):
        require_safe_executor_url("https://executor.example.internal")
