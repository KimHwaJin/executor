# Executor Integration Test Agent

An isolated LangGraph/LangChain development application for exercising the Executor through a real
Agent Server. It has its own Python environment and lock file so Agent dependencies do not alter
the Executor service dependency graph.

The graph supports a deterministic Agent → Executor → Jupyter integration scenario without an
LLM. The Agent submits an Execution through Executor MCP and interrupts after checkpointing its
`execution_id`. The test event bridge receives the terminal notification through an Agent-owned
Redis consumer group and resumes the same LangGraph thread. The resumed graph reconciles
PostgreSQL-backed state and reads Step, Artifact, and Runtime-owned Notebook results through MCP.
When `TEST_AGENT_LLM_MODEL` is configured, ordinary non-execution messages can also use the
OpenAI-compatible vLLM gateway.

## Layout

```text
test_harness/agent/
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

Executor integration code lives under `src/executor_test_agent/integrations/`. It owns public
contract models and does not import Executor internals or access Executor database tables. Redis
is only a wake-up channel; after a terminal event, MCP state is treated as the source of truth.
The smoke bridge creates a unique temporary consumer group per run and deletes it afterward. It is
not the production consumer design: production requires a stable Agent-owned group, transactional
and durable `event_id` deduplication, Pending recovery with `XAUTOCLAIM`, and an Agent-owned DLQ.

## Setup

From the repository root:

```bash
cd test_harness/agent
cp .env.example .env
uv sync
```

For deterministic startup testing, leave `TEST_AGENT_LLM_MODEL` empty. To use the internal vLLM
OpenAI-compatible API, set these values in `test_harness/agent/.env`:

```dotenv
TEST_AGENT_LLM_BASE_URL=http://your-vllm-gateway/v1
TEST_AGENT_LLM_MODEL=your-model-name
TEST_AGENT_LLM_API_KEY=your-runtime-secret
```

The API key is read only from the environment. Do not commit `.env` or real credentials.

The Executor integration settings default to the repository Compose topology:

```dotenv
EXECUTOR_MCP_URL=http://127.0.0.1:8000/mcp
EXECUTOR_REDIS_URL=redis://127.0.0.1:6379/0
EXECUTOR_EVENT_STREAM=executor.events
EXECUTOR_AGENT_CONSUMER_GROUP=executor-test-agent
EXECUTOR_EXECUTION_TIMEOUT_SECONDS=120
```

## Run the Agent Server

```bash
cd test_harness/agent
uv run langgraph dev --no-browser
```

Default endpoints:

- Agent Server: `http://127.0.0.1:2024`
- API documentation: `http://127.0.0.1:2024/docs`
- Graph ID: `executor_test_agent`

In another terminal, invoke the running graph:

```bash
cd test_harness/agent
uv run python scripts/smoke.py
```

The smoke client creates a thread and invokes a deterministic two-Step STATIC Execution. It first
observes the graph's `WAITING_FOR_EVENT` interrupt, consumes the terminal Redis notification, and
resumes the same thread with that event. The second cell creates
`artifacts/reports/agent-e2e.txt`; the client requires both that file and the Runtime-owned
`execution.ipynb`, then prints the terminal event and Jupyter Runtime Target. Override
`TEST_AGENT_SERVER_URL` when the development server uses a different address.

## Full integration test

From the repository root, start current Executor infrastructure and its configured Jupyter target:

```bash
docker compose up -d --build --wait
```

Start the Agent Server in a second terminal:

```bash
cd test_harness/agent
uv sync --all-groups
uv run langgraph dev --no-browser
```

Run the scenario in a third terminal:

```bash
cd test_harness/agent
uv run python scripts/smoke.py
```

On Windows PowerShell without WSL, use the same commands without shell continuations. PostgreSQL,
Redis, Executor, and Jupyter may be native processes instead of containers as long as the `.env`
endpoints point to them. The Agent Server and smoke client are normal Python processes.

## Verify locally

```bash
cd test_harness/agent
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest -q
```

`langgraph dev` is a development server backed by in-memory persistence. The test proves that a
run ends at an interrupt and the same thread resumes while the server remains alive; it does not
prove checkpoint survival across an Agent Server restart. Its local state and behavior must not be
treated as the production persistence or deployment design.
