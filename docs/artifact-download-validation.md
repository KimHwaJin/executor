# Current-file Artifact download validation

## Change / rollback boundary

- Branch: `feature/current-file-artifact-download`
- Previous main / rollback reference: `274d8ae`
- Public URL: `GET /api/v1/artifacts/{artifact_id}/content` (unchanged)
- No Execution/Redis JSON schema or database migration changes.
- Runtime internal file protocol changes from `start/end` query bounds to optional `Range`.
  Executor and the Jupyter extension/image must be deployed or rolled back together.
- Existing Compose containers, local PostgreSQL/Redis data and user PV workspaces were not
  replaced/reset during verification. Live tests used disposable Jupyter containers, local
  Executor/BFF HTTP servers and isolated SQLite databases.

## Behavior verified

1. Full downloads use the opened file's actual size and SHA-256, independent of DB observations.
2. Single ranges, suffix ranges, open-ended ranges and end clamping retain `206` semantics;
   invalid/out-of-bounds/empty-file ranges produce `416` with current total size.
3. Zero-byte files produce an empty `200` without Range.
4. Metadata and body stay on one descriptor, with bounded reads and explicit ownership of
   upstream responses/drivers. POSIX atomic replacement leaves an active reader on the old file.
5. In-place edits are detected by size/mtime and requested-byte digest checks, not claimed to be
   snapshot-isolated. Known inconsistent final bytes are not emitted as a completed response.
6. A setup-only inconsistency permits one fresh open before headers. Continued changes fail;
   after metadata is exposed, neither target fallback nor replacement-file concatenation occurs.
7. Cancellation during setup, streaming errors, client send failures and normal completion close
   the owning HTTP/file resources. Error logs preserve transport type and received/expected length.
8. Artifact registration observations and audit fields remain unchanged by download reads.
9. `append_to_notebook` remains opt-in (`false` by default); no report API behavior was changed.

## Automated checks

```shell
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest -q
```

Result: **518 passed, 28 skipped**. Skips: 24 opt-in PostgreSQL multi-worker tests, 3 opt-in
live-download cases and 1 root-only Linux identity test. The 3 live cases and Linux identity test
were run separately and passed; PostgreSQL multi-worker tests were not rerun for this change.

Focused coverage includes `tests/test_artifact_content.py`, `tests/test_runtime_file_content.py`,
`tests/test_artifact_streaming_response.py`, REST/Jupyter adapter regressions and
`test_harness/jupyter/extension/tests/test_file_download.py`.

## Real HTTP / Jupyter verification

```shell
RUN_ARTIFACT_DOWNLOAD_LIVE=1 uv run pytest -q -s tests/test_artifact_download_live.py
```

Five consecutive runs passed all three cases (15 case runs). The fixture loads the checkout's
extension into a disposable `executor-jupyter:local` container, without retagging/restarting the
Compose Jupyter service. This validates the changed extension code; rollout still requires a
fresh image build.

| Case | Observed result |
|---|---|
| basic / Python 3.11 | 7 executed code cells including a real Matplotlib PNG; notebook 34,547 bytes |
| ml / Python 3.12 | Same 7-cell execution and image validation; notebook 34,547 bytes |
| Report appended through REST | 8-cell notebook; 36,694 bytes (basic) / 36,691 bytes (ml) |
| Manual edit through standard Contents API | Saved a 192-byte notebook; full download matched |
| Five Range parts through BFF | Reassembled bytes and whole-file SHA-256 matched each current notebook |
| Binary download during path replacement | 8,388,611 original bytes delivered; next request read replacement |
| Same-size overwrite / suffix / open end / empty / missing file | Current bytes/headers or explicit error; no stale-size truncation |

A real test caught a setup-time mismatch immediately after truncating a host bind-mounted file
to zero bytes: metadata indicated remaining bytes but the read reached EOF. It was rejected as
`409`, not returned as a corrupted `200`. The bounded fresh-open setup path was added and covered
by deterministic tests (one change settles; continued changes fail; every descriptor closes).
The precise host/VM filesystem cache mechanism was not established; no cross-filesystem snapshot
guarantee is inferred from these tests.

Linux UID 1000 writer → UID 65534 reader/HTML renderer regression was separately run in a disposable
root-run container: **1 test passed**. Native Windows replacement/concurrent-save semantics were
not tested and are not promised to match POSIX behavior.

## Operational limits

- Each request hashes the full opened file before headers, then reads the requested bytes. This
  adds full-file I/O even for small ranges but avoids stale checksum headers and unbounded RAM.
- BFF and Istio buffering/timeouts/pool capacity require deployment-specific validation.
- Multiple Range requests are independent reads, not a shared download session; clients compare
  ETag/total size and validate the assembled checksum.
- No background file watcher, historical file-version service or immutable snapshot copy was added.
- Agent/BFF integration contract: [`dev_docs/get-artifact-content.md`](../dev_docs/get-artifact-content.md).
