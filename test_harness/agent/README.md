# Executor Integration Test Agents

An isolated LangGraph/LangChain development application for exercising the Executor through a real
Agent Server. It has its own Python environment and lock file so Agent dependencies do not alter
the Executor service dependency graph.

One Agent Server exposes two independent Graph IDs:

| Graph ID | Behavior |
| --- | --- |
| `executor_mcp_agent` | Preserves the direct Tool Agent. The LLM receives 21 read Tools and five policy-wrapped Executor mutation Tools. |
| `executor_planning_agent` | Routes ordinary chat separately, generates a structured code plan, interrupts for approve/edit/reject, and only then lets graph-owned nodes call Executor mutation Tools. |

Both Agents discover Executor contracts through official MCP SDK 2.x Clients. Redis Streams are a
wake-up channel; after every Operation or terminal boundary the graph reconciles PostgreSQL-backed
state, bounded Step summaries, referenced Runtime outputs, Artifacts, and the Runtime-owned
Notebook through MCP.

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
│   ├── planning.py
│   ├── planning_graph.py
│   ├── planning_state.py
│   └── state.py
├── scripts/chat_smoke.py
├── scripts/planning_chat_smoke.py
├── scripts/smoke.py
└── tests/
```

Executor integration code lives under `src/executor_test_agent/integrations/`. It owns public
contract models and does not import Executor internals or access Executor database tables. Redis
is only a wake-up channel; after every Operation or terminal boundary, MCP state is treated as the
source of truth.
The smoke bridge creates a unique temporary consumer group per run and deletes it afterward. It is
started at the Redis Stream watermark captured immediately before each mutation, scoped to the
expected `operation_id`, and removes duplicate `event_id` values in memory. A newly created group
therefore neither scans the complete history nor mistakes an earlier MULTI boundary for the current
one. It is not the production consumer design: production requires a stable Agent-owned group,
transactional and durable `event_id` deduplication, Pending recovery with `XAUTOCLAIM`, and an
Agent-owned DLQ.

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

With natural-language execution enabled, connect Agent Chat UI to
`http://127.0.0.1:2024`. Change only the Agent/Graph name to switch behavior:

- `executor_mcp_agent`: direct MCP Tool selection.
- `executor_planning_agent`: plan review before every initial code execution.

Then try:

```text
default 커널에서 1부터 10까지의 합계를 계산하고 출력해줘.
```

The MCP Agent can answer current-state questions such as `사용 가능한 커널 종류가 뭐야?` by
calling `runtime_target_list`; `supported_profiles` contains the selectable profiles. Explicit
execution requests call the policy-wrapped `execution_submit`. The Chat UI run waits for the
relevant Redis boundary and then displays the execution ID, status, referenced Step results,
Artifact names, and Runtime-owned notebook path. A MULTI submit or Operation wakes on
`execution.operation_completed`; a finalize wakes on `execution.completed`. This synchronous wait
exists only to make the local Chat UI test self-contained. Production still requires the durable
Agent-owned consumer, event deduplication, Pending recovery, DLQ, and external thread resume
described above.

The Planning Agent classifies ordinary questions as `CHAT` and answers them with read-only MCP
Tools. A code request becomes a validated `ExecutionPlan`, surfaced as a standard HITL request:

- `approve`: submit the generated plan unchanged.
- `edit`: validate and submit `edited_action.args.plan` from the review card.
- `reject`: return `CANCELLED` without creating an Executor Execution.

Approved SINGLE plans execute one Operation. Approved MULTI plans submit their Operations in order
on the retained Runtime session and finalize after the last boundary. An Operation error is shown
to the user and stops later approved Operations; automatic LLM correction is intentionally not
performed without another review policy.

The Agent exposes the allowlisted Executor read Tools and five policy-wrapped mutation Tools:

- Runtime reads: `runtime_target_list`, `runtime_target_get`.
- Execution reads: list/detail/result, Step lists, Operations, Attempts, events, Artifacts, and
  notebook cells. Complete Step output bodies are resolved from checksum-verified shared-PV result
  references rather than an MCP output-body Tool.
- Mutations: submit, cancel, retry, append Operation, and finalize.

It does not expose Runtime target upsert, probe, disable, or state-change Tools. The bridge uses the
official MCP Python SDK 2.x Client directly and converts discovered MCP JSON Schemas to LangChain
`StructuredTool` objects. This preserves the project SDK baseline; the currently available
`langchain-mcp-adapters` release is not used because it is incompatible with the SDK 2.x
`RequestContext` API.

The same LLM paths can be tested without a browser while Agent Server is running:

```bash
# Direct MCP Agent
uv run python scripts/chat_smoke.py

# Planning Agent, print the plan and approve it
uv run python scripts/planning_chat_smoke.py

# Planning Agent cancellation path
TEST_AGENT_PLAN_DECISION=reject uv run python scripts/planning_chat_smoke.py
```

Override the default Korean sum prompt with `TEST_AGENT_CHAT_PROMPT` and Agent Server with
`TEST_AGENT_SERVER_URL`. `chat_smoke.py` also accepts `TEST_AGENT_GRAPH_ID`, defaulting to
`executor_mcp_agent`. Both natural-language scripts require `TEST_AGENT_LLM_MODEL`.

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
- Direct MCP Graph ID: `executor_mcp_agent`
- Planning/HITL Graph ID: `executor_planning_agent`

In another terminal, invoke the running graph:

```bash
cd test_harness/agent
uv run python scripts/smoke.py
```

The smoke client creates a thread and invokes a deterministic two-Operation MULTI Execution. The
initial Operation executes two Steps. After the first `execution.operation_completed`, the graph
submits a one-Step follow-up Operation that reuses variables from the retained Runtime session.
After the second boundary, the graph calls `execution_finalize` and waits for
`execution.completed` with `payload.status=SUCCEEDED`. The follow-up Step creates
`artifacts/reports/agent-e2e.txt`; the client
requires both that file and the Runtime-owned `execution.ipynb`, verifies all three Step results and
the submit/Operation/finalize receipts, then prints the Runtime Target. Override
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
