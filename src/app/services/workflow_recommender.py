"""Agent adapter for the shared WorkflowCatalogService vector search."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from app.services.workflow_catalog_service import WorkflowCatalogService


@dataclass
class WorkflowRecommender:
    """Return a stored Workflow recommendation, or None when no match exists."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    catalog_service: WorkflowCatalogService | None = None
    minimum_similarity: float = 0.8
    recommendation_limit: int = 3

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if self.catalog_service is not None:
            raise RuntimeError("Catalog recommendation requires async ainvoke()")
        self.calls.append(payload)
        return None

    async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        self.calls.append(payload)
        if self.catalog_service is None:
            return None
        from app.graphs.nodes.candidate_contract import candidate_fingerprint
        search_options = {}
        if payload.get("exclude_workflow_ids"):
            search_options["exclude_workflow_ids"] = payload["exclude_workflow_ids"]
        result = await self.catalog_service.search(
            payload["user_request"], limit=self.recommendation_limit, threshold=self.minimum_similarity, **search_options,
        )
        excluded = {item["fingerprint"] for item in payload.get("excluded_workflow_candidates", [])}
        recommendations = []
        for item in result["items"]:
            if item["similarity"] < self.minimum_similarity:
                continue
            if candidate_fingerprint(item["workflow"]) in excluded:
                continue
            recommendations.append({
                "workflow_id": str(item["workflow_id"]),
                "version_id": str(item["version_id"]), "revision": item["revision"],
                "similarity_score": item["similarity"],
                "reason": "사용자 요청과의 유사도가 추천 기준을 충족한 저장 워크플로우입니다.",
                "workflow": item["workflow"],
            })
            if len(recommendations) >= self.recommendation_limit:
                break
        if recommendations:
            return {
                "recommendation_available": True,
                "recommendation": recommendations[0],
                "recommendations": recommendations,
                "no_match_reason": None,
            }
        return None


__all__ = ["WorkflowRecommender"]
