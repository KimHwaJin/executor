# Executor Test Agent

An isolated LangGraph/LangChain development application for exercising the Executor through a real
Agent Server. It has its own Python environment and lock file so Agent dependencies do not alter
the Executor service dependency graph.

The initial graph is intentionally small. It proves that `langgraph dev`, the Agent Server API,
LangGraph state, and LangChain messages work before Executor MCP, Redis event consumption, and
DYNAMIC continuation are added. When `TEST_AGENT_LLM_MODEL` is configured, the response node uses
the OpenAI-compatible vLLM gateway through LangChain. Without a model, it returns a deterministic
bootstrap response and makes no external LLM call.

## Layout

```text
test_agent/
├── langgraph.json
├── pyproject.toml
├── uv.lock
├── src/executor_test_agent/
│   ├── config.py
│   ├── graph.py
│   └── state.py
├── scripts/smoke.py
└── tests/test_graph.py
```

Executor integration code will live under `src/executor_test_agent/integrations/` in the next
stage. The Agent must use Executor's public MCP/REST contracts and its own Redis consumer group; it
must not import Executor internals or access Executor database tables.

## Setup

From the repository root:

```bash
cd test_agent
cp .env.example .env
uv sync
```

For deterministic startup testing, leave `TEST_AGENT_LLM_MODEL` empty. To use the internal vLLM
OpenAI-compatible API, set these values in `test_agent/.env`:

```dotenv
TEST_AGENT_LLM_BASE_URL=http://your-vllm-gateway/v1
TEST_AGENT_LLM_MODEL=your-model-name
TEST_AGENT_LLM_API_KEY=your-runtime-secret
```

The API key is read only from the environment. Do not commit `.env` or real credentials.

## Run the Agent Server

```bash
cd test_agent
uv run langgraph dev --no-browser
```

Default endpoints:

- Agent Server: `http://127.0.0.1:2024`
- API documentation: `http://127.0.0.1:2024/docs`
- Graph ID: `executor_test_agent`

In another terminal, invoke the running graph:

```bash
cd test_agent
uv run python scripts/smoke.py
```

The smoke client creates a thread, starts a run, and prints streamed events. Override
`TEST_AGENT_SERVER_URL` when the development server uses a different address.

## Verify locally

```bash
cd test_agent
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest -q
```

`langgraph dev` is a development server. Its local state and behavior must not be treated as the
production persistence or deployment design.
