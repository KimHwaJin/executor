# Expanded output validation — 2026-08-31

## Outcome: output preservation is not ready

The current local configuration loses large stdout output at the Jupyter IOPub rate limiter.
Executions still report success and complete results. This is a blocking correctness finding,
not a successful performance result. No Executor runtime or Jupyter configuration changes were
made in this work; only the measurement harness and regression tests were corrected.

| Scenario, concurrency 1 | Execution outcome | Requested content retained | Validation |
| --- | --- | ---: | --- |
| Text 5 MiB | `SUCCEEDED`, `complete=true` | 0 stdout bytes | FAILED |
| Text 10 MiB | `SUCCEEDED`, `complete=true` | 0 stdout bytes | FAILED |
| Text 25 MiB | `SUCCEEDED`, `complete=true` | 0 stdout bytes | FAILED |
| PNG 5 MiB | `SUCCEEDED`, `complete=true` | 5,242,880 bytes | PASSED |
| PNG 10 MiB | `SUCCEEDED`, `complete=true` | 10,485,760 bytes | PASSED |
| PNG 25 MiB | `FAILED`, `OUTPUT_LIMIT_EXCEEDED`, incomplete ref | Complete PNG not retained | Expected-limit protection PASSED |

The limit scenario's harness subsequently requested retry then cancellation to release the
retained Runtime session. Its final state is `CANCELLED` with cleanup `SUCCEEDED`; that is test
cleanup, not automatic production retry. Passing this scenario does not mean 25 MiB PNG support.

## Environment

- Local Compose Executor, PostgreSQL 17, Redis 7.4, Jupyter Server 2.20.0.
- Database revision `0001`; one registered INTERACTIVE Jupyter, profile `basic`, capacity 1.
- Jupyter container limits: 2 CPU cores / 4 GiB memory.
- Executor `RUNTIME_MAX_OUTPUT_MESSAGE_BYTES=33554432` (32 MiB), unchanged.
- Observed Jupyter data rate: 1,000,000 bytes/s over a 3-second window, unchanged.
- Original shared results and Jupyter workspace were preserved. This run did not reset data.

## Cause and impact

The Jupyter log and retained stderr explicitly report `IOPub data rate exceeded`. The installed
`ZMQChannelsWebsocketConnection._limit_rate` implementation drops the over-limit output message
and emits a stderr warning. Its byte-rate accounting applies to `stream` messages, explaining why
the same-sized `display_data` PNGs behaved differently in this installed version.

Executor's Jupyter protocol adapter treats this as ordinary stderr. The execution collector
marks an error only for an error execute reply or error output, so the successful execute reply
and idle status complete the Step. The files and notebook faithfully preserve the warning but
never receive the requested stdout. Their checksums match, yet `complete=true` is misleading.

- Relevant code: `src/executor_service/infrastructure/_jupyter/execution.py` and `protocol.py`.
- Text notebooks were only 1,138–1,141 bytes, containing source and the warning, not 5–25 MiB output.
- Missing output cannot be recovered from those saved notebooks or result files.
- Agents relying on success/completeness can wrongly assume they received the actual result.
- Merely raising Executor's WebSocket size limit does not fix this separate Jupyter rate limit.

PNG 25 MiB expands to 34,952,536 base64 bytes before JSON framing, exceeding the independent
32 MiB message limit. That case correctly records an explicit failure and an incomplete result.
No limit was raised to make a case pass.

## Test harness correction

The initial T35 run checked execution status and references but not actual content. Its initial
`PASSED` report (`8eb86297ffaa4fd88838d48736cfb9ce`) is not accepted as successful text-output
evidence. The raw report remains untouched for traceability.

The harness now:

- Measures durable `execution_events` and `execution_event_sequences` tables as well as Outbox.
- Reads the returned shared Step reference through the checksum-validating result store.
- Requires one complete result for the controlled successful one-Step workload.
- Checks exact stdout byte count and run marker, or one PNG's base64, signature, marker and size.
- Records `output_validation` on each retrieval and `validation_error` on the scenario.
- Writes a failure report with scenario measurements before exiting nonzero on missing content.
- Marks full-content checks `NOT_APPLICABLE` only for explicit expected-output-limit cases;
  those still require an incomplete reference and validated cancellation/cleanup.

Use `LOCAL_TEST_SHARED_STORAGE_ROOT` when the host-side shared PV is not `shared_dir`.
The generic Executor does not know an arbitrary function's expected output; exact expected sizes
and markers here belong only to this controlled test workload.

Regression coverage includes valid text/PNG, rate-warning-only output, truncated text with a
correct marker, invalid PNG, and inclusion of durable event tables. Full quality gate:
255 unit tests, 10 Redis tests, 18 PostgreSQL tests, Ruff and ty all passed.

## Measurements and integrity

The verified warm-process image runs returned result API responses of 2,598 bytes (5 MiB PNG)
and 2,600 bytes (10 MiB PNG), in 6.183 ms and 6.793 ms respectively. Their notebooks were
6,991,992 and 13,982,503 bytes. Independent checks confirmed exact shared-output/notebook bytes
and matching generated source code.

The sampled Executor RSS in that warm series was about 210.69 MiB, with near-zero incremental
growth, after earlier trials had already allocated buffers. This does not imply zero temporary
allocation or a leak-free memory bound. First-pass PNG cases observed increases of about
22.51 MiB and 25.00 MiB. Resource samples had no reported collection errors. Database physical
growth in the warm image runs was 8 KiB / 24 KiB; page reuse and concurrent activity make this
unsuitable as an exact per-execution storage cost.

These are single-run observations, not latency distributions or production capacity estimates.
Some revalidation overlapped independent quality-gate tests. Text memory/latency observations
must not be used to claim large-text throughput because the requested stdout never arrived.
PNG fixtures are valid size-controlled one-pixel images with padding, not realistic plot rendering.

Final checks: all 20 local Executions are terminal (16 succeeded, 4 cancelled, including earlier
tests). All 148 PostgreSQL/Redis public event IDs match; no duplicate IDs or sequence gaps.
There are zero active Executions, zero Jupyter sessions and zero unpublished Outbox rows.
Readiness is healthy. Jupyter emitted WebSocket-closed errors during deliberately oversized
message rejection; no sessions remained afterward.

## Evidence and reproduction

Evidence is gitignored under `test-results/output-expansion-20260831/`. The six corrected runs:

| Scenario | Report run ID | Execution ID |
| --- | --- | --- |
| Text 5 | `99a36291fa634d279ec5b5ee4a8fd7d0` | `f4c795b3-8f95-4eba-a1aa-9aff9dc42fd0` |
| Text 10 | `ff13fdbcccef47acab7f79b4ccac25af` | `6a2b5602-c104-45e6-86c9-70bf01c9e8b5` |
| Text 25 | `1caa9a4ce444425194a725605ee145de` | `499df830-64c0-4aba-8286-9a867fc321c6` |
| Image 5 | `d519f3b471e44876a50d85feea5a335c` | `0916af6b-617a-4120-a817-c741ab67d17f` |
| Image 10 | `c2c2e8ee746543dd83472a930ae8c31a` | `efff9a6d-6ddc-4784-a66f-76d5109e32b0` |
| Image 25 | `9c1c72f5f4ab419e921df108e64b19a4` | `819a78e6-6d7c-4a0c-8b83-6571491e093c` |

Reports are named `t35-output-measurement-<run-id>.json`. Additional evidence:
`verified-*.log`, `notebook-integrity.log`, `event-integrity.log`, `quality-gate.log`.

With local service/DB/Redis/shared-volume environment configured, run each independently:

```bash
uv run python scripts/t35_output_measurement.py --scenario TEXT:5:1
uv run python scripts/t35_output_measurement.py --scenario TEXT:10:1
uv run python scripts/t35_output_measurement.py --scenario TEXT:25:1
uv run python scripts/t35_output_measurement.py --scenario IMAGE:5:1
uv run python scripts/t35_output_measurement.py --scenario IMAGE:10:1
uv run python scripts/t35_output_measurement.py --scenario IMAGE:25:1 --expect-output-limit
```

## Next work — not implemented here

1. Define a Jupyter configuration/transport policy that does not silently suppress Executor-bound
   output while retaining explicit size limits and resource controls.
2. Prevent a known output-loss condition from being reported as a complete result. Review reliable
   signaling before relying on broad stderr text matching, which could misclassify user output.
3. Repeat these exact-byte tests after the fix, then proceed to message-limit boundaries and
   multi-execution load. Full T35 and soak validation remain open.
