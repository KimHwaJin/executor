# API Keys

Every API call sends `X-Api-Key`. Executor stores only each key's SHA-256 in `AUTH_CLIENTS`, so a
key is any random string the operator generates; there is no issuing API.

## Kinds

| Kind | Reaches | Entry in `AUTH_CLIENTS` |
|---|---|---|
| `master` | `/workerz`, `/runtime-targets*`, `/runtime-pools`, `/maintenance*`; not the Execution API, as it acts for no tenant | `admin: true` |
| `normal` | Execution API of any tenant through `X-Tenant-Id`; admin APIs get `403` | `relay: true`, `allowed_tenants: ["*"]` |

## Callers

A caller is the service that holds a key. Each has its own entry, so its key can be rotated or
revoked alone, and the name appears as `client=` in the log line of each write request. The name
grants nothing: the kind decides what a key reaches. Names are free text of 1-64 characters,
unique in `AUTH_CLIENTS`; `tests/test_environment_config.py` pins the three shipped ones.

| Caller | Kind | Key is held by |
|---|---|---|
| `pr-agentic-service` | `normal` | `pr-agentic-service` `EXECUTOR_API_KEY` |
| `pr-batch` | `normal` | `pr-batch` `EXECUTOR_API_KEY` |
| `ops` | `master` | `pr-agentic-service` `EXECUTOR_ADMIN_API_KEY` |

`pr-agentic-service` holds two keys on purpose. It sends `ops` only on the runtime and maintenance
APIs, after its own admin check, so a mistake on its general path still gets `403` from Executor.

## Generate keys

Run once per caller and environment; keys are never shared between environments.

```bash
uv run python scripts/generate_api_keys.py prd --kind normal --caller pr-agentic-service --tenant dtest-agent-service
uv run python scripts/generate_api_keys.py prd --kind normal --caller pr-batch
uv run python scripts/generate_api_keys.py prd --kind master --caller ops
```

`--tenant` sets the tenant used when a call sends no `X-Tenant-Id`. Each run prints an
`AUTH_CLIENTS` entry and the key:

1. Put the entry in Executor's `config.prd.yml`, replacing that caller's placeholder.
2. Put the key in the setting that holds it, in that service's `config.prd.yml`.
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

1. Generate a key for the same caller and kind.
2. Add its hash next to the old one, `key_sha256: ["<new>", "<old>"]`, and deploy Executor.
3. Switch the caller to the new key and deploy it.
4. Remove the old hash and deploy Executor.

## Local

`docker-compose.yml` already defines local-only keys, listed in its comments; generate nothing.
