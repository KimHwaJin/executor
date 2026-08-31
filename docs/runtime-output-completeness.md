# Runtime output completeness — 2026-08-31

## Outcome

Jupyter-generated IOPub data/message rate warnings now enter the existing
`OUTPUT_LIMIT_EXCEEDED` failure workflow. A known suppressed output is **not**
reported as a complete successful Step. Jupyter rate limits and Executor's
32 MiB per-message ceiling remain unchanged.

No new Jupyter extension, REST/MCP endpoint, request field, Redis schema version
or database migration is required. The public failure type remains the existing
`OUTPUT_LIMIT_EXCEEDED`; its message distinguishes message size, data rate and
message rate. Structured common diagnostic fields are still separate follow-up work.

## Detection and compatibility boundary

The adapter requires all of the following, not just a text search:

1. IOPub `stream` output named `stderr`.
2. `parent_header.msg_id` matches the current execute request.
3. `header.session` equals the unique WebSocket connection `session_id` supplied
   by Executor. Ordinary kernel stdout/stderr uses the kernel's own session.
4. The native Jupyter warning prefix and the corresponding `ServerApp` rate-limit
   setting name match the known data/message rate warning format.

Installed Jupyter Server **2.20.0** was inspected and tested. In this version,
`KernelWebsocketHandler.pre_get` sets the connection Session from `session_id`;
`ZMQChannelsWebsocketConnection.write_stderr` constructs server warnings with it;
`_limit_rate` sends the warning when suppressing output. This implementation detail
is not a universal Jupyter protocol guarantee. Jupyter upgrades/custom gateways
must rerun the live check below. A server that changes the warning origin/format
or silently drops output without a signal requires a separate adapter change.

This is not an authentication boundary against code intentionally forging kernel
protocol headers. It prevents normal user stderr, including an exact copy of the
warning text, from being mistaken for the server's limiter.

## Processing and state

The warning is first saved as normal output evidence. Executor then seals the
partial manifest as `ABORTED`, `complete=false`, with the limit reason, stops the
current collection, and interrupts/confirms Runtime state using the existing
timeout/output-limit workflow. Following Steps in that Operation are skipped.

| Case | Step / Operation | Execution | Output reference |
| --- | --- | --- | --- |
| SINGLE rate limit | `FAILED` / `FAILED` | `FAILED` | `complete=false` |
| MULTI rate limit, idle confirmed | `FAILED` / `FAILED` | `WAITING_FOR_OPERATION` | `complete=false` |
| MULTI rate limit, session lost | `FAILED` / `FAILED` | `FAILED` | `complete=false` |
| Ordinary kernel warning text | Normal execution rules | Normal mode lifecycle | `complete=true` when otherwise complete |

The existing retry policy is unchanged: kernel reuse requires confirmed idle.
Do not blindly retry the same large print/display; reduce displayed output or
write large data to an artifact. Code may already have produced side effects.
No automatic retry, hidden truncation, recovery of dropped output or limit
increase is introduced.

Step / Operation / Attempt records and operator logs preserve the failure reason.
The notebook contains the actually retained warning/partial outputs, not invented
missing output. `complete=false` describes delivery, not whether Python finished
some or all of its computation before the limit was observed.

Redis behavior remains compatible:

- `execution.step_completed`: `status=FAILED`, generic Step error code and the
  specific rate/size failure message.
- `execution.operation_completed`: `status=FAILED` and an error; MULTI consumers
  must not interpret `WAITING_FOR_OPERATION` as Operation success.
- SINGLE `execution.completed`: `status=FAILED`, error code
  `EXECUTION_OUTPUT_LIMIT_EXCEEDED`.
- Incomplete `result_ref` is still omitted from events by the existing contract.
  Retrieve Step detail or Execution result to locate partial evidence in shared PV.

## Validation

- Protocol and Worker output safety suite: 48 cases across data/message/size
  limits, matching/mismatched sessions, parent IDs, channel/name, binary/text
  framing, streaming/non-streaming, SINGLE/MULTI and retained/missing sessions.
- Full regression suite: **337 passed**; PostgreSQL/Redis integration: **28 passed**.
- Ruff, format and ty passed.
- Live local Jupyter: **24 case runs**. Each matrix has normal output, an exact
  kernel-origin warning echo, and a real data-rate breach, in both SINGLE and MULTI.

| Kernel | Large stdout | Case runs | Outcome |
| --- | ---: | ---: | --- |
| basic / Python 3.11 | 5 MiB | 6 | Passed |
| basic / Python 3.11 | 10 MiB | 6 | Passed |
| basic / Python 3.11 | 25 MiB | 6 | Passed |
| ml / Python 3.12 | 5 MiB | 6 | Passed |

For the limit cases only the 321-byte server warning was retained in this local
configuration. Those are successful **limit-detection tests**, not successful
large-output retention. Every limit Step was `FAILED`, `complete=false`; the next
Step was skipped. Normal output and warning echoes remained successful. The later
10/25 MiB and ML runs additionally compared saved notebook output to the shared
result files and checked the durable Step-event failure message.

Example live execution IDs (isolated test DB, not the running service DB):

- basic 25 MiB SINGLE: `fd8cefbd-adf6-4bf2-8666-c18d40f9dff5`
- basic 25 MiB MULTI: `9ed2e527-1ebf-473f-b22b-5262ec0e0617`
- ml 5 MiB SINGLE: `ed0e9d97-4a9f-41c2-930d-646036405108`
- ml 5 MiB MULTI: `1729e1ea-e7c3-4fb0-9812-24b9302e1a37`

The live harness uses the current Python code, an isolated SQLite DB and temporary
shared result storage. It exercises real Jupyter and Worker persistence/Outbox
creation; it does **not** run the public HTTP/MCP listener or publish to Redis.
PostgreSQL/Redis behavior is covered separately by the integration gate. Test
kernels are deleted; temporary DB/shared evidence is discarded. Generated test
notebooks remain on Jupyter storage under `users/diagnostics-smoke/`.
The existing Executor container/image was not rebuilt or restarted by this work.

## Reproduce (Docker optional, PowerShell supported)

The Jupyter image/extension does not need modification. Set the URL/token for an
existing test Jupyter with basic/ml kernels and its normal rate policy:

```powershell
$env:JUPYTER_GATEWAY_ENDPOINT = 'http://127.0.0.1:8888'
$env:JUPYTER_GATEWAY_TOKEN = '<test Jupyter token>'
$env:JUPYTER_GATEWAY_PROFILE = 'basic'
$env:JUPYTER_GATEWAY_OUTPUT_MIB = '5'
uv run python scripts/jupyter_output_completeness_smoke.py
```

POSIX shells use `export NAME=value` for the same variables. Profile defaults to
`basic`; output size defaults to 5 MiB and is restricted to 1–25 MiB. An empty
token supports a deliberately tokenless local test server. The harness must fail
if its workload does not actually trigger the configured limiter; do not change
the server policy merely to hide or manufacture a pass.

The synthetic message-rate warning path is covered by protocol/Worker tests; an
actual message-rate flood, full T35 load matrix, unknown remote output loss,
and common durable diagnostic fields remain outside this validation.
