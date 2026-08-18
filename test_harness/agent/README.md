# Executor Integration Test Agent

An isolated LangGraph/LangChain development application for exercising the Executor through a real
Agent Server. It has its own Python environment and lock file so Agent dependencies do not alter
the Executor service dependency graph.

The graph supports both a deterministic Agent → Executor → Jupyter integration scenario without an
LLM and a natural-language Chat UI scenario with an OpenAI-compatible LLM. The deterministic flow
submits an Execution through Executor MCP and interrupts after checkpointing its
`execution_id`. The test event bridge receives the terminal notification through an Agent-owned
Redis consumer group and resumes the same LangGraph thread. The resumed graph reconciles
PostgreSQL-backed state and reads Step, Artifact, and Runtime-owned Notebook results through MCP.
When `TEST_AGENT_LLM_MODEL` is configured, LangChain `create_agent` receives an explicit allowlist
of Executor MCP Tools. Read Tools use the server-discovered MCP schemas, while five mutation Tools
apply Agent-side identity, ownership, idempotency, state-version, and code policies before calling
the corresponding MCP Tool. Runtime administration Tools are never exposed. Long-running mutations
wait on the Redis notification and then reconcile authoritative results through Executor MCP.

## Layout

```text
test_harness/agent/
├── langgraph.json
├── pyproject.toml
├── uv.lock
├── src/executor_test_agent/
│   ├── config.py
│   ├── graph.py
│   ├── code_policy.py
│   ├── mcp_tools.py
│   └── state.py
├── scripts/chat_smoke.py
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
TEST_AGENT_ENABLE_NL_EXECUTION=true
TEST_AGENT_USER_ID=chat-ui-user
TEST_AGENT_PROJECT_ID=chat-ui-project
```

The API key is read only from the environment. Do not commit `.env` or real credentials.

With natural-language execution enabled, connect Agent Chat UI with deployment URL
`http://127.0.0.1:2024` and graph ID `executor_test_agent`, then try:

```text
basic 커널에서 1부터 10까지의 합계를 계산하고 출력해줘.
```

The Tool Agent can answer current-state questions such as `사용 가능한 커널 종류가 뭐야?` by
calling `runtime_target_list`; `supported_profiles` contains the selectable profiles. Explicit
execution requests call the policy-wrapped `execution_submit`. The Chat UI run waits for the
terminal Redis event and then displays the execution ID, status, notebook outputs, Artifact names,
and Runtime-owned notebook path. This synchronous wait exists only to make the local Chat UI test
self-contained. Production still requires the durable Agent-owned consumer, event deduplication,
Pending recovery, DLQ, and external thread resume described above.

The Agent exposes 16 read Tools and five policy-wrapped mutation Tools:

- Runtime reads: `runtime_target_list`, `runtime_target_get`.
- Execution reads: list/get, Steps, Operations, Attempts, events, Artifacts, and notebook cells.
- Mutations: submit, cancel, retry, continue, and finish.

It does not expose Runtime target upsert, probe, disable, or state-change Tools. The bridge uses the
official MCP Python SDK 2.x Client directly and converts discovered MCP JSON Schemas to LangChain
`StructuredTool` objects. This preserves the project SDK baseline; the currently available
`langchain-mcp-adapters` release is not used because it is incompatible with the SDK 2.x
`RequestContext` API.

The same LLM path can be tested without a browser while Agent Server is running:

```bash
uv run python scripts/chat_smoke.py
```

Override the default Korean sum prompt with `TEST_AGENT_CHAT_PROMPT` and Agent Server with
`TEST_AGENT_SERVER_URL`. Unlike `scripts/smoke.py`, this script requires `TEST_AGENT_LLM_MODEL` and
exercises LLM Tool selection before submitting the execution.

The mutation Tool guard rejects several obvious process, network, environment, and dynamic-code access
patterns, but it is not a Python sandbox. Run the natural-language harness only with trusted models,
prompts, data, and isolated test infrastructure. Production code-execution isolation remains a
deployment and Runtime security responsibility.

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

The smoke client creates a thread and invokes a deterministic two-Step SINGLE Execution. It first
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
