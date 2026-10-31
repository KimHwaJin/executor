"""Executor settings.

Sources, highest first: constructor arguments, environment variables,
``.env`` (local runs only), ``config.{APP_ENV}.yml`` (values for one environment, when
``APP_ENV`` is set; local runs leave it unset or empty), then ``config.yml``
(values shared by every environment, when present), both in the working
directory. ConfigMaps and Secrets are unavailable on the platform, so the
files ship in the image.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from executor_service.domain.tenants import (
    RESERVED_TENANT_IDS,
    TENANT_ID_PATTERN,
)

APP_ENVS = ("dev", "stg", "prd")
# AUTH_MODE=header trusts identity headers without a key.
HEADER_AUTH_ENVS = ("", "dev")

TenantId = Annotated[str, Field(pattern=TENANT_ID_PATTERN)]
KeySha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def config_files() -> list[Path]:
    """YAML settings files, highest precedence first."""
    files = []
    app_env = os.environ.get("APP_ENV")
    if app_env:
        if app_env not in APP_ENVS:
            raise ValueError(f"APP_ENV must be one of {', '.join(APP_ENVS)}.")
        path = Path(f"config.{app_env}.yml")
        if not path.is_file():
            raise FileNotFoundError(
                f"APP_ENV={app_env} needs {path.resolve()}."
            )
        files.append(path)
    common = Path("config.yml")
    if common.is_file():
        files.append(common)
    return files


class AuthClient(BaseModel):
    """One caller allowed to use Executor with its own API key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    # SHA-256 hex of each accepted key; several while a key is rotated.
    key_sha256: tuple[KeySha256, ...] = Field(min_length=1)
    tenant: TenantId | None = None
    # A relay may name another tenant from allowed_tenants in X-Tenant-Id.
    relay: bool = False
    allowed_tenants: tuple[TenantId, ...] = ()
    admin: bool = False

    @model_validator(mode="after")
    def validate_tenants(self) -> Self:
        reserved = RESERVED_TENANT_IDS.intersection(
            (self.tenant, *self.allowed_tenants)
        )
        if reserved:
            raise ValueError(f"tenant {sorted(reserved)} is reserved.")
        if self.allowed_tenants and not self.relay:
            raise ValueError("allowed_tenants requires relay: true.")
        return self


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    log_level: str | None = None
    log_config_file: Path = Path("logger.yml")
    rest_error_body_logging_enabled: bool = False
    rest_error_body_log_max_bytes: int = Field(default=65536, ge=1)
    request_body_max_bytes: int = Field(default=31457280, ge=1024)

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    # Seconds open HTTP requests get to finish on SIGTERM before Uvicorn
    # closes them; Worker drain runs after this inside the Pod grace period.
    http_graceful_shutdown_seconds: int = Field(default=20, ge=0)

    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://executor:executor@localhost:5432/executor"
    )
    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=5, ge=0)
    database_pool_timeout_seconds: float = Field(default=30, gt=0)
    database_pool_recycle_seconds: int = Field(default=1800, ge=1)
    database_connect_timeout_seconds: int = Field(default=10, ge=1)
    database_statement_timeout_seconds: int = Field(default=60, ge=1)
    database_idle_in_transaction_timeout_seconds: int = Field(default=60, ge=1)
    db_auto_migrate: bool = False
    db_migrations_path: Path = Path("migrations")
    db_migration_lock_timeout_seconds: int = Field(default=60, ge=1)
    db_migration_statement_timeout_seconds: int = Field(default=300, ge=1)
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    redis_connect_timeout_seconds: float = Field(default=5, gt=0)
    redis_socket_timeout_seconds: float = Field(default=5, gt=0)
    redis_event_stream: str = Field(default="executor.events", min_length=1)
    # Approximate per-Stream cap on every event XADD. None keeps only the
    # age-based retention below.
    redis_event_stream_maxlen: int | None = Field(default=None, ge=1)
    outbox_poll_interval_seconds: float = Field(default=0.5, gt=0)
    outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    outbox_publish_timeout_seconds: float = Field(default=5, gt=0)
    outbox_shutdown_timeout_seconds: float = Field(default=10, gt=0)
    event_retention_enabled: bool = True
    event_retention_interval_seconds: float = Field(default=600, gt=0)
    event_retention_batch_size: int = Field(default=1000, ge=1, le=10000)
    redis_event_retention_seconds: int = Field(default=604800, ge=3600)
    published_outbox_retention_seconds: int = Field(default=604800, ge=3600)
    execution_event_retention_seconds: int = Field(default=7776000, ge=3600)

    auth_mode: Literal["api_key", "header"] = "header"
    auth_clients: tuple[AuthClient, ...] = ()
    mcp_enabled: bool = True
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
    jupyter_stream_reconnect_seconds: float = Field(default=30, gt=0)
    runtime_enabled: bool = True
    # Base64-encoded 32-byte Fernet key. Replace in every non-local environment.
    runtime_credential_key: SecretStr = SecretStr(
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    runtime_allowed_profiles: Annotated[tuple[str, ...], NoDecode] = (
        "default",
        "3102311",
    )
    runtime_default_max_concurrent_executions: int = Field(default=2, ge=1)
    runtime_health_poll_interval_seconds: float = Field(default=15, gt=0)
    runtime_session_count_max_age_seconds: float = Field(default=45, gt=0)
    runtime_resource_max_age_seconds: float = Field(default=45, gt=0)
    runtime_memory_admission_limit: float = Field(default=0.9, gt=0, le=1)
    shared_storage_root: Path = Path("./shared_dir")
    # Caller-authored PATH inputs may live outside Executor's result root.
    # None keeps the single-root deployment option.
    input_storage_root: Path | None = None
    execution_inline_spec_max_bytes: int = Field(default=262144, ge=1)
    execution_file_spec_max_bytes: int = Field(default=52428800, ge=1)
    execution_max_steps_per_operation: int = Field(default=100, ge=1)
    execution_max_steps_per_execution: int = Field(default=1000, ge=1)
    # Executions one Worker runs at once. Keeps a backlog's claims bounded
    # and spreads work across replicas.
    execution_worker_max_active: int = Field(default=20, ge=1)
    execution_lease_seconds: int = Field(default=60, ge=30)
    execution_heartbeat_seconds: int = Field(default=15, ge=5)
    execution_drain_timeout_seconds: float = Field(default=30, ge=0)
    execution_shutdown_cleanup_seconds: float = Field(default=20, gt=0)
    runtime_abort_timeout_seconds: float = Field(default=15, gt=0)
    runtime_max_output_message_bytes: int = Field(
        default=33554432,
        ge=1048576,
    )
    runtime_cleanup_retry_interval_seconds: float = Field(default=60, gt=0)
    failed_session_retention_seconds: int = Field(default=3600, ge=60)
    execution_max_runtime_seconds: int = Field(default=604800, ge=60)
    execution_queue_timeout_seconds: int = Field(default=86400, ge=60)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        files = [
            YamlConfigSettingsSource(settings_cls, yaml_file=path)
            for path in config_files()
        ]
        # A developer's .env must never leak into an image-built environment.
        dotenv = () if os.environ.get("APP_ENV") else (dotenv_settings,)
        return (
            init_settings,
            env_settings,
            *dotenv,
            *files,
            file_secret_settings,
        )

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper() or None
        return value

    @field_validator("input_storage_root", mode="before")
    @classmethod
    def normalize_input_storage_root(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def effective_input_storage_root(self) -> Path:
        return self.input_storage_root or self.shared_storage_root

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
    def validate_settings(self) -> Self:
        if self.execution_heartbeat_seconds >= self.execution_lease_seconds:
            raise ValueError(
                "EXECUTION_HEARTBEAT_SECONDS must be less than "
                "EXECUTION_LEASE_SECONDS."
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
            self.execution_max_steps_per_operation
            > self.execution_max_steps_per_execution
        ):
            raise ValueError(
                "EXECUTION_MAX_STEPS_PER_OPERATION must not exceed "
                "EXECUTION_MAX_STEPS_PER_EXECUTION."
            )
        if (
            os.environ.get("APP_ENV")
            and self.runtime_credential_encryption_key
            == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
        ):
            raise ValueError(
                "RUNTIME_CREDENTIAL_KEY must be replaced outside local environments."
            )
        if (
            self.auth_mode == "header"
            and os.environ.get("APP_ENV", "") not in HEADER_AUTH_ENVS
        ):
            raise ValueError(
                "AUTH_MODE=header is allowed only locally and in dev."
            )
        names = [client.name for client in self.auth_clients]
        if len(names) != len(set(names)):
            raise ValueError("AUTH_CLIENTS names must be unique.")
        hashes = [
            key for client in self.auth_clients for key in client.key_sha256
        ]
        if len(hashes) != len(set(hashes)):
            raise ValueError("AUTH_CLIENTS key_sha256 values must be unique.")
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
