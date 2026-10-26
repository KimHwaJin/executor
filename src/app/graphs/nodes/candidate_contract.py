"""Candidate display adapters, independent of recommendation search."""
from copy import deepcopy
import hashlib
import json

from app.schemas.common.workflow_hitl_schema import CandidateSelectionDisplay
from app.schemas.common.workflow_hitl_actions import candidate_actions


def candidate_fingerprint(document: dict) -> str:
    """Compare execution structure, not names, actors or generated IDs."""
    workflow = document["workflow"]
    structure = {"steps": workflow.get("steps", []),
                 "execution_mode": workflow.get("execution_mode", "static")}
    return hashlib.sha256(json.dumps(structure, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def candidate_display(
    candidates: list[dict], *, can_regenerate: bool, can_recommend_again: bool = False,
    can_generate: bool = True, recommendation_search: dict | None = None,
) -> CandidateSelectionDisplay:
    origins = {c["origin"] for c in candidates}
    if len(origins) > 1:
        raise ValueError("Recommended and generated candidates require separate selection rounds")
    items = []
    for candidate in candidates:
        definition = candidate["workflow"]
        workflow = definition["workflow"]
        metadata = candidate.get("metadata") or {}
        item = {
            "candidate_id": candidate["id"], "origin": candidate["origin"],
            "description": candidate.get("description") or workflow.get("user_request") or workflow.get("goal") or "",
            "reason": candidate.get("reason") or metadata.get("reason") or (
                "사용자 요청을 바탕으로 생성한 후보입니다." if candidate["origin"] == "generated"
                else "추천 서비스에서 제공한 후보입니다."
            ),
            "workflow": deepcopy(definition),
        }
        if candidate["origin"] == "recommended":
            for key in ("workflow_id", "version_id", "revision", "similarity_score"):
                value = candidate.get(key, metadata.get(key))
                if value is not None:
                    item[key] = value
        if workflow.get("name"):
            item["name"] = workflow["name"]
        items.append(item)
    actions = candidate_actions(
        origin=next(iter(origins), None), has_candidates=bool(items),
        capabilities={"regenerate": can_regenerate, "recommend_again": can_recommend_again,
                      "generate": can_generate},
    )
    return CandidateSelectionDisplay(candidates=items, available_actions=actions,
                                     recommendation_search=recommendation_search)


def excluded_candidates(state: dict, candidates: list[dict]) -> list[dict]:
    history = deepcopy(state.get("excluded_workflow_candidates", []))
    seen = {item["fingerprint"] for item in history}
    for candidate in candidates:
        fingerprint = candidate_fingerprint(candidate["workflow"])
        if fingerprint not in seen:
            history.append({"fingerprint": fingerprint, "workflow": deepcopy(candidate["workflow"])})
            seen.add(fingerprint)
    return history
