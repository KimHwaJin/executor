"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "local"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)

    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://executor:executor@localhost:5432/executor"
    )
    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=5, ge=0)
    database_pool_timeout_seconds: float = Field(default=30, gt=0)
    database_pool_recycle_seconds: int = Field(default=1800, ge=1)
    database_connect_timeout_seconds: int = Field(default=10, ge=1)
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    redis_work_stream: str = Field(default="executor.work", min_length=1)
    redis_event_stream: str = Field(default="executor.events", min_length=1)
    redis_work_dead_letter_stream: str = Field(
        default="executor.work.dlq", min_length=1
    )
    redis_event_dead_letter_stream: str = Field(
        default="executor.events.dlq", min_length=1
    )
    outbox_poll_interval_seconds: float = Field(default=0.5, gt=0)
    outbox_batch_size: int = Field(default=100, ge=1, le=1000)

    mcp_allowed_hosts: Annotated[tuple[str, ...], NoDecode] = (
        "localhost:*",
        "127.0.0.1:*",
        "testserver",
    )
    mcp_allowed_origins: Annotated[tuple[str, ...], NoDecode] = (
        "http://localhost:*",
        "http://127.0.0.1:*",
    )
    jupyter_request_timeout_seconds: float = Field(default=30, gt=0)
    jupyter_storage_timeout_seconds: float = Field(default=300, gt=0)
    runtime_enabled: bool = True
    # Base64-encoded 32-byte Fernet key. Replace in every non-local environment.
    runtime_credential_key: SecretStr = SecretStr(
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    runtime_allowed_profiles: Annotated[tuple[str, ...], NoDecode] = (
        "basic",
        "ml",
    )
    runtime_default_max_concurrent_executions: int = Field(default=2, ge=1)
    runtime_health_poll_interval_seconds: float = Field(default=15, gt=0)
    runtime_resource_max_age_seconds: float = Field(default=45, gt=0)
    runtime_memory_admission_limit: float = Field(default=0.9, gt=0, le=1)
    input_host_root: Path = Path("./input_dir")
    execution_inline_spec_max_bytes: int = Field(default=262144, ge=1)
    execution_file_spec_max_bytes: int = Field(default=52428800, ge=1)
    execution_consumer_group: str = "executor-workers"
    execution_consumer_name: str = ""
    execution_pending_claim_interval_seconds: float = Field(default=5, gt=0)
    execution_pending_claim_idle_milliseconds: int = Field(default=30000, ge=1)
    execution_pending_claim_batch_size: int = Field(default=100, ge=1, le=1000)
    execution_lease_seconds: int = Field(default=60, ge=30)
    execution_heartbeat_seconds: int = Field(default=15, ge=5)
    execution_drain_timeout_seconds: float = Field(default=30, ge=0)
    execution_shutdown_cleanup_seconds: float = Field(default=20, gt=0)
    runtime_abort_timeout_seconds: float = Field(default=15, gt=0)
    failed_session_retention_seconds: int = Field(default=3600, ge=60)
    execution_max_runtime_seconds: int = Field(default=432000, ge=60)
    tracing_enabled: bool = False
    otel_service_name: str = "executor-service"
    otel_project_name: str = "executor-service"
    otel_exporter_otlp_endpoint: str = "http://127.0.0.1:6006/v1/traces"
    otel_exporter_otlp_headers: SecretStr = SecretStr("")
    otel_exporter_timeout_seconds: float = Field(default=5, gt=0)
    otel_sample_ratio: float = Field(default=1.0, ge=0, le=1)

    @field_validator(
        "mcp_allowed_hosts",
        "mcp_allowed_origins",
        "runtime_allowed_profiles",
        mode="before",
    )
    @classmethod
    def parse_allowed_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(
                item.strip() for item in value.split(",") if item.strip()
            )
        return value

    @model_validator(mode="after")
    def validate_redis_streams(self) -> Self:
        stream_names = {
            self.redis_work_stream,
            self.redis_event_stream,
            self.redis_work_dead_letter_stream,
            self.redis_event_dead_letter_stream,
        }
        if len(stream_names) != 4:
            raise ValueError(
                "Redis work, event, and dead-letter Stream names must be distinct."
            )
        if not self.runtime_allowed_profiles:
            raise ValueError(
                "RUNTIME_ALLOWED_PROFILES must contain at least one profile."
            )
        if len(self.runtime_allowed_profiles) != len(
            set(self.runtime_allowed_profiles)
        ):
            raise ValueError(
                "RUNTIME_ALLOWED_PROFILES must not contain duplicates."
            )
        if (
            self.app_env.lower() != "local"
            and self.runtime_credential_encryption_key
            == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        ):
            raise ValueError(
                "RUNTIME_CREDENTIAL_KEY must be replaced outside local environments."
            )
        return self

    @property
    def database_dsn(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def redis_dsn(self) -> str:
        return self.redis_url.get_secret_value()

    @property
    def runtime_credential_encryption_key(self) -> str:
        return self.runtime_credential_key.get_secret_value()

    @property
    def otel_export_headers(self) -> dict[str, str]:
        raw = self.otel_exporter_otlp_headers.get_secret_value()
        headers: dict[str, str] = {}
        for item in raw.split(","):
            key, separator, value = item.partition("=")
            if separator and key.strip():
                headers[key.strip()] = value.strip()
        return headers


@lru_cache
def get_settings() -> Settings:
    return Settings()
