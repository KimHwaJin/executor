"""Workflow catalog HTTP adapter; persistence and embeddings live in the service."""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.schemas.common.workflow_catalog_schema import (
    CatalogMetadata, DetailResponse, ErrorResponse, ExecutionMode, ListItem,
    ListResponse, RecommendYN, RecommendationRequest, RecommendationResponse,
    RegisterRequest, RegistrationType, SearchItem, SearchRequest, SearchResponse,
    UpdateRequest, VersionDetail, WriteResponse,
)
from app.services.workflow_catalog_service import (
    WorkflowCatalogService, WorkflowEmbeddingError, WorkflowEmbeddingTimeoutError,
    WorkflowIdempotencyConflictError, WorkflowNotFoundError,
    WorkflowRevisionConflictError, WorkflowValidationError,
)


def catalog_error(status_code, code, message, details=None):
    return JSONResponse(status_code=status_code, content={"error": {
        "code": code, "message": message, "details": details or [],
    }})


def _handle_service_error(exc):
    # Never send exception strings: they can contain SQL, URLs or credentials.
    if isinstance(exc, WorkflowRevisionConflictError):
        return catalog_error(409, exc.code, "최신 버전을 조회한 뒤 다시 수정해주세요.", [{
            "field": "expected_revision", "reason": "현재 버전과 일치하지 않습니다.",
            "expected_revision": exc.expected_revision, "current_revision": exc.current_revision,
        }])
    if isinstance(exc, WorkflowIdempotencyConflictError):
        return catalog_error(409, exc.code, "같은 요청 키가 다른 요청에 사용되었습니다.")
    if isinstance(exc, WorkflowNotFoundError):
        return catalog_error(404, exc.code, "워크플로우 또는 요청한 버전을 찾을 수 없습니다.")
    if isinstance(exc, WorkflowEmbeddingTimeoutError):
        return catalog_error(504, exc.code, "임베딩 서비스 응답 시간이 초과되었습니다.")
    if isinstance(exc, WorkflowEmbeddingError):
        return catalog_error(502, exc.code, "임베딩 처리에 실패했습니다.")
    if isinstance(exc, WorkflowValidationError):
        return catalog_error(422, exc.code, "워크플로우 구조 또는 Skill/Tool 연결을 확인해주세요.",
                             [{"field": "workflow", "reason": "워크플로우 검증에 실패했습니다."}])
    if isinstance(exc, ValueError):
        return catalog_error(422, "REQUEST_VALIDATION_ERROR", "요청값이 올바르지 않습니다.")
    return catalog_error(500, "INTERNAL_ERROR", "워크플로우 처리 중 내부 오류가 발생했습니다.")


class CatalogRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError as exc:
                details = [{"field": ".".join(str(p) for p in e["loc"][1:]),
                            "reason": "요청 필드의 형식 또는 값이 올바르지 않습니다."}
                           for e in exc.errors()]
                return catalog_error(422, "REQUEST_VALIDATION_ERROR", "요청값을 확인해주세요.", details)
            except Exception:
                # Includes response-contract errors; never expose internal data.
                return catalog_error(500, "INTERNAL_ERROR", "워크플로우 처리 중 내부 오류가 발생했습니다.")
        return handler


router = APIRouter(prefix="/workflows", tags=["Workflow catalog"], route_class=CatalogRoute)
_catalog_service: WorkflowCatalogService | None = None


def get_workflow_catalog_service() -> WorkflowCatalogService:
    global _catalog_service
    if _catalog_service is None:
        _catalog_service = WorkflowCatalogService.from_environment()
    return _catalog_service


def _responses(*codes):
    descriptions = {404: "대상 또는 버전 없음", 409: "버전 또는 요청 키 충돌", 422: "검증 실패",
                    500: "내부 오류", 502: "임베딩 오류", 504: "임베딩 시간 초과"}
    return {code: {"model": ErrorResponse, "description": descriptions[code]} for code in codes}


def _select(model, row):
    return {name: row[name] for name in model.model_fields}


def _version_arguments(payload):
    values = payload.model_dump(exclude={"workflow"})
    values["workflow"] = payload.workflow.model_dump(mode="json", exclude_unset=True)
    return values


def _check_key(key):
    if key is not None and (not 1 <= len(key) <= 255 or key != key.strip()):
        raise ValueError("Invalid Idempotency-Key")


@router.post("", status_code=201, summary="워크플로우 최초 등록", operation_id="registerWorkflow",
             response_model=WriteResponse, responses=_responses(422, 409, 500, 502, 504))
async def register_workflow(payload: RegisterRequest,
                            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    try:
        _check_key(idempotency_key)
        row = await get_workflow_catalog_service().register(
            **_version_arguments(payload), idempotency_key=idempotency_key)
    except Exception as exc:
        return _handle_service_error(exc)
    return _select(WriteResponse, row)


@router.get("", summary="현재 버전 기준 관리용 목록", operation_id="listWorkflows",
            response_model=ListResponse, responses=_responses(422, 500))
async def list_workflows(
    q: str | None = None, recommend_yn: RecommendYN | None = None,
    execution_mode: ExecutionMode | None = None, registration_type: RegistrationType | None = None,
    created_by: UUID | None = None, page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    sort_by: Literal["created_at", "updated_at", "name"] = "updated_at",
    sort_order: Literal["asc", "desc"] = "desc",
):
    try:
        result = await get_workflow_catalog_service().list(
            q=q, recommend_yn=recommend_yn, execution_mode=execution_mode,
            registration_type=registration_type, created_by=created_by, page=page,
            page_size=page_size, sort_by=sort_by, sort_order=sort_order)
    except Exception as exc:
        return _handle_service_error(exc)
    return {**result, "items": [_select(ListItem, row) for row in result["items"]]}


@router.post("/search", summary="추천용 의미 기반 검색", operation_id="searchWorkflows",
             response_model=SearchResponse, responses=_responses(422, 500, 502, 504))
async def search_workflows(payload: SearchRequest):
    try:
        result = await get_workflow_catalog_service().search(**payload.model_dump())
    except Exception as exc:
        return _handle_service_error(exc)
    return {**result, "items": [_select(SearchItem, row) for row in result["items"]]}


@router.get("/{workflow_id}", summary="현재 또는 지정 버전 상세 조회", operation_id="getWorkflow",
            response_model=DetailResponse, responses=_responses(404, 422, 500))
async def get_workflow(workflow_id: UUID, revision: int | None = Query(default=None, ge=1)):
    try:
        row = await get_workflow_catalog_service().get(workflow_id, revision=revision)
    except Exception as exc:
        return _handle_service_error(exc)
    version = {name: row[name] for name in VersionDetail.model_fields
               if name not in ("created_by", "created_at")}
    version.update(created_by=row["version_created_by"], created_at=row["version_created_at"])
    return {**_select(CatalogMetadata, row), "version": version}


@router.put("/{workflow_id}", summary="내용 수정 및 새 버전 생성", operation_id="updateWorkflow",
            response_model=WriteResponse, responses=_responses(404, 409, 422, 500, 502, 504))
async def update_workflow(workflow_id: UUID, payload: UpdateRequest,
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    try:
        _check_key(idempotency_key)
        row = await get_workflow_catalog_service().update(
            workflow_id, **_version_arguments(payload), idempotency_key=idempotency_key)
    except Exception as exc:
        return _handle_service_error(exc)
    return _select(WriteResponse, row)


@router.patch("/{workflow_id}/recommendation", summary="추천 허용 여부 변경",
              operation_id="updateWorkflowRecommendation", response_model=RecommendationResponse,
              responses=_responses(404, 422, 500))
async def update_workflow_recommendation(workflow_id: UUID, payload: RecommendationRequest):
    try:
        row = await get_workflow_catalog_service().set_recommendation(workflow_id, **payload.model_dump())
    except Exception as exc:
        return _handle_service_error(exc)
    return _select(RecommendationResponse, row)


@router.delete("/{workflow_id}", status_code=204, summary="논리 삭제",
               operation_id="deleteWorkflow", responses=_responses(404, 422, 500))
async def delete_workflow(workflow_id: UUID, deleted_by: UUID = Query(...)):
    try:
        await get_workflow_catalog_service().delete(workflow_id, deleted_by=deleted_by)
    except Exception as exc:
        return _handle_service_error(exc)
    return Response(status_code=204)
