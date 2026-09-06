# Current Jupyter profiles: local replacement and verification

Date: 2026-09-05. Executor source: `81c66ca`.

## Result

All four local Jupyter containers now run the current Dockerfile configuration.
The former `basic/ml` profile mismatch is resolved. Executor's original
`default/3102311` allowed-profile settings were retained; no temporary override
is in use. Redis remains 6.0.8.

| Kernel profile | Actual Python | Separate executable |
| --- | --- | --- |
| `default` | 3.11.14 | `/opt/venvs/default/bin/python` |
| `3102311` | 3.10.19 | `/opt/venvs/3102311/bin/python` |

The profile `3102311` is a name, not a requirement to pin Python to 3.10.11.
The accepted requirement is Python 3.10.x. Its custom requirements file remains
empty: only the kernel's baseline dependencies are installed. It does not
inherit default's analysis packages.

## Replacement

```bash
docker compose build jupyter
docker compose --profile multi-jupyter --profile batch-jupyter \
  up -d --no-deps --wait \
  jupyter jupyter-secondary jupyter-batch-primary jupyter-batch-secondary
```

- Dockerfile: `test_harness/jupyter/Dockerfile`, unchanged.
- Image: `executor-jupyter:local`.
- Built image ID:
  `sha256:a70826595097f917621f6e6bb23bce92409639e5b92196390ed00aa2e8f0fb49`.
- All four containers were checked to use that same image ID.
- Existing bind mount `test_harness/jupyter/workspace:/workspace/pv` was kept
  unchanged on every server. No volume, database or Stream was reset.
- Executor, PostgreSQL and Redis were not recreated during this replacement.
- Existing notebook for Execution `47020113-a2dc-49e6-82e7-891577c3a412`
  still downloaded as valid three-cell JSON, 15,417 bytes, after replacement.

## Real Execution verification

| Profile | Mode | Execution ID | Steps | Operations | Events |
| --- | --- | --- | ---: | ---: | ---: |
| default | SINGLE | `d51c2ee5-a991-4b63-b2a9-4d9734eaba05` | 3 | 1 | 10 |
| default | MULTI | `0bf334b3-1a28-4cdf-91a2-000412644b53` | 3 | 2 | 12 |
| 3102311 | SINGLE | `a0a08d86-01b8-4136-8c97-cf8f0f3d73f4` | 3 | 1 | 10 |
| 3102311 | MULTI | `7d34b84b-21b7-4e7f-ba76-56dde85cf2f7` | 3 | 2 | 12 |

All four executions succeeded, totaling 12 Steps and 44 public event deliveries.
The executed code asserted its actual Python major/minor version and virtual
environment prefix; the table above is not inferred from kernelspec labels.

Verified in each case:

- MCP submission; for MULTI, initial two-Step Operation, Redis completion event,
  REST subsequent Operation using the event's expected-version, then MCP finalize.
- Same retained kernel across MULTI Operations, including reading the previous
  Operation's variable and producing `ANSWER 42`.
- Successful DB Step/Operation states and matching Outbox/API/Redis event IDs.
- Consumer-group receipt and acknowledgements, contiguous event sequences,
  no duplicate event IDs, and zero pending messages in the verifier group.
- Checksum-verified shared result files containing text, HTML and PNG output.
- Complete original code and outputs in downloaded three-cell notebooks.
- HTTP 200 full Artifact downloads with matching Content-Length, valid PNG
  signatures and `answer.txt` content equal to `42`.

Default tested pandas and matplotlib. The minimal 3102311 environment used
Python standard-library PNG generation and IPython display/HTML; no analysis
packages were added for testing. Each execution explicitly wrote its plot
Artifact separately from displaying the image.

Local supplementary evidence is in
`/tmp/executor-redis608-e2e-ndIXeV/normal_e2e.py` and `profile-results.json`.
The script was extended from the previous verification. Temporary files are
not repository deliverables and may be removed by OS cleanup. Generated
Execution records, outputs, notebooks and Artifacts remain for inspection.

## Final service state and boundaries

All four kernelspec APIs returned exactly `default` and `3102311`. All Targets
were ACTIVE, all actual Jupyter kernel counts were zero, and all active
Execution counts were zero. Executor readiness checks passed.

The batch servers were rebuilt and health/profile checked; batch Execution
flow, load, failure recovery and multi-day soak were not re-run in this stage.
No production API/schema or application code changes were required.
