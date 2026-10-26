"""Central configuration for the user-agent LangGraph application.

Secrets are read from environment variables (or .env for local development)
and are never committed here. The internal variable MODEL_API_KEY is
intentionally preserved to match the deployment contract.
"""

from __future__ import annotations

import os
import json
import re
from math import isfinite
from dataclasses import dataclass, replace
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

USER_LLM_MODEL = "GaiA-Big-Latest"
MAIN_MODEL_PRESETS = {
    "GaiA-Big-Latest": {
        "model": "GLM-5.3",
        "parameters": {"reasoning_effort": "low", "clear_thinking": False,
                       "temperature": 1.0, "top_p": 0.95},
    },
    "GaiA-Medium-Latest": {
        "model": "GLM-5.3-Flash",
        "parameters": {"reasoning_effort": "low", "clear_thinking": False,
                       "temperature": 1.0, "top_p": 0.95},
    },
    "GaiA-Small-Latest": {
        "model": "gaia-Qwen3.6-35B-A3B",
        # General-task sampling values, with thinking explicitly disabled.
        "parameters": {"temperature": 1.0, "top_p": 0.95, "top_k": 20,
                       "min_p": 0.0, "presence_penalty": 1.5,
                       "repetition_penalty": 1.0, "enable_thinking": False},
    },
}


# LLM_MODEL_NAME takes precedence over USER_LLM_MODEL at settings load time.
# Use a preset alias or a deployed model name.
# Parameters override preset defaults; arbitrary deployed names start with no defaults.
MAIN_MODEL_CONFIG = {"parameters": {}}


def resolve_main_model(value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve an alias and overlay explicitly supplied generation parameters."""
    value = value or {}
    name = value.get("name", USER_LLM_MODEL)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Model name must be a non-empty string")
    preset = MAIN_MODEL_PRESETS.get(name)
    if preset is None:
        preset = next((item for item in MAIN_MODEL_PRESETS.values() if item["model"] == name), {})
    parameters = deepcopy(preset.get("parameters", {}))
    parameters.update(deepcopy(value.get("parameters", {})))
    # These are connection/identity settings, not generation parameters.
    reserved = {"model", "api_key", "base_url", "api_base", "azure_endpoint"}
    extra = parameters.get("extra_body", {})
    if not isinstance(extra, dict):
        raise ValueError("parameters.extra_body must be an object")
    if reserved & (parameters.keys() | extra.keys()):
        raise ValueError("Model identity and connection settings are not generation parameters")
    json.dumps(parameters, allow_nan=False)
    for key in ("max_output_tokens", "max_tokens"):
        if key in parameters and (type(parameters[key]) is not int or parameters[key] <= 0):
            raise ValueError(f"{key} must be a positive integer")
    if "max_output_tokens" in parameters and "max_tokens" in parameters:
        raise ValueError("Specify only one of max_output_tokens or max_tokens")
    return {"name": name, "parameters": parameters}


def with_main_model(settings: AgentSettings, value: Mapping[str, Any]) -> AgentSettings:
    """Apply only chat-model settings; embedding/connection settings stay intact."""
    parameters = deepcopy(value["parameters"])
    return replace(settings, model_name=MAIN_MODEL_PRESETS.get(value["name"], {}).get("model", value["name"]),
                   model_temperature=parameters.get("temperature", settings.model_temperature),
                   model_enable_thinking=parameters.get("enable_thinking"),
                   main_model=deepcopy(dict(value)))

DEFAULT_EXECUTOR_SHARED_INPUT_ROOT = Path("/workspace/shared")
DEFAULT_MOCK_DATA_ROOT = Path("/workspace/pv")
MOCK_DATA_PATH_BY_TYPE = {
    "nce": "df_nce_wide_format.parquet",
    "wt_symbol": "df_wt_symbol_wide_format.parquet",
}


def resolve_mock_data_path(data_type: str) -> Path:
    """Resolve a mock path from the configured data root and type mapping."""
    normalized_type = str(data_type).strip().lower()
    try:
        filename = MOCK_DATA_PATH_BY_TYPE[normalized_type]
    except KeyError as error:
        supported = ", ".join(sorted(MOCK_DATA_PATH_BY_TYPE))
        raise ValueError(
            f"Unsupported mock data_type {normalized_type!r}; "
            f"supported: {supported}"
        ) from error
    mock_data_root = Path(
        os.environ.get(
            "MOCK_DATA_ROOT",
            str(DEFAULT_MOCK_DATA_ROOT),
        )
    )
    return mock_data_root / "data" / filename


LOCAL_MOCK_USER_ID = "mock-user-001"
LOCAL_MOCK_PROJECT_ID = "mock-project-001"
LOCAL_MOCK_SESSION_ID = "mock-session-001"

# 현재 서비스에서 활성화한 분석 의도입니다. 향후 지원 범위를 넓힐 때
# root_cause 또는 data_drift를 이 tuple에 추가하면 됩니다.
ENABLED_ANALYSIS_INTENTS = ("failure_prediction","root_cause", "data_drift")


def build_langgraph_thread_id(session_id: str) -> str:
    """Build the checkpoint thread key for one conversation session."""
    normalized_session_id = session_id.strip()
    if not normalized_session_id:
        raise ValueError("session_id is required")
    return normalized_session_id


def build_local_mock_request_context(
    *,
    session_id: str = LOCAL_MOCK_SESSION_ID,
) -> dict[str, str]:
    """Return request-scoped IDs for local/manual graph tests only."""
    return {
        "user_id": LOCAL_MOCK_USER_ID,
        "project_id": LOCAL_MOCK_PROJECT_ID,
        "session_id": session_id,
        "request_id": f"mock-request-{uuid4()}",
        "thread_id": build_langgraph_thread_id(session_id),
    }


def _load_dotenv(path: Path) -> None:
    """Load a minimal KEY=VALUE dotenv file without another dependency."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            os.environ.setdefault(key, value)


def _find_key(value: Any, key: str) -> Any | None:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_key(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, key)
            if found is not None:
                return found
    return None


def _load_phoenix_endpoint(path: Path) -> str | None:
    if not path.is_file():
        return None
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    endpoint = _find_key(document, "PHOENIX_ENDPOINT")
    return str(endpoint) if endpoint else None


def _load_phoenix_api_key(path: Path) -> str | None:
    if not path.is_file():
        return None
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    api_key = _find_key(document, "PHOENIX_API_KEY")
    return str(api_key) if api_key else None


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_optional_bool(value: str | None) -> bool | None:
    """Parse an optional boolean whose absence means do not send an option."""
    if value is None or not value.strip():
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


# Executor's tenant_id rule; dlq and work are reserved Stream suffixes.
_TENANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")


def require_executor_tenant(settings: AgentSettings) -> str:
    """The API and the Worker cannot run without EXECUTOR_TENANT_ID."""
    if settings.executor_tenant_id is None:
        raise RuntimeError("EXECUTOR_TENANT_ID is required")
    return settings.executor_tenant_id


@dataclass(frozen=True)
class AgentSettings:
    environment: str
    model_provider: str
    model_name: str
    model_api_key: str | None
    api_base_url: str | None
    model_temperature: float
    model_timeout_seconds: float
    model_max_retries: int
    model_enable_thinking: bool | None
    azure_openai_api_key: str | None
    azure_openai_endpoint: str | None
    azure_openai_api_version: str
    azure_openai_deployment: str | None
    checkpoint_db_uri: str
    checkpoint_setup_on_start: bool
    checkpoint_pool_min_size: int
    checkpoint_pool_max_size: int
    checkpoint_pool_timeout_seconds: float
    strict_checkpoint_msgpack: bool
    executor_base_url: str
    executor_tls_verify: bool
    executor_runtime_profile: str
    executor_executions_path: str
    executor_operations_path: str
    executor_execution_path: str
    executor_result_path: str
    executor_notebook_path: str
    executor_finalize_path: str
    executor_cancel_path: str
    executor_artifacts_path: str
    executor_shared_input_root: Path
    executor_result_read_mode: str
    executor_shared_result_root: Path
    data_mock: bool
    executor_source_type: str
    executor_report_source_type: str
    executor_report_append_to_notebook: bool
    executor_timeout_seconds: float
    executor_operation_timeout_seconds: int
    executor_operation_wait_timeout_seconds: int
    executor_submit_enabled: bool
    # false면 제출 성공·실패와 관계없이 실행 결과 대기로 들어가지 않는다.
    # executor_wait_enabled: bool # 삭제후보(테스트용)
    demo_artifacts_enabled: bool
    demo_artifacts_root: Path
    phoenix_endpoint: str | None
    phoenix_project_name: str
    phoenix_api_key: str | None
    max_workflow_revisions: int
    workflow_recommendation_enabled: bool
    workflow_similarity_score: float
    model_structured_output_mode: str
    embedding_model_name: str = "bge-m3"
    embedding_dimension: int = 1024
    embedding_provider: str = "openai_compatible"
    embedding_api_base_url: str | None = None
    workflow_recommendation_limit: int = 3
    workflow_generation_limit: int = 2
    main_model: dict[str, Any] | None = None
    context_input_budget_tokens: int = 32000
    context_summary_trigger_tokens: int = 8000
    # This service's Executor tenant (EXECUTOR_TENANT_ID): the tenant_id of
    # its submissions and of the batches it registers in pr-batch; the
    # Worker reads "{EW_EXECUTOR_EVENT_STREAM}.{tenant}". Required by the API
    # and the Worker (require_executor_tenant).
    executor_tenant_id: str | None = None

    def __post_init__(self):
        tenant = self.executor_tenant_id
        if tenant is not None and (
            not _TENANT_ID.fullmatch(tenant) or tenant in {"dlq", "work"}
        ):
            raise ValueError(f"EXECUTOR_TENANT_ID is invalid or reserved: {tenant!r}")
        if type(self.context_input_budget_tokens) is not int or self.context_input_budget_tokens < 1024:
            raise ValueError("CONTEXT_INPUT_BUDGET_TOKENS must be an integer >= 1024")
        if (type(self.context_summary_trigger_tokens) is not int
                or not 128 <= self.context_summary_trigger_tokens < self.context_input_budget_tokens):
            raise ValueError("CONTEXT_SUMMARY_TRIGGER_TOKENS must be >= 128 and below CONTEXT_INPUT_BUDGET_TOKENS")

    @property
    def executor_executions_url(self) -> str:
        return self._executor_url(self.executor_executions_path)

    @property
    def executor_operations_url(self) -> str:
        return self._executor_url(self.executor_operations_path)

    @property
    def executor_execution_url(self) -> str:
        return self._executor_url(self.executor_execution_path)

    @property
    def executor_result_url(self) -> str:
        return self._executor_url(self.executor_result_path)

    @property
    def executor_notebook_url(self) -> str:
        return self._executor_url(self.executor_notebook_path)

    @property
    def executor_finalize_url(self) -> str:
        return self._executor_url(self.executor_finalize_path)

    @property
    def executor_cancel_url(self) -> str:
        return self._executor_url(self.executor_cancel_path)

    @property
    def executor_artifacts_url(self) -> str:
        return self._executor_url(self.executor_artifacts_path)

    def _executor_url(self, path: str) -> str:
        return (
            f"{self.executor_base_url.rstrip('/')}/"
            f"{path.lstrip('/')}"
        )


def load_agent_settings(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: Path | None = None,
) -> AgentSettings:
    """Load local/internal-network/Azure settings from one place."""
    _load_dotenv(dotenv_path or PROJECT_ROOT / ".env")
    env = dict(os.environ if environ is None else environ)

    embedding_provider = env.get("EMBEDDING_PROVIDER", "openai_compatible").strip().lower()
    if embedding_provider not in {"openai_compatible", "mock"}:
        raise ValueError("EMBEDDING_PROVIDER must be openai_compatible or mock")
    embedding_model_name = env.get("EMBEDDING_MODEL_NAME", "bge-m3").strip()
    if not embedding_model_name:
        raise ValueError("EMBEDDING_MODEL_NAME must not be empty")
    try:
        embedding_dimension = int(env.get("EMBEDDING_DIMENSION", "1024"))
    except (TypeError, ValueError):
        raise ValueError("EMBEDDING_DIMENSION must be a positive integer") from None
    if embedding_dimension <= 0:
        raise ValueError("EMBEDDING_DIMENSION must be a positive integer")
    workflow_similarity_score = float(env.get("WORKFLOW_SIMILARITY_SCORE", "0.8"))
    if not isfinite(workflow_similarity_score) or not -1 <= workflow_similarity_score <= 1:
        raise ValueError("WORKFLOW_SIMILARITY_SCORE must be finite and in -1..1")

    try:
        workflow_recommendation_limit = int(env.get("WORKFLOW_RECOMMENDATION_LIMIT", "3"))
    except (TypeError, ValueError):
        raise ValueError("WORKFLOW_RECOMMENDATION_LIMIT must be an integer in 1..20") from None
    if not 1 <= workflow_recommendation_limit <= 20:
        raise ValueError("WORKFLOW_RECOMMENDATION_LIMIT must be an integer in 1..20")

    try:
        workflow_generation_limit = int(env.get("WORKFLOW_GENERATION_LIMIT", "2"))
    except (TypeError, ValueError):
        raise ValueError("WORKFLOW_GENERATION_LIMIT must be an integer in 1..20") from None
    if not 1 <= workflow_generation_limit <= 20:
        raise ValueError("WORKFLOW_GENERATION_LIMIT must be an integer in 1..20")


    pool_min = int(env.get("CHECKPOINT_POOL_MIN_SIZE", "1"))
    pool_max = int(env.get("CHECKPOINT_POOL_MAX_SIZE", "4"))
    pool_timeout = float(env.get("CHECKPOINT_POOL_TIMEOUT_SECONDS", "10"))
    if not 1 <= pool_min <= pool_max:
        raise ValueError("Checkpoint pool requires 1 <= min_size <= max_size")
    if not 0 < pool_timeout < float("inf"):
        raise ValueError("Checkpoint pool timeout must be finite and positive")

    config_path = Path(
        env.get("PHOENIX_CONFIG_PATH", str(PROJECT_ROOT / "config.dev.yml"))
    )
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    phoenix_endpoint = env.get("PHOENIX_ENDPOINT") or _load_phoenix_endpoint(
        config_path
    )
    phoenix_api_key = env.get("PHOENIX_API_KEY") or _load_phoenix_api_key(
        config_path
    )
    azure_endpoint = env.get("AZURE_OPENAI_ENDPOINT")
    provider = env.get("MODEL_PROVIDER") or (
        "azure_openai" if azure_endpoint else "openai_compatible"
    )

    demo_artifacts_root = Path(
        env.get("DEMO_ARTIFACTS_ROOT", str(PROJECT_ROOT / "demo_artifacts"))
    )
    if not demo_artifacts_root.is_absolute():
        demo_artifacts_root = PROJECT_ROOT / demo_artifacts_root
    model_structured_output_mode = env.get(
        "MODEL_STRUCTURED_OUTPUT_MODE", "prompt_json"
    ).strip()
    if model_structured_output_mode not in {
        "prompt_json",
        "provider_json_schema",
    }:
        raise ValueError(
            "MODEL_STRUCTURED_OUTPUT_MODE must be 'prompt_json' or "
            "'provider_json_schema'"
        )

    executor_source_type = env.get("EXECUTOR_SOURCE_TYPE", "PATH").strip().upper()
    if executor_source_type not in {"PATH", "INLINE"}:
        raise ValueError("EXECUTOR_SOURCE_TYPE must be 'PATH' or 'INLINE'")
    executor_report_source_type = env.get(
        "EXECUTOR_REPORT_SOURCE_TYPE", "INLINE"
    ).strip().upper()
    if executor_report_source_type not in {"PATH", "INLINE"}:
        raise ValueError(
            "EXECUTOR_REPORT_SOURCE_TYPE must be 'PATH' or 'INLINE'"
        )
    executor_result_read_mode = env.get(
        "EXECUTOR_RESULT_READ_MODE", "API"
    ).strip().upper()
    if executor_result_read_mode not in {"API", "MANIFEST"}:
        raise ValueError(
            "EXECUTOR_RESULT_READ_MODE must be 'API' or 'MANIFEST'"
        )

    executor_operation_timeout_seconds = int(
        env.get("EXECUTOR_OPERATION_TIMEOUT_SECONDS", "600")
    )
    executor_operation_wait_timeout_seconds = int(
        env.get("EXECUTOR_OPERATION_WAIT_TIMEOUT_SECONDS", "600")
    )
    if executor_operation_timeout_seconds <= 0:
        raise ValueError("EXECUTOR_OPERATION_TIMEOUT_SECONDS must be greater than 0")
    if executor_operation_wait_timeout_seconds < 30:
        raise ValueError(
            "EXECUTOR_OPERATION_WAIT_TIMEOUT_SECONDS must be at least 30"
        )

    model_config = {
        "name": env.get("LLM_MODEL_NAME", "").strip() or USER_LLM_MODEL,
        "parameters": deepcopy(MAIN_MODEL_CONFIG["parameters"]),
    }
    main_model = resolve_main_model(model_config)
    return AgentSettings(
        main_model=main_model,
        environment=env.get("APP_ENV", "development"),
        model_provider=provider,
        model_name=MAIN_MODEL_PRESETS.get(main_model["name"], {}).get("model", main_model["name"]),
        model_api_key=env.get("MODEL_API_KEY"),
        api_base_url=env.get("API_BASE_URL"),
        embedding_model_name=embedding_model_name,
        embedding_dimension=embedding_dimension,
        embedding_provider=embedding_provider,
        embedding_api_base_url=env.get("EMBEDDING_API_BASE_URL") or None,
        context_input_budget_tokens=int(env.get("CONTEXT_INPUT_BUDGET_TOKENS", "32000")),
        context_summary_trigger_tokens=int(env.get("CONTEXT_SUMMARY_TRIGGER_TOKENS", "8000")),
        model_temperature=main_model["parameters"].get("temperature", 1.0),
        model_timeout_seconds=float(env.get("MODEL_TIMEOUT_SECONDS", "60")),
        model_max_retries=int(env.get("MODEL_MAX_RETRIES", "2")),
        model_enable_thinking=main_model["parameters"].get("enable_thinking"),
        azure_openai_api_key=env.get("AZURE_OPENAI_API_KEY"),
        azure_openai_endpoint=azure_endpoint,
        azure_openai_api_version=env.get(
            "AZURE_OPENAI_API_VERSION", "2024-10-21"
        ),
        azure_openai_deployment=env.get("AZURE_OPENAI_DEPLOYMENT"),
        checkpoint_db_uri=env.get(
            "CHECKPOINT_DB_URI",
            "postgresql://postgres:postgres@localhost:5432/dtest_agent?sslmode=disable",
        ),
        checkpoint_setup_on_start=_as_bool(
            env.get("CHECKPOINT_SETUP_ON_START"), True
        ),
        checkpoint_pool_min_size=pool_min,
        checkpoint_pool_max_size=pool_max,
        checkpoint_pool_timeout_seconds=pool_timeout,
        strict_checkpoint_msgpack=_as_bool(
            env.get("LANGGRAPH_STRICT_MSGPACK"), True
        ),
        executor_base_url=env.get("EXECUTOR_BASE_URL", "http://executor:8080"),
        executor_tls_verify=_as_bool(env.get("EXECUTOR_TLS_VERIFY"), True),
        executor_runtime_profile=(
            env.get("EXECUTOR_RUNTIME_PROFILE", "ml").strip()
        ),
        executor_executions_path=env.get(
            "EXECUTOR_EXECUTIONS_PATH",
            env.get("EXECUTOR_JOBS_PATH", "/api/v1/executions"),
        ),
        executor_operations_path=env.get(
            "EXECUTOR_OPERATIONS_PATH",
            "/api/v1/executions/{execution_id}/operations",
        ),
        executor_execution_path=env.get(
            "EXECUTOR_EXECUTION_PATH",
            "/api/v1/executions/{execution_id}",
        ),
        executor_result_path=env.get(
            "EXECUTOR_RESULT_PATH",
            "/api/v1/executions/{execution_id}/result",
        ),
        executor_notebook_path=env.get(
            "EXECUTOR_NOTEBOOK_PATH",
            "/api/v1/executions/{execution_id}/notebook",
        ),
        executor_finalize_path=env.get(
            "EXECUTOR_FINALIZE_PATH",
            "/api/v1/executions/{execution_id}/finalize",
        ),
        executor_cancel_path=env.get(
            "EXECUTOR_CANCEL_PATH",
            "/api/v1/executions/{execution_id}/cancel",
        ),
        executor_artifacts_path=env.get(
            "EXECUTOR_ARTIFACTS_PATH",
            "/api/v1/executions/{execution_id}/artifacts",
        ),
        executor_shared_input_root=Path(
            env.get(
                "EXECUTOR_SHARED_INPUT_ROOT",
                str(DEFAULT_EXECUTOR_SHARED_INPUT_ROOT),
            )
        ),
        executor_result_read_mode=executor_result_read_mode,
        executor_shared_result_root=Path(
            env.get("EXECUTOR_SHARED_RESULT_ROOT", "/workspace/pv")
        ),
        data_mock=_as_bool(env.get("DATA_MOCK"), False),
        executor_source_type=executor_source_type,
        executor_report_source_type=executor_report_source_type,
        executor_report_append_to_notebook=_as_bool(
            env.get("EXECUTOR_REPORT_APPEND_TO_NOTEBOOK"), True
        ),
        executor_timeout_seconds=float(env.get("EXECUTOR_TIMEOUT_SECONDS", "30")),
        executor_operation_timeout_seconds=executor_operation_timeout_seconds,
        executor_operation_wait_timeout_seconds=(
            executor_operation_wait_timeout_seconds
        ),
        executor_submit_enabled=_as_bool(
            env.get("EXECUTOR_SUBMIT_ENABLED"), True
        ),
        # executor_wait_enabled=_as_bool(
        #     env.get("EXECUTOR_WAIT_ENABLED"), True
        # ), # 삭제후보(테스트용)
        demo_artifacts_enabled=_as_bool(
            env.get("DEMO_ARTIFACTS_ENABLED"), True
        ),
        demo_artifacts_root=demo_artifacts_root,
        phoenix_endpoint=phoenix_endpoint,
        phoenix_project_name=env.get("PHOENIX_PROJECT_NAME", "dtest-agent"),
        phoenix_api_key=phoenix_api_key,
        max_workflow_revisions=int(env.get("MAX_WORKFLOW_REVISIONS", "10")),
        workflow_recommendation_enabled=_as_bool(
            env.get("WORKFLOW_RECOMMENDATION_ENABLED"), True
        ),
        workflow_similarity_score=workflow_similarity_score,
        workflow_recommendation_limit=workflow_recommendation_limit,
        workflow_generation_limit=workflow_generation_limit,
        model_structured_output_mode=model_structured_output_mode,
        executor_tenant_id=env.get("EXECUTOR_TENANT_ID", "").strip() or None,
    )


__all__ = [
    "AgentSettings",
    "DEFAULT_EXECUTOR_SHARED_INPUT_ROOT",
    "ENABLED_ANALYSIS_INTENTS",
    "LOCAL_MOCK_PROJECT_ID",
    "LOCAL_MOCK_SESSION_ID",
    "LOCAL_MOCK_USER_ID",
    "MOCK_DATA_PATH_BY_TYPE",
    "PROJECT_ROOT",
    "build_langgraph_thread_id",
    "build_local_mock_request_context",
    "load_agent_settings",
    "resolve_mock_data_path",
]
