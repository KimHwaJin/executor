from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from app.models.common.jupyter_server_model import JupyterServerModel
from app.services.helpers import utc_now


@dataclass(frozen=True)
class HealthResult:
    status: str
    http_status: int | None
    latency_ms: int
    error: str | None = None


class JupyterServerService:
    @staticmethod
    def normalize_endpoint(endpoint: str) -> str:
        parsed = urlsplit(endpoint.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(status_code=422, detail="endpoint must be an http(s) URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(status_code=422, detail="endpoint must not contain credentials, query, or fragment.")
        allowed = {item.strip().lower() for item in settings.jupyter_allowed_hosts.split(",") if item.strip()}
        if parsed.hostname.lower() not in allowed:
            raise HTTPException(status_code=422, detail="Jupyter endpoint host is not allowlisted.")
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))

    @staticmethod
    def _fernet() -> Fernet:
        if not settings.jupyter_token_encryption_key:
            raise HTTPException(status_code=503, detail="Jupyter token encryption key is not configured.")
        try:
            return Fernet(settings.jupyter_token_encryption_key.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(status_code=503, detail="Jupyter token encryption key is invalid.") from exc

    @staticmethod
    def encrypt_token(token: str | None) -> str | None:
        return JupyterServerService._fernet().encrypt(token.encode()).decode() if token else None

    @staticmethod
    def decrypt_token(ciphertext: str | None) -> str | None:
        if not ciphertext:
            return None
        try:
            return JupyterServerService._fernet().decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise HTTPException(status_code=503, detail="Stored Jupyter token cannot be decrypted.") from exc

    @staticmethod
    def _probe(endpoint: str, token: str | None) -> HealthResult:
        started = time.monotonic()
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"token {token}"
        request = Request(f"{endpoint}/api/status", headers=headers, method="GET")
        try:
            with urlopen(request, timeout=settings.jupyter_health_timeout_seconds) as response:
                # JSON 파싱까지 성공해야 Jupyter-compatible health로 인정합니다.
                json.loads(response.read().decode("utf-8"))
                latency = int((time.monotonic() - started) * 1000)
                return HealthResult("healthy", response.status, latency)
        except HTTPError as exc:
            latency = int((time.monotonic() - started) * 1000)
            return HealthResult("unhealthy", exc.code, latency, f"Jupyter returned HTTP {exc.code}.")
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            latency = int((time.monotonic() - started) * 1000)
            # Token이나 응답 body를 포함할 수 있는 원문은 DB/API에 저장하지 않습니다.
            return HealthResult("unhealthy", None, latency, type(exc).__name__)

    @staticmethod
    async def check(server: JupyterServerModel) -> HealthResult:
        token = JupyterServerService.decrypt_token(server.token_ciphertext)
        return await asyncio.to_thread(JupyterServerService._probe, server.endpoint, token)

    @staticmethod
    def apply_health(server: JupyterServerModel, result: HealthResult) -> None:
        server.health_status = result.status
        server.last_http_status = result.http_status
        server.last_latency_ms = result.latency_ms
        server.last_error = result.error
        server.last_checked_at = utc_now()

    @staticmethod
    async def get(db: AsyncSession, user_id: UUID, server_id: UUID) -> JupyterServerModel:
        server = await db.scalar(select(JupyterServerModel).where(
            JupyterServerModel.jupyter_server_id == server_id,
            JupyterServerModel.created_by_user_id == user_id,
            JupyterServerModel.deleted_at.is_(None),
        ))
        if server is None:
            raise HTTPException(status_code=404, detail="Jupyter server not found.")
        return server

    @staticmethod
    async def refresh_health(db: AsyncSession, user_id: UUID, server_id: UUID) -> JupyterServerModel:
        server = await JupyterServerService.get(db, user_id, server_id)
        JupyterServerService.apply_health(server, await JupyterServerService.check(server))
        await db.commit()
        await db.refresh(server)
        return server
