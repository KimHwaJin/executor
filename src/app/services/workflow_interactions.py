"""Pure projection of persisted interrupts; IDs survive process restarts."""
from uuid import NAMESPACE_URL, uuid5

from app.schemas.common.workflow_task_schema import InteractionResource


def interactions(run):
    result = []
    for index, item in enumerate(run.interrupt or []):
        if item.get("kind") == "EXECUTOR_EVENT":
            continue
        result.append({**item,
            "interaction_id": str(uuid5(NAMESPACE_URL, f"workflow_run:{run.run_id}:{index}")),
            "revision": 1,
        })
    return result


def public_interactions(run):
    if run.task_id is None:
        return []
    result = []
    for item in interactions(run):
        requests = item.get("action_requests", [])
        configs = item.get("review_configs", [])
        if len(requests) != 1 or len(configs) != 1:
            continue
        action, config = requests[0], configs[0]
        name = action["name"]
        if name == "next_user_request":
            continue
        input_schema = config.get("args_schema", {})
        actions = []
        for decision in config.get("allowed_decisions", []):
            schema = input_schema if decision == "edit" else (
                {"type": "string"} if decision in {"respond", "reject"} else {"type": "object"}
            )
            if item.get("payload_version", 1) == 2 and decision == "approve":
                schema = {"type": "object", "additionalProperties": False}
            actions.append({"name": decision, "label": {
                "approve": "승인", "edit": ("이유와 함께 거절" if name == "workflow_approval"
                    and item.get("payload_version", 1) == 2 else "입력 제출"), "respond": "답변", "reject": "수정 요청",
            }.get(decision, decision), "input_schema": schema})
        result.append(InteractionResource(
            interaction_id=item["interaction_id"], revision=item["revision"], run_id=run.run_id,
            kind={"workflow_approval": "plan_review", "analysis_context": "planning_question"}.get(name, name),
            payload_version=item.get("payload_version", 1),
            summary=action.get("description", ""),
            payload={"action_name": name, "arguments": action.get("args", {}), "input_schema": input_schema},
            actions=actions,
            resume_target={"task_id": run.task_id, "run_id": run.run_id,
                           "interaction_id": item["interaction_id"], "revision": item["revision"]},
        ).model_dump(mode="json"))
    return result
