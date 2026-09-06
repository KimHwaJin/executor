# Redis 6.0.8: normal SINGLE/MULTI E2E

Date: 2026-09-05. Tested Executor commit: `81c66ca`.

Follow-up: the profile mismatch described below was subsequently resolved by
rebuilding and replacing all four local Jupyter containers, without changing
Executor's allowed profiles. See [current-profile verification](jupyter-current-profiles-validation.md).
The remaining text records the earlier test's setup and observations.

## Outcome

Four real-Jupyter executions passed, comprising 10 successful Steps and 38
public Execution event deliveries. PostgreSQL, Redis 6.0.8, Executor, and the
existing local Jupyter containers were used. This was not an LLM/Agent UI test.

| Case | Execution ID | Steps | Operations | Events |
| --- | --- | ---: | ---: | ---: |
| SINGLE REST | `9a62c1eb-7285-4951-8fcb-144af8ca3c09` | 2 | 1 | 8 |
| SINGLE MCP | `542d779c-2c26-4d5d-8d95-a76319d25e63` | 2 | 1 | 8 |
| SINGLE mixed outputs | `47020113-a2dc-49e6-82e7-891577c3a412` | 3 | 1 | 10 |
| MULTI mixed outputs | `3909176f-9e8b-4f62-9dc8-b515d505da87` | 3 | 2 | 12 |

## Important setup finding

The running Jupyter images still expose `basic/ml`, whereas the current
Executor defaults allow `default/3102311`. All four registered Targets initially
reported OFFLINE with `RUNTIME_PROFILE_MISMATCH`. Executor `/readyz` still
reported ready: service readiness does not guarantee a schedulable Runtime.

Testing used a temporary Compose override setting only
`RUNTIME_ALLOWED_PROFILES=basic,ml`, with requests selecting `basic`. Profile
validation was not disabled. All Targets became ACTIVE. After the test, the
override was removed and the original Executor configuration restored.

Consequently, the local profile mismatch remains an open setup issue, not a
Redis failure. Before normal local execution, rebuild/recreate Jupyter with
the intended `default/3102311` configuration, or explicitly configure Executor
to accept the currently deployed profiles. These tests do not validate the
new default/3102311 images or their Python/package versions.

## Verified behavior

- The existing `scripts/single_execution_observability_smoke.py` passed with
  `OBSERVABILITY_RUNTIME_PROFILE=basic` through both REST and MCP.
- SINGLE traversed QUEUED, RUNNING, SUCCEEDED; DB Step and Attempt histories,
  Artifact registration, Outbox publication, event API and Redis event IDs
  agreed. Each generated a two-cell notebook and text Artifact.
- Additional mixed-output cases submitted through MCP. Their verifier used a
  dedicated Redis consumer group created before submission, reading with
  XREADGROUP and acknowledging received messages with XACK.
- MULTI submitted two Steps initially, received both Step references in the
  Operation completion event, then submitted the next Step through REST using
  the event's continuation expected-version. It retained the same kernel,
  observed WAITING_FOR_OPERATION after each Operation, and finalized via MCP.
- MULTI finalization and SINGLE completion emitted successful terminal events.
  All DB Steps and Operations succeeded, and their public Outbox event IDs
  matched Redis and API history. The mixed-output events had contiguous
  Execution-local event sequences starting at 1, with no duplicate event IDs.
- Shared-PV result references were read using the checksum-verifying result
  store. The outputs contained stdout `ANSWER 42`, a pandas HTML table, and a
  decodable image/png representation with a valid PNG signature.
- Both mixed-output executions produced `answer.txt`, `redis608.png`, and
  `execution.ipynb`. The plot file was explicitly created by `fig.savefig` in
  the submitted code; display output alone was not assumed to create a plot
  Artifact. Shared result image representations were verified separately.
- All registered mixed-output Artifacts downloaded without Range as HTTP 200
  with matching Content-Length. Each notebook was 15,417 bytes, valid JSON,
  with all three original code sources, execution counts, image output, and
  final stdout intact. Text and PNG Artifact bodies also matched expectations.
- At test completion both interactive Jupyter servers had zero actual kernels;
  all four Targets had zero active Execution/session counts. The verifier's
  Redis group had zero pending messages before it was removed.

## Evidence and retained data

The supplemental verifier and JSON summary are local test evidence at
`/tmp/executor-redis608-e2e-ndIXeV/normal_e2e.py` and `results.json` in that same
directory. Temporary evidence is not part of the repository and may be removed
by OS temporary-file cleanup.

Execution rows, published events, notebooks, result files and Artifacts remain
available under the Execution IDs above. Existing data was not reset. Only
the verifier's uniquely named Redis consumer group was deleted after testing.

## Not covered in this stage

Failure/cancellation/retry, Redis outages, Worker restart/failover, load,
multi-day soak, Kubernetes/PV behavior and security/ACL acceptance are separate
stages. No production source changes were needed for the four passing cases.
