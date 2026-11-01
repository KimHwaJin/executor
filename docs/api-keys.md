# API Keys

Every API call sends `X-Api-Key`. Executor stores only each key's SHA-256 in `AUTH_CLIENTS`, so a
key is any random string the operator generates; there is no issuing API.

## Callers

| Caller | Used for | Key is held by |
|---|---|---|
| `pr-agentic-service` | Execution API, any tenant through `X-Tenant-Id` | `pr-agentic-service` `EXECUTOR_API_KEY` |
| `pr-batch` | Execution API, tenant sent per request | `pr-batch` `EXECUTOR_API_KEY` |
| `ops` (admin) | `/workerz`, `/runtime-targets*`, `/runtime-pools`, `/maintenance*` | `pr-agentic-service` `EXECUTOR_ADMIN_API_KEY` |

`pr-agentic-service` holds two keys on purpose. It sends `ops` only on the runtime and maintenance
APIs, after its own admin check, so a mistake on its general path still gets `403` from Executor.

## Generate keys

Run once per environment; keys are never shared between environments.

```bash
python3 scripts/generate_api_keys.py prd
```

For each caller this prints the hash and the key:

1. Put the hash in that caller's `key_sha256` in Executor's `config.prd.yml`.
2. Put the key in the setting the output names, in that service's `config.prd.yml`.
3. Rebuild and deploy each service: these files ship in its image.

The key is shown only once and cannot be recovered from the hash; a lost key is replaced. Do not
paste keys into chat, tickets or this repository's docs.

To hash an existing key, use `printf`, because `echo` would hash a trailing newline too:

```bash
printf %s "$KEY" | shasum -a 256
```

## Verify

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Api-Key: $OPS_KEY" http://<executor-host>/workerz
```

The `ops` key gets `200`, any other valid key `403`, and an unknown or missing key `401`.

## Rotate

1. `python3 scripts/generate_api_keys.py prd --caller ops`
2. Add the new hash next to the old one, `key_sha256: ["<new>", "<old>"]`, and deploy Executor.
3. Switch the caller to the new key and deploy it.
4. Remove the old hash and deploy Executor.

## Local

`docker-compose.yml` already defines local-only keys, listed in its comments; generate nothing.
