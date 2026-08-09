"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    redis_stream: str = "executor.events"
    outbox_poll_interval_seconds: float = Field(default=0.5, gt=0)
    outbox_batch_size: int = Field(default=100, ge=1, le=1000)

    mcp_allowed_hosts: tuple[str, ...] = ("localhost:*", "127.0.0.1:*", "testserver")
    mcp_allowed_origins: tuple[str, ...] = ("http://localhost:*", "http://127.0.0.1:*")
    jupyter_request_timeout_seconds: float = Field(default=30, gt=0)
    jupyter_enabled: bool = True
    jupyter_server_name: str = "local-jupyter"
    jupyter_endpoint: str = "http://127.0.0.1:8888"
    jupyter_token: SecretStr = SecretStr("change-me-local-only")
    # Base64-encoded 32-byte Fernet key. Replace in every non-local environment.
    jupyter_credential_key: SecretStr = SecretStr(
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    jupyter_pool: str = "INTERACTIVE"
    jupyter_max_concurrent_executions: int = Field(default=2, ge=1)
    jupyter_health_poll_interval_seconds: float = Field(default=15, gt=0)
    workspace_host_root: Path = Path("/Users/kimhwajin/vscode/AX_PROJECT/executor/notebook_dir")
    workspace_jupyter_root: str = "/workspace/pv"
    execution_consumer_group: str = "executor-workers"
    execution_consumer_name: str = ""
    execution_worker_concurrency: int = Field(default=2, ge=1)
    execution_lease_seconds: int = Field(default=60, ge=30)
    execution_heartbeat_seconds: int = Field(default=15, ge=5)
    failed_kernel_retention_seconds: int = Field(default=3600, ge=60)

    @field_validator("mcp_allowed_hosts", "mcp_allowed_origins", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @property
    def database_dsn(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def redis_dsn(self) -> str:
        return self.redis_url.get_secret_value()

    @property
    def jupyter_auth_token(self) -> str:
        return self.jupyter_token.get_secret_value()

    @property
    def jupyter_credential_encryption_key(self) -> str:
        return self.jupyter_credential_key.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
