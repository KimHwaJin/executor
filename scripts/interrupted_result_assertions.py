"""Contract assertions shared by Docker interruption E2E and unit tests."""

from typing import Any

from executor_service.events import ExecutionStreamEnvelope


def validate_evidence(
    snapshot: dict[str, Any],
    result: dict[str, Any],
    events: list[dict[str, Any]],
    action: str,
) -> None:
    expected = "CANCELLED" if action == "cancel" else "FAILED"
    assert snapshot["result"] == result, "DB / HTTP result mismatch"
    database = snapshot["database"]
    assert database["status"] == expected
    assert database["lease_owner"] is None
    assert database["cancellation_lease_owner"] is None
    assert database["cleanup_status"] == "SUCCEEDED"
    assert database["runtime_session_id"] is None
    assert result["execution"]["state"]["status"] == expected
    operation = result["operations"][0]
    assert operation["result"]["status"] == expected
    steps = operation["steps"]
    assert len(steps) == 3
    assert steps[0]["result"]["status"] == "SUCCEEDED"
    assert steps[1]["result"]["status"] == expected
    assert steps[2]["result"]["status"] in {"SKIPPED", "CANCELLED"}
    assert steps[2]["result"]["result_ref"] is None
    files = snapshot["files"]
    assert [item["sequence"] for item in files] == [0, 1]
    assert files[0]["complete"] is True
    assert "completed-before-interrupt" in files[0]["text"]
    partial = files[1]
    assert partial["complete"] is False
    assert partial["state"] == "ABORTED"
    assert partial["error_message"], "Interruption reason missing"
    assert "before-interrupt" in partial["text"]
    assert "after-interrupt" not in partial["text"]
    assert partial["png_sizes"] and min(partial["png_sizes"]) > 1000
    assert partial["output_summary"] == steps[1]["result"]["output_summary"]

    envelopes = [
        ExecutionStreamEnvelope.model_validate(
            {key: item[key] for key in ExecutionStreamEnvelope.model_fields}
        ).model_dump(mode="json")
        for item in events
    ]
    assert [e["event_sequence"] for e in envelopes] == list(
        range(1, len(envelopes) + 1)
    )
    published: dict[str, Any] = {}
    for message in snapshot["redis"]:
        event = ExecutionStreamEnvelope.model_validate(
            message["event"]
        ).model_dump(mode="json")
        event_id = event["event_id"]
        # At-least-once delivery permits identical duplicate messages only.
        assert event_id not in published or published[event_id] == event
        published[event_id] = event
    assert published == {e["event_id"]: e for e in envelopes}
    for name in ("execution.operation_completed", "execution.completed"):
        terminal = [e for e in envelopes if e["event_type"] == name]
        assert len(terminal) == 1
        assert terminal[0]["payload"]["status"] == expected
    completed = {
        event["payload"]["step"]["sequence"]: event["payload"]
        for event in envelopes
        if event["event_type"] == "execution.step_completed"
    }
    assert set(completed) == {0, 1}
    for item in files:
        event = completed[item["sequence"]]
        ref = event["result_ref"]
        assert ref is not None
        assert ref["complete"] is item["complete"]
        for key in ("relative_path", "size_bytes", "checksum_sha256"):
            assert ref[key] == item["ref"][key]
    terminal_operation = next(
        e["payload"]
        for e in envelopes
        if e["event_type"] == "execution.operation_completed"
    )
    for item in terminal_operation["step_results"]:
        step_event = completed.get(item["sequence"])
        if step_event is not None:
            assert item["result_ref"] == step_event["result_ref"]
            assert item["attempt"] == step_event["attempt"]
        else:
            assert item["result_ref"] is None
