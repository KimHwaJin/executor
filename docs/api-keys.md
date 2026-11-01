# API keys

Every Executor API call sends `X-Api-Key`. Executor keeps only each key's SHA-256 in
`AUTH_CLIENTS` of `config.{dev,stg,prd}.yml`; the key itself lives with its caller. Use
different keys in each environment.

## Callers

| `AUTH_CLIENTS` name | Key holder | Setting on the caller | Can call |
|---|---|---|---|
| `pr-agentic-service` | pr-agentic-service | `EXECUTOR_API_KEY` | Execution API; default tenant `dtest-agent-service`, any tenant through `X-Tenant-Id` |
| `pr-batch` | pr-batch | `EXECUTOR_API_KEY` | Execution API; no default tenant, so every call sends `X-Tenant-Id` |
| `ops` | pr-agentic-service | `EXECUTOR_ADMIN_API_KEY` | Admin API only: Runtime Targets and Pools, maintenance (drain/activate), `/workerz` |

pr-agentic-service uses the `ops` key only on its administrator screens and checks the
administrator role itself; Executor does not know which person pressed the button. Its other
calls, administrators' included, use its own key. Keeping two keys means a bug on a user path
cannot reach the admin API, and the write log tells `client=ops` apart.

## Generate

```bash
uv run python scripts/generate_api_keys.py prd
```

It prints a fresh key and its hash for every caller in `config.prd.yml`. Store the output in a
password manager, not in a file, ticket or chat. Nothing is written to disk.

1. Put each hash in that caller's `key_sha256` in `config.prd.yml`. Hashes are safe to commit;
   the same file's database and Redis passwords are not, so fill those outside Git.
2. Put each key in the caller's setting from the table above.
3. Rebuild and deploy the images of all three services for that environment
   (`--build-arg APP_ENV=prd`); settings ship inside the images.
4. Check with the `ops` key: `/workerz` answers 200. The other keys answer 403 there, and a
   missing or unknown key 401.

To hash a key you already have, pipe it in so it stays out of the shell history:
`pbpaste | uv run python scripts/generate_api_keys.py --hash`.
Without the repository: `printf %s "$KEY" | shasum -a 256` (`sha256sum` on Linux). Use
`printf %s`, not `echo`: a trailing newline changes the hash.

## Rotate

1. `uv run python scripts/generate_api_keys.py prd ops` for the caller being rotated.
2. List both hashes, `key_sha256: ["<new>", "<old>"]`, and deploy Executor.
3. Switch the caller to the new key and deploy it.
4. Remove the old hash and deploy Executor again.

A leaked key is revoked by removing its hash and redeploying Executor.

## Local

`docker-compose.yml` and `.env.example` carry local-only keys in their comments, so local runs
need no generated key. Those keys are not secrets and never belong in a deployed config.
