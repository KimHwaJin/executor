# Executor Integration Guide (for coding agents)

> This single file is enough to write code that calls Executor. For exact field types and required flags,
> use `openapi.json` in this folder (generated from the code). If this guide and `openapi.json` disagree, `openapi.json` wins.
> Written for services inside the Kubernetes cluster calling the deployed Executor.
> A Korean copy for human reviewers is in [agent-guide.ko.md](agent-guide.ko.md).

## 0. Summary

Executor is an HTTP service that runs Python code in a Jupyter kernel **asynchronously**. **Submitting** an execution returns `202`
and an `execution_id` immediately. The caller then **polls the status or consumes Redis events** to learn when it is done, and reads the outcome from the **result API**.

## 1. Rules you must follow

1. Send `X-Api-Key` on every call. Send `X-User-Id` on every write (POST). If your key has no default tenant (for example `pr-batch`), send `X-Tenant-Id` on every call too.
2. Never send fields that are not documented. Unknown fields are rejected with `422`. In particular, never put `tenant_id`, `user_id`, `actor` or `context` in a body (they go in headers).
3. Every POST command carries an `idempotency_key`. **Reuse the same key when retrying the same action**, use a new key for a new action (for example `f"{task_id}:submit"`). The same key with a different body returns `409 IDEMPOTENCY_CONFLICT`.
4. `202` means "accepted". Never treat it as "finished".
5. An execution is finished only when `state.status` is `SUCCEEDED`, `FAILED` or `CANCELLED`. `WAITING_FOR_OPERATION` means a MULTI execution is waiting for your next instruction (not finished).
6. Pass `next_cursor` back as `cursor` exactly as received. Never parse or modify it.
7. Never call the admin APIs (`/runtime-targets`, `/runtime-pools`, `/maintenance`, `/workerz`). They require an admin key.
8. Retry with exponential backoff only on 5xx (`500 INTERNAL_ERROR`, `503 DATABASE_UNAVAILABLE`) and network errors (commands keep the same `idempotency_key`). Never retry a 4xx; fix the cause.

## 2. Connection

| Item | Value |
| --- | --- |
| Base URL | `{EXECUTOR_URL}/api/v1`. Same namespace: `http://executor:8000/api/v1`. Other namespace: `http://executor.<namespace>.svc.cluster.local:8000/api/v1`. Read it from the `EXECUTOR_URL` environment variable; never hard-code it |
| Auth | `X-Api-Key: {API_KEY}`. Each environment (dev, stg, prd) has its own key. Read it from `EXECUTOR_API_KEY` and never log it |
| User | `X-User-Id: {user}`, lowercase `^[a-z0-9][a-z0-9._-]{0,63}$`. Required on writes. On reads it limits results to that user |
| Tenant | `X-Tenant-Id`. Required when the key has no default tenant; otherwise only to act for another tenant. Format `^[a-z][a-z0-9-]{1,62}[a-z0-9]$` |
| Project / session (optional) | `X-Project-Id`, `X-Session-Id`. Send them **only on submit (`POST /executions`)**; they are stored as where the execution belongs. Format `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`. `X-Session-Id` requires `X-Project-Id` |
| Body | `Content-Type: application/json`, at most 30 MB |
| Time | UTC ISO-8601 strings |

Another user's execution returns `404` even if it exists. Use the same `X-User-Id` to submit, read and command an execution.
To filter a list by project, session or user, use query parameters (`project_id`, `session_id`, `user_id`), not headers.

## 3. Concepts

| Term | Meaning |
| --- | --- |
| Execution | One run. `execution_id` is its key |
| Operation | One batch of Steps inside an Execution. SINGLE has one, MULTI has several |
| Step | One piece of code (one Jupyter cell). `sequence` starts at 0 and is contiguous across the **whole Execution** |
| Attempt | One time a Worker ran the execution (first run, retry, recovery). Never use it as a lookup key |
| SINGLE | The Execution ends when its submitted Operation ends |
| MULTI | After each Operation the Execution goes to `WAITING_FOR_OPERATION`. Append more code to the same kernel (variables are kept) or finalize |
| `state.version` | Increments on every state change. Used as `expected_version` in MULTI commands |

## 4. Flow A: SINGLE, run code once and get the result

```text
POST /executions  →  202 {execution_id, state.status=QUEUED}
loop: GET /executions/{id}  →  until state.status is terminal (backoff 2s → max 10s)
GET /executions/{id}/result →  per-Step status, output_summary, error_message, result_ref
```

Submit body:

```json
{
  "idempotency_key": "task-100:submit",
  "lifecycle": {"operation_mode": "SINGLE"},
  "trigger": {"type": "INTERACTIVE"},
  "runtime": {"type": "JUPYTER", "profile": "default"},
  "operation": {
    "operation_timeout_seconds": 600,
    "spec": {
      "schema_version": "1.0",
      "steps": [
        {"sequence": 0, "payload": {"type": "PYTHON_EXECUTE", "source": {"type": "INLINE", "content": "import pandas as pd\nprint(pd.__version__)"}}},
        {"sequence": 1, "payload": {"type": "PYTHON_EXECUTE", "source": {"type": "INLINE", "content": "print(1 + 1)"}}, "step_timeout_seconds": 120}
      ]
    }
  },
  "metadata": {"task_id": "task-100"}
}
```

- **Do not send** `lifecycle.operation_wait_timeout_seconds` for SINGLE (it returns `422`).
- `trigger.type`: `INTERACTIVE` for user-facing work, `BATCH` for batch jobs.
- `runtime.profile`: usually `default`. Allowed values come from the deployment (default `default`, `3102311`).
- At most 100 Steps per Operation (default setting). `sequence` is 0, 1, 2… with no gaps.
- `metadata` keys match `^[a-z][a-z0-9_]{0,63}$`, at most 32 keys and 4096 bytes. No secrets.

## 5. Flow B: MULTI, look at the result and continue with more code

```text
POST /executions (operation_mode=MULTI, operation_wait_timeout_seconds=600)
loop until status ∈ {WAITING_FOR_OPERATION, SUCCEEDED, FAILED, CANCELLED}
GET  /executions/{id}/operations/{operation_id}/result   ← result of the Operation that just ran
     ↓ decide from the result
 ├ continue: POST /executions/{id}/operations {expected_version, spec (sequence continues)}
 ├ finish  : POST /executions/{id}/finalize   {expected_version}   ← only if the last Operation succeeded
 └ stop    : POST /executions/{id}/cancel
```

- `expected_version` is `state.version` from a fresh `GET /executions/{id}`. A mismatch returns `409`; read again and use the latest value.
- The next Operation's `sequence` starts at the number of Steps so far (if the first Operation used 0–1, the next starts at 2). Use the count from `GET /executions/{id}/steps` or the previous Operation's `sequence_range.last + 1`.
- The next-Operation body has **no** `lifecycle`, `trigger`, `runtime` or `operation` wrapper. Send only `{idempotency_key, expected_version, operation_timeout_seconds?, spec, metadata?}`.
- Finalizing after a failed last Operation returns `409`. Run one more Operation with fixed code that succeeds, or cancel.
- If no next request arrives within `operation_wait_timeout_seconds` (at least 30), the execution ends as `FAILED` (`OPERATION_WAIT_TIMEOUT`).

```json
{
  "idempotency_key": "task-100:op-2",
  "expected_version": 4,
  "spec": {
    "schema_version": "1.0",
    "steps": [{"sequence": 2, "payload": {"type": "PYTHON_EXECUTE", "source": {"type": "INLINE", "content": "df.describe()"}}}]
  }
}
```

## 6. Reading results

Shape of `GET /executions/{id}/result` (key fields only):

```text
execution: {execution_id, state: {status, version, reason{code, message, detail}}}
operations[]: {operation_id, operation_number, sequence_range{first,last}, result{status, error_message}, lifecycle, steps[]}
  steps[]: {step_id, sequence, lineage, lifecycle,
            result: {status, error_message, output_summary{output_count, output_types, stream_names, mime_types, has_image, image_count, has_error},
                     result_ref: {relative_path, checksum_sha256, size_bytes, complete, ...} | null}}
attempts[]: Attempt summaries
artifacts[]: Artifact summaries (notebook, reports, ...)
```

- A Step's outcome is `steps[].result.status` (`SUCCEEDED`, `FAILED`, `SKIPPED`, `CANCELLED`, ...). The failure message is `error_message`.
- **Raw output (stdout, tables, images) is not in this response.** Read it one of two ways:
  1. Pod that mounts the shared PVC: the root is `/mnt/data/dtest`. Read `manifest.json` under `/mnt/data/dtest/{result_ref.relative_path}` (`executor/executions/...`), verify `checksum_sha256`, then read the files at `outputs[].representations[].relative_path`. Reject absolute paths and `..` when joining paths.
  2. No shared PVC: use the notebook API, `GET /executions/{id}/notebook?view=FULL&start_index=0&limit=20` or `GET /executions/{id}/notebook/cells/{index}`. If the notebook is not ready yet you get `409 EXECUTION_NOTEBOOK_NOT_AVAILABLE`; try again shortly.
- For files (Artifacts), list them with `GET /executions/{id}/artifacts` and download with `GET /artifacts/{artifact_id}/content`.

## 7. Failure, cancel, retry

| Situation | What to do |
| --- | --- |
| Cancel | `POST /executions/{id}/cancel` `{"idempotency_key": "...", "reason": "..."}` → `CANCEL_REQUESTED` → later `CANCELLED`. Already finished returns `409` |
| Retry a failed SINGLE | If `retry.strategy` from `GET /executions/{id}` is not `NOT_RETRYABLE`, call `POST /executions/{id}/retry` `{"idempotency_key": "..."}`. Executor decides where it restarts |
| Failed MULTI | Retry is not available. Submit the next Operation with fixed code |
| Why it failed | `state.reason{code, message}` from `GET /executions/{id}`; more detail from `GET /executions/{id}/diagnostics` |
| `QUEUED` for a long time | `state.reason.code` from `GET /executions/{id}` says what it waits for (`CAPACITY_FULL`, `NO_ACTIVE_TARGET`, `WAITING_FOR_WORKER`, ...). `detail.queue_position` is its place in the queue. All codes: [README](README.md#상태별-reason) |

`state.reason.code` values for `FAILED`: `TOOL_ERROR` (exception in user code), `STEP_TIMEOUT`, `OPERATION_TIMEOUT`, `OPERATION_WAIT_TIMEOUT`, `EXECUTION_TIMEOUT`, `QUEUE_TIMEOUT`, `OUTPUT_LIMIT_EXCEEDED`, `RUNTIME_UNAVAILABLE`, `RUNTIME_SESSION_LOST`, `INFRASTRUCTURE_ERROR`, `WORKER_SHUTDOWN`, `LEASE_EXPIRED`, `COMPLETION_FAILED`, `INTERNAL_ERROR`.

## 8. Error handling

Every error body is `{"error": {"code", "message", "details": [{"location", "message", "type", "context"}]}}`.
Branch on `code`.

| HTTP | Typical code | What your code does |
| --- | --- | --- |
| 400 | `INVALID_IDENTITY` | Fix the header value. Do not retry |
| 401 / 403 | `UNAUTHENTICATED` / `FORBIDDEN` | Key or permission problem. Do not retry |
| 404 | `EXECUTION_NOT_FOUND`, ... | Wrong id, or a different `X-User-Id` |
| 409 | `EXECUTION_VERSION_CONFLICT`, `INVALID_STATE_TRANSITION` | Re-read state and version with `GET /executions/{id}`, then decide |
| 409 | `IDEMPOTENCY_CONFLICT` | Same key sent with a different body. Fix how keys are built |
| 409 | `EXECUTION_NOTEBOOK_NOT_AVAILABLE` | Try again shortly |
| 413 | `REQUEST_BODY_TOO_LARGE` | Send less code or use a PATH source |
| 422 | `REQUEST_VALIDATION_ERROR`, `INVALID_EXECUTION_SPEC`, `UNSUPPORTED_RUNTIME_PROFILE`, `INVALID_CURSOR` | Fix the value that `details[].location` points to |
| 500 / 503 | `INTERNAL_ERROR` / `DATABASE_UNAVAILABLE` | Retry with backoff (same `idempotency_key`) |

## 9. Python client example

```python
import time
import uuid

import httpx

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class ExecutorError(Exception):
    def __init__(self, status: int, body: dict):
        err = body.get("error", {})
        self.status, self.code = status, err.get("code")
        self.details = err.get("details", [])
        super().__init__(f"{status} {self.code}: {err.get('message')}")


class ExecutorClient:
    def __init__(self, base_url: str, api_key: str, user_id: str,
                 tenant_id: str | None = None):
        headers = {"X-Api-Key": api_key, "X-User-Id": user_id}
        if tenant_id:
            headers["X-Tenant-Id"] = tenant_id
        self._http = httpx.Client(base_url=f"{base_url}/api/v1", headers=headers, timeout=30)

    def _call(self, method: str, path: str, **kw) -> dict:
        for attempt in range(5):
            try:
                r = self._http.request(method, path, **kw)
            except httpx.TransportError:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise ExecutorError(r.status_code, r.json())
            return r.json()
        raise RuntimeError(f"{method} {path} failed after retries")

    # --- commands -------------------------------------------------------
    def submit(self, codes: list[str], *, multi: bool = False, key: str | None = None,
               trigger: str = "INTERACTIVE", profile: str = "default",
               operation_timeout: int | None = 600, wait_timeout: int = 600,
               metadata: dict | None = None,
               project_id: str | None = None, session_id: str | None = None) -> dict:
        lifecycle = {"operation_mode": "MULTI" if multi else "SINGLE"}
        if multi:
            lifecycle["operation_wait_timeout_seconds"] = wait_timeout
        operation = {"spec": _spec(codes, start=0)}
        if operation_timeout:
            operation["operation_timeout_seconds"] = operation_timeout
        body = {
            "idempotency_key": key or f"submit-{uuid.uuid4()}",
            "lifecycle": lifecycle,
            "trigger": {"type": trigger},
            "runtime": {"type": "JUPYTER", "profile": profile},
            "operation": operation,
        }
        if metadata:
            body["metadata"] = metadata
        scope = {}
        if project_id:
            scope["X-Project-Id"] = project_id
            if session_id:
                scope["X-Session-Id"] = session_id
        return self._call("POST", "/executions", json=body, headers=scope)

    def add_operation(self, execution_id: str, codes: list[str], *, key: str) -> dict:
        state = self.get(execution_id)
        steps = self._call("GET", f"/executions/{execution_id}/operations", params={"limit": 200})
        next_seq = max((op["sequence_range"]["last"] for op in steps["items"]), default=-1) + 1
        return self._call("POST", f"/executions/{execution_id}/operations", json={
            "idempotency_key": key,
            "expected_version": state["state"]["version"],
            "spec": _spec(codes, start=next_seq),
        })

    def finalize(self, execution_id: str, *, key: str) -> dict:
        version = self.get(execution_id)["state"]["version"]
        return self._call("POST", f"/executions/{execution_id}/finalize",
                          json={"idempotency_key": key, "expected_version": version})

    def cancel(self, execution_id: str, *, key: str, reason: str | None = None) -> dict:
        return self._call("POST", f"/executions/{execution_id}/cancel",
                          json={"idempotency_key": key, "reason": reason})

    def retry(self, execution_id: str, *, key: str) -> dict:
        return self._call("POST", f"/executions/{execution_id}/retry", json={"idempotency_key": key})

    # --- queries --------------------------------------------------------
    def get(self, execution_id: str) -> dict:
        return self._call("GET", f"/executions/{execution_id}")

    def list(self, *, status: str | None = None, project_id: str | None = None,
             session_id: str | None = None, limit: int = 100) -> list[dict]:
        params = {k: v for k, v in {"status": status, "project_id": project_id,
                                    "session_id": session_id, "limit": limit}.items() if v}
        items, cursor = [], None
        while True:
            page = self._call("GET", "/executions", params={**params, **({"cursor": cursor} if cursor else {})})
            items += page["items"]
            if not page["has_more"]:
                return items
            cursor = page["next_cursor"]

    def result(self, execution_id: str) -> dict:
        return self._call("GET", f"/executions/{execution_id}/result")

    def notebook(self, execution_id: str, start: int = 0, limit: int = 20) -> dict:
        return self._call("GET", f"/executions/{execution_id}/notebook",
                          params={"view": "FULL", "start_index": start, "limit": limit})

    def wait(self, execution_id: str, until: set[str] = TERMINAL, timeout: float = 3600) -> dict:
        delay, deadline = 2.0, time.monotonic() + timeout
        while True:
            execution = self.get(execution_id)
            if execution["state"]["status"] in until:
                return execution
            if time.monotonic() > deadline:
                raise TimeoutError(execution_id)
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)


def _spec(codes: list[str], start: int) -> dict:
    return {"schema_version": "1.0", "steps": [
        {"sequence": start + i,
         "payload": {"type": "PYTHON_EXECUTE", "source": {"type": "INLINE", "content": code}}}
        for i, code in enumerate(codes)
    ]}


# SINGLE
import os
client = ExecutorClient(os.environ["EXECUTOR_URL"], api_key=os.environ["EXECUTOR_API_KEY"],
                        user_id="user-100", tenant_id=os.environ.get("EXECUTOR_TENANT_ID"))
accepted = client.submit(["print('hello')"], key="task-100:submit",
                         project_id="proj-1", session_id="chat-42")
done = client.wait(accepted["execution_id"])
result = client.result(accepted["execution_id"])
for op in result["operations"]:
    for step in op["steps"]:
        print(step["sequence"], step["result"]["status"], step["result"]["error_message"])

# MULTI
accepted = client.submit(["import pandas as pd\ndf = pd.DataFrame({'a': [1, 2]})"], multi=True, key="task-200:submit")
eid = accepted["execution_id"]
client.wait(eid, until=TERMINAL | {"WAITING_FOR_OPERATION"})
client.add_operation(eid, ["print(df.sum())"], key="task-200:op-2")
client.wait(eid, until=TERMINAL | {"WAITING_FOR_OPERATION"})
client.finalize(eid, key="task-200:finalize")
client.wait(eid)
```

`add_operation` assumes no more than 200 Operations when computing the next sequence. If there can be more, follow `next_cursor` to the end.

## 10. Events instead of polling (optional)

For high volume or low latency, consume the Redis Stream `executor.events.{tenant_id}`. Treat an event only as a signal to read the state again.

- Key events: `execution.operation_completed` (MULTI decides here; it carries `payload.continuation.expected_version`) and `execution.completed` (finished).
- The same `event_id` can arrive more than once. If `event_sequence` skips, fill the gap with `GET /executions/{id}/events?after_sequence={last}`.
- Full rules are in [05-events.md](05-events.md). Starting with polling is fine.

## 11. MCP (dev only)

MCP is enabled **only in the dev deployment**; in stg and prd `/mcp` does not exist (404). Write code that will reach production against REST.
In dev, connect to `POST /mcp` on the same server (Streamable HTTP, stateless) with the same headers. `Host` and `Origin` must be on Executor's allowlist. Tool names include `execution_submit`,
`execution_get`, `execution_result_get`, `execution_operation_create`, `execution_finalize` and `execution_cancel`.
Command tools take one `request` object (the REST body plus `execution_id`). Errors read `[CODE] message details=[...]`. The full list is in [04-mcp.md](04-mcp.md).

## 12. Never do this

- Treat `202` as success.
- Track state by `attempt_id` instead of `execution_id`.
- Build or edit `next_cursor`, or change filters in the middle of paging.
- Create a new `idempotency_key` on every retry (this creates duplicate executions).
- Cache the MULTI version and reuse it (read it right before each command).
- Make the final decision from an event payload alone (confirm with `GET .../result` before reporting).
- Call admin APIs, or assume fields that are not in the response.

## 13. Endpoint cheat sheet

| Goal | Call |
| --- | --- |
| Submit | `POST /executions` |
| Status | `GET /executions/{id}` |
| Result | `GET /executions/{id}/result` |
| One Operation's result | `GET /executions/{id}/operations/{operation_id}/result` |
| MULTI: next code | `POST /executions/{id}/operations` |
| MULTI: finish | `POST /executions/{id}/finalize` |
| Cancel | `POST /executions/{id}/cancel` |
| Retry SINGLE | `POST /executions/{id}/retry` |
| My executions | `GET /executions?status=&project_id=&session_id=&limit=` (with `X-User-Id`, only yours) |
| Notebook | `GET /executions/{id}/notebook?view=FULL` |
| List / download files | `GET /executions/{id}/artifacts`, `GET /artifacts/{artifact_id}/content` |
| Save a text report | `POST /executions/{id}/artifacts` (`SUCCEEDED` executions only, `type: "REPORT"`) |
| Failure diagnostics | `GET /executions/{id}/diagnostics` |
