from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
import os

class Settings(BaseSettings):
    """환경변수 기반 애플리케이션 설정."""

    app_name: str = "Chat CRUD API"
    api_v1_prefix: str = "/api/v1"
    # 로컬 기본값입니다. 배포 환경에서는 DATABASE_URL로 덮어쓰세요.
    database_url: str = (
        "postgresql+asyncpg://postgres:1234@127.0.0.1:5432/chat_app"
    )
    sql_echo: bool = False
    database_pool_size: int = dict(os.environ).get("DATABASE_POOL_SIZE","10")
    database_max_overflow: int = dict(os.environ).get("DATABASE_MAX_OVERFLOW","10")
    database_pool_timeout_seconds: float = 30.0
    database_pool_recycle_seconds: int = 300
    # run.py에서 사용하는 로컬 Uvicorn 설정입니다.
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    # Agent 산출물 생성 중 개발 reloader가 Worker를 죽이지 않도록 기본은 단일 프로세스입니다.
    server_reload: bool = False

    # Agent가 사용하는 기존 MODEL_* 환경변수를 FastAPI 설정과 같은 값으로 연결합니다.
    llm_provider: str = Field(
        default="openai_compatible",
        validation_alias=AliasChoices("MODEL_PROVIDER", "LLM_PROVIDER"),
    )
    llm_api_base_url: str = Field(
        default="http://model.frodo.com/v1",
        validation_alias=AliasChoices("API_BASE_URL", "LLM_API_BASE_URL"),
    )
    llm_api_key: str = Field(
        default="dummy",
        validation_alias=AliasChoices("MODEL_API_KEY", "LLM_API_KEY"),
    )
    llm_timeout_seconds: float = Field(
        default=60.0,
        validation_alias=AliasChoices("MODEL_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"),
    )
    llm_retry_backoff_seconds: float = 0.5
    # E03: Worker가 heartbeat를 갱신하지 못했을 때 Run을 stale로 판단하는 lease 길이입니다.
    task_lease_seconds: int = 300
    task_reconcile_interval_seconds: int = 30
    # 단일 서버/로컬에서는 API lifespan과 함께 Reconciler를 실행합니다.
    # 운영에서 별도 Reconciler Deployment를 둘 때는 false로 끌 수 있습니다.
    task_reconciler_enabled: bool = True
    # E05: 별도 Pod 없이 Gaia API Router lifespan 안에서 durable DB queue를 소비합니다.
    agent_worker_enabled: bool = True
    agent_worker_poll_interval_seconds: float = 0.25
    # 최초 실행은 제외한 자동 재시도 횟수입니다. 3이면 총 최대 4회 실행합니다.
    agent_worker_max_retries: int = 3
    # 워커가 한 그래프 턴을 붙잡는 상한입니다. 넘기면 호출을 취소하고 재시도 큐로 되돌립니다.
    agent_run_turn_timeout_seconds: float = Field(default=120, gt=0)
    # 상한 초과 뒤 취소가 끝날 때까지 기다리는 시간입니다. 넘기면 그 호출을 놓습니다.
    agent_run_turn_cancel_grace_seconds: float = Field(default=2, gt=0)
    # 재시도 폭주를 막기 위한 지수 backoff의 시작/최대 대기 시간입니다.
    agent_worker_retry_backoff_seconds: float = 1.0
    agent_worker_retry_max_backoff_seconds: float = 30.0
    # 실행 중 취소 요청을 다른 API worker에서도 감지하기 위한 DB polling 주기입니다.
    # 동시성
    agent_run_worker_concurrency: int = dict(os.environ).get("AGENT_RUN_WORKER_CONCURRENCY","4")
    task_reconciler_conncurrency: int = dict(os.environ).get("TASK_RECONCILER_CONCURRENCY","4")
    ##
    task_cancel_poll_interval_seconds: float = 0.25
    # LangGraph HITL state를 API 재시작 뒤에도 재개하기 위한 비동기 PostgreSQL checkpoint입니다.
    graph_checkpointer: str = "postgres"
    checkpoint_db_uri: str = (
        "postgresql://postgres:1234@127.0.0.1:5432/chat_app?sslmode=disable"
    )
    # E13: DB에는 이 root 기준 상대 경로만 저장해 서버 이동과 path traversal 방지를 돕습니다.
    workflow_storage_root: Path = Path("var/workflows")
    # E10-T04: comma-separated exact host allowlist. 운영 내부 DNS는 환경변수로 추가합니다.
    jupyter_allowed_hosts: str = "127.0.0.1,localhost"
    jupyter_health_timeout_seconds: float = 5.0
    # Fernet key. Token을 등록할 때 비어 있으면 503으로 거부해 평문 저장을 막습니다.
    jupyter_token_encryption_key: str = ""
    # E05-T05: Redis 없이 DB Event Store를 조회하는 SSE polling 설정입니다.
    sse_poll_interval_seconds: float = 0.5
    sse_heartbeat_seconds: float = 15.0
    sse_event_batch_size: int = 100
    llm_token_flush_interval_seconds: float = 0.2
    llm_token_flush_characters: int = 256
    # 로컬에서는 외부 Jupyter/executor가 없으므로 제출 직전 성공 응답으로 대체합니다.
    executor_submit_enabled: bool = False
    # executor 비활성 상태에서도 생성 코드 파일은 쓰기 가능한 로컬 경로에 보존합니다.
    executor_shared_input_root: Path = Field(
        default=Path("/workspace/pv"),
        validation_alias="EXECUTOR_SHARED_INPUT_ROOT",
    )
    # Workflow 송수신용 Redis broker. 비밀번호 특수문자는 URL 인코딩이 필요할 수 있습니다.
    redis_host: str = "127.0.0.1:6379"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_ping_timeout_seconds: float = 5.0
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
