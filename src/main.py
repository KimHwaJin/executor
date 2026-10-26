from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, RedirectResponse

from agent_config import load_agent_settings, require_executor_tenant
from app.api.v1.router import api_router
from config import settings
from app.core.problems import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.services.agent_graph_service import runtime
from app.services.async_io_resources import close_async_io_resources


@asynccontextmanager
async def lifespan(_app: FastAPI):
    require_executor_tenant(load_agent_settings())
    try:
        yield
    finally:
        try:
            await runtime.shutdown()
        finally:
            await close_async_io_resources()


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description="User → Project → Session → Message CRUD + Agent graph",
    lifespan=lifespan,
)
app.include_router(api_router, prefix=settings.api_v1_prefix)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Propagate a request ID so API calls can be traced end-to-end."""
    request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex}"
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/demo")


@app.get("/demo", include_in_schema=False)
async def demo_frontend():
    """Mock 데이터로 계획 수립 HITL을 시험하는 단일 페이지 화면입니다."""

    return FileResponse(Path(__file__).parent / "app" / "static" / "demo.html")


@app.get("/workflow-test", include_in_schema=False)
async def workflow_test_frontend():
    """Task 중심 Workflow REST/SSE API 통합 테스트 화면."""
    return FileResponse(Path(__file__).parent / "app" / "static" / "workflow-test.html")


@app.get("/demo_temp.html", include_in_schema=False)
@app.get("/demo_temp", include_in_schema=False)
async def workflow_chat_demo_frontend():
    return FileResponse(Path(__file__).parent / "app" / "static" / "demo_temp.html")


@app.get("/demo_v2.html", include_in_schema=False)
@app.get("/demo_v2", include_in_schema=False)
async def workflow_hitl_v2_demo_frontend():
    return FileResponse(Path(__file__).parent / "app" / "static" / "demo_v2.html")
