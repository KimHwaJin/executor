"""Exercise real HTTP request/parsing code with a 1024-array mock response."""
import json

import httpx
import pytest

from app.services.workflow_catalog_service import (
    WorkflowCatalogService, WorkflowEmbeddingError, WorkflowEmbeddingTimeoutError,
)
from app.services.workflow_embedding_provider import OpenAICompatibleEmbeddingProvider


def provider(client):
    return OpenAICompatibleEmbeddingProvider(base_url="https://model.test/v1/",
                                             api_key="test-key", model="bge-m3", client=client)


@pytest.mark.asyncio
async def test_embedding_http_payload_and_1024_response():
    def transport(request):
        assert str(request.url) == "https://model.test/v1/embeddings"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {"model":"bge-m3", "input":"search text", "encoding_format":"float"}
        return httpx.Response(200, json={"model":"bge-m3", "data":[{"index":0,"embedding":[0.5]*1024}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service = WorkflowCatalogService("postgresql://unused", provider(client))
        vector, dim = await service._embed("search text")
        assert dim == 1024 and json.loads(vector) == [0.5]*1024


@pytest.mark.asyncio
async def test_environment_uses_existing_connection_and_separate_model(monkeypatch):
    values = {"EMBEDDING_API_BASE_URL":"", "EMBEDDING_PROVIDER":"openai_compatible", "MODEL_PROVIDER":"openai_compatible", "LLM_MODEL_NAME":"gpt-oss-120b",
              "MODEL_API_KEY":"test-key", "API_BASE_URL":"https://model.test/v1",
              "EMBEDDING_MODEL_NAME":"bge-m3", "MODEL_TIMEOUT_SECONDS":"7",
              "EW_DATABASE_URL":"postgresql://unused"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
    def transport(request):
        assert json.loads(request.content)["model"] == "bge-m3"
        assert request.extensions["timeout"]["read"] == 7
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"data":[{"embedding":[1.0]*1024}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        p = OpenAICompatibleEmbeddingProvider.from_environment(client=client)
        service = WorkflowCatalogService.from_environment(p)
        assert service.database_url == "postgresql://unused"
        assert service.embedding_provider.model == "bge-m3"
        assert len(await p.embed("query")) == 1024
        # Default factory selects the real provider, without making a request.
        assert isinstance(WorkflowCatalogService.from_environment().embedding_provider, OpenAICompatibleEmbeddingProvider)


def test_factory_reads_agent_dotenv(monkeypatch, tmp_path):
    keys = ("EMBEDDING_API_BASE_URL", "EMBEDDING_PROVIDER", "WORKFLOW_DATABASE_URL", "EW_DATABASE_URL", "MODEL_PROVIDER", "LLM_PROVIDER",
            "MODEL_API_KEY", "LLM_API_KEY", "API_BASE_URL", "LLM_API_BASE_URL",
            "EMBEDDING_MODEL_NAME", "EMBEDDING_DIMENSION", "MODEL_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS")
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("agent_config.PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "EW_DATABASE_URL=postgresql://unused\n"
        "EMBEDDING_PROVIDER=openai_compatible\nMODEL_PROVIDER=openai_compatible\nMODEL_API_KEY=test-key\n"
        "API_BASE_URL=https://model.test/v1\nEMBEDDING_MODEL_NAME=bge-m3\nEMBEDDING_DIMENSION=4096\n")
    service = WorkflowCatalogService.from_environment()
    assert service.database_url == "postgresql://unused"
    assert service.embedding_provider.model == "bge-m3"
    assert service.embedding_dimension == 4096
    # The existing Agent loader populates os.environ using setdefault.
    assert __import__('os').environ["MODEL_API_KEY"] == "test-key"


@pytest.mark.asyncio
async def test_configured_dimension_accepts_model_output_and_rejects_mismatch(monkeypatch):
    monkeypatch.setenv("EW_DATABASE_URL", "postgresql://unused")
    monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
    monkeypatch.setenv("EMBEDDING_DIMENSION", "4096")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data":[{"embedding":[0.5]*4096}]}))) as client:
        service = WorkflowCatalogService.from_environment(provider(client))
        assert service.embedding_dimension == 4096
        assert (await service._embed("query"))[1] == 4096
        monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
        service = WorkflowCatalogService.from_environment(provider(client))
        with pytest.raises(WorkflowEmbeddingError):
            await service._embed("query")


@pytest.mark.parametrize("dimension", ["0", "-1", "abc", "1.5"])
def test_invalid_dimension_environment(monkeypatch, dimension):
    monkeypatch.setenv("EMBEDDING_DIMENSION", dimension)
    with pytest.raises(ValueError):
        WorkflowCatalogService.from_environment()


def test_agent_settings_embedding_defaults_and_explicit_values():
    from agent_config import load_agent_settings
    defaults = load_agent_settings({})
    assert defaults.embedding_model_name == "bge-m3"
    assert defaults.embedding_dimension == 1024
    settings = load_agent_settings({"LLM_MODEL_NAME":"gpt-oss-120b",
                                    "EMBEDDING_MODEL_NAME":"embedding-test",
                                    "EMBEDDING_DIMENSION":"4096"})
    assert settings.model_name == "gpt-oss-120b"
    assert settings.embedding_model_name == "embedding-test"
    assert settings.embedding_dimension == 4096
    with pytest.raises(ValueError, match="EMBEDDING_MODEL_NAME"):
        load_agent_settings({"EMBEDDING_MODEL_NAME":" "})


@pytest.mark.parametrize("payload", [
    {}, {"data":[]}, {"data":[{},{}]}, {"data":[{"embedding":"base64"}]},
    {"data":[{"embedding":[1.0]*3}]}, {"data":[{"embedding":[0.0]*1024}]},
    {"data":[{"embedding":[True]*1024}]}, {"data":[{"index":1,"embedding":[1.0]*1024}]},
    {"model":"wrong-model", "data":[{"embedding":[1.0]*1024}]},
])
@pytest.mark.asyncio
async def test_invalid_http_response_never_reaches_db(monkeypatch, payload):
    async def no_db(*args):
        raise AssertionError("DB must not be opened on invalid embeddings")
    monkeypatch.setattr("app.services.workflow_catalog_service.workflow_connection_pool", no_db)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        service = WorkflowCatalogService("postgresql://unused", provider(client))
        with pytest.raises(WorkflowEmbeddingError):
            await service.search("query")


@pytest.mark.parametrize("failure", ["status", "timeout", "connection", "json", "redirect"])
@pytest.mark.asyncio
async def test_http_failure_is_sanitized_and_no_mock_fallback(monkeypatch, failure):
    async def no_db(*args):
        raise AssertionError("DB must not be opened after HTTP failure")
    monkeypatch.setattr("app.services.workflow_catalog_service.workflow_connection_pool", no_db)
    calls = []
    def transport(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private details", request=request)
        if failure == "connection":
            raise httpx.ConnectError("private details", request=request)
        if failure == "json":
            return httpx.Response(200, text="private invalid response")
        if failure == "redirect":
            return httpx.Response(302, headers={"Location":"https://other.test"})
        return httpx.Response(500, json={"error":"private internal message"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        service = WorkflowCatalogService("postgresql://unused", provider(client))
        expected = WorkflowEmbeddingTimeoutError if failure == "timeout" else WorkflowEmbeddingError
        with pytest.raises(expected) as error:
            await service.search("query")
        assert "private" not in str(error.value) and "test-key" not in str(error.value)
        assert len(calls) == 1


@pytest.mark.parametrize("kwargs", [
    {"base_url":"file:///tmp"}, {"base_url":"https://model.test/v1?token=invalid"},
    {"api_key":""}, {"model":" "}, {"timeout_seconds":0},
])
def test_invalid_configuration(kwargs):
    settings = dict(base_url="https://model.test/v1", api_key="test-key", model="bge-m3")
    settings.update(kwargs)
    with pytest.raises(ValueError):
        OpenAICompatibleEmbeddingProvider(**settings)


@pytest.mark.parametrize("dimension", [1024, 4096])
@pytest.mark.asyncio
async def test_explicit_mock_provider_uses_configured_dimension_without_http(dimension):
    from agent_config import load_agent_settings
    from app.services.workflow_embedding_provider import create_embedding_provider
    from app.services.workflow_catalog_service import MockEmbeddingProvider
    settings = load_agent_settings({"EMBEDDING_PROVIDER": "mock", "EMBEDDING_DIMENSION": str(dimension)})
    mock = create_embedding_provider(settings)
    assert isinstance(mock, MockEmbeddingProvider)
    assert mock.model == f"mock/bge-m3/{dimension}/v1"
    values = await mock.embed("query")
    assert len(values) == dimension and values == await mock.embed("query")
    assert mock.model != settings.embedding_model_name
    service = WorkflowCatalogService("postgresql://unused", mock, embedding_dimension=dimension)
    assert (await service._embed("query"))[1] == dimension


def test_invalid_embedding_provider():
    from agent_config import load_agent_settings
    with pytest.raises(ValueError, match="EMBEDDING_PROVIDER"):
        load_agent_settings({"EMBEDDING_PROVIDER": "automatic"})


@pytest.mark.asyncio
@pytest.mark.parametrize("model_provider", ["openai_compatible", "azure_openai"])
@pytest.mark.parametrize("dedicated_endpoint", [False, True])
async def test_embedding_connection_is_independent_of_llm_provider(model_provider, dedicated_endpoint):
    from agent_config import load_agent_settings
    from app.services.workflow_embedding_provider import create_embedding_provider
    settings = load_agent_settings({"MODEL_PROVIDER": model_provider, "MODEL_API_KEY": "test-key",
                                    "API_BASE_URL": "https://llm.test/v1",
                                    "AZURE_OPENAI_ENDPOINT": "https://azure.test/",
                                    "AZURE_OPENAI_API_KEY": "azure-key",
                                    "AZURE_OPENAI_DEPLOYMENT": "gpt-4.1",
                                    "EMBEDDING_API_BASE_URL": "http://127.0.0.1:8091/v1" if dedicated_endpoint else "",
                                    "EMBEDDING_MODEL_NAME": "BAAI/bge-m3"})
    expected_base = "http://127.0.0.1:8091/v1" if dedicated_endpoint else settings.api_base_url
    def transport(request):
        assert str(request.url) == f"{expected_base}/embeddings"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content)["model"] == "BAAI/bge-m3"
        return httpx.Response(200, json={"model": "BAAI/bge-m3", "data": [{"embedding": [0.5]*1024}]})
    assert isinstance(create_embedding_provider(settings), OpenAICompatibleEmbeddingProvider)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        embedding = OpenAICompatibleEmbeddingProvider.from_settings(settings, client=client)
        assert len(await embedding.embed("query")) == 1024
    assert settings.api_base_url == "https://llm.test/v1"
