"""OpenAI-compatible embeddings using the existing MODEL_* connection settings."""
from __future__ import annotations

import math
import httpx

from app.services.async_io_resources import executor_http_client


class OpenAICompatibleEmbeddingProvider:
    def __init__(self, *, base_url: str, api_key: str, model: str,
                 timeout_seconds: float = 60, client: httpx.AsyncClient | None = None):
        url = httpx.URL(base_url.strip())
        if url.scheme not in ("http", "https") or not url.host or url.query or url.fragment:
            raise ValueError("API_BASE_URL must be an HTTP(S) API base URL")
        if not model.strip():
            raise ValueError("EMBEDDING_MODEL_NAME is required")
        if not api_key or not api_key.strip():
            raise ValueError("MODEL_API_KEY is required")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("MODEL_TIMEOUT_SECONDS must be positive and finite")
        self.model = model.strip()
        self._endpoint = str(url).rstrip("/") + "/embeddings"
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._client = client

    @classmethod
    def from_environment(cls, *, client: httpx.AsyncClient | None = None):
        from agent_config import load_agent_settings
        return cls.from_settings(load_agent_settings(), client=client)

    @classmethod
    def from_settings(cls, settings, *, client: httpx.AsyncClient | None = None):
        return cls(base_url=settings.embedding_api_base_url or settings.api_base_url or "", api_key=settings.model_api_key or "",
                   model=settings.embedding_model_name,
                   timeout_seconds=settings.model_timeout_seconds, client=client)

    async def embed(self, text: str) -> list[float]:
        client = self._client if self._client is not None else executor_http_client()
        response = await client.post(
            self._endpoint,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self.model, "input": text, "encoding_format": "float"},
            timeout=self._timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
        # Single input => exactly one result; reject missing/ambiguous payloads.
        # Vector values and expected 1024 dimensions are checked by the service.
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid embedding response")
        if "model" in payload and payload["model"] != self.model:
            raise ValueError("Embedding response model differs from requested model")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise ValueError("Invalid embedding response data")
        if data[0].get("index", 0) != 0:
            raise ValueError("Invalid embedding response index")
        vector = data[0].get("embedding")
        if not isinstance(vector, list):
            raise ValueError("Embedding API must return a numeric array")
        return vector



def create_embedding_provider(settings):
    """Select explicitly configured embeddings; never fallback on API errors."""
    if settings.embedding_provider == "mock":
        from app.services.workflow_catalog_service import MockEmbeddingProvider
        return MockEmbeddingProvider(model_name=settings.embedding_model_name,
                                     dimension=settings.embedding_dimension)
    return OpenAICompatibleEmbeddingProvider.from_settings(settings)
