# API Keys

Every API call sends `X-Api-Key`. Executor stores only each key's SHA-256 in `AUTH_CLIENTS` of
`config.{dev,stg,prd}.yml`, so a key is any random string the operator generates; there is no
issuing API. Use different keys in each environment.

## Callers

An `AUTH_CLIENTS` entry is one caller: a service or operator holding one key. Its `name` is a free
label (1-64 characters, unique); Executor uses it only in the write log (`client=<name>`), so name
callers after who holds the key. What a key may do comes from the other fields:

| Kind | Fields | Can call |
|---|---|---|
| Admin | `admin: true` | Admin API only: `/workerz`, `/runtime-targets*`, `/runtime-pools`, `/maintenance*` |
| Service | `tenant` and/or `relay` + `allowed_tenants` | Execution API for `tenant`, or for a tenant named in `X-Tenant-Id` when it is in `allowed_tenants` (`["*"]`: any) |

One caller per key holder keeps each key revocable and rotatable on its own and tells callers
apart in the log. The shipped callers:

| Caller | Kind | Key is held by |
|---|---|---|
| `pr-agentic-service` | Service, default tenant `dtest-agent-service`, relay `*` | `pr-agentic-service` `EXECUTOR_API_KEY` |
| `pr-batch` | Service, relay `*` (sends `X-Tenant-Id` on every call) | `pr-batch` `EXECUTOR_API_KEY` |
| `ops` | Admin | `pr-agentic-service` `EXECUTOR_ADMIN_API_KEY` |

`pr-agentic-service` holds two keys on purpose. It sends `ops` only on the runtime and maintenance
APIs, after its own admin check, so a mistake on its general path still gets `403` from Executor.

## Generate a key

`scripts/generate_api_keys.py ENV NAME KIND` prints the `AUTH_CLIENTS` entry and the key. It needs
only `python3`; nothing is written to disk.

```bash
python3 scripts/generate_api_keys.py prd ops --admin
python3 scripts/generate_api_keys.py prd pr-agentic-service --tenant dtest-agent-service --relay '*'
python3 scripts/generate_api_keys.py prd pr-batch --relay '*'
python3 scripts/generate_api_keys.py prd report-service --tenant report          # a new caller
python3 scripts/generate_api_keys.py prd gateway --relay tenant-a,tenant-b       # listed tenants
```

1. Paste the entry into `AUTH_CLIENTS` in Executor's `config.prd.yml`, replacing that caller's
   placeholder. Hashes are safe to commit; the file's database and Redis passwords are not.
2. Give the key to the caller for its `X-Api-Key` setting. It is shown once and cannot be recovered
   from the hash; a lost key is replaced. Do not paste keys into chat, tickets or this repository.
3. Rebuild and deploy Executor (`--build-arg APP_ENV=prd`) and the caller: settings ship in images.

The script refuses entries Executor would refuse (bad tenant names, `dlq`/`work`, `*` mixed with
names) and an admin key with a tenant: give such a caller a separate service key instead.
`tests/test_environment_config.py` checks the shipped caller names; update it when adding one.

To hash a key you already have, pipe it in so it stays out of the shell history:
`pbpaste | python3 scripts/generate_api_keys.py --hash`, or `printf %s "$KEY" | shasum -a 256`
(`echo` would hash a trailing newline too).

## Verify

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Api-Key: $OPS_KEY" http://<executor-host>/workerz
```

An admin key gets `200`, a service key `403`, and an unknown or missing key `401`.

## Rotate

1. Run the same command for the caller and copy only the new hash.
2. Add it next to the old one, `key_sha256: ["<new>", "<old>"]`, and deploy Executor.
3. Switch the caller to the new key and deploy it.
4. Remove the old hash and deploy Executor.

A leaked key is revoked by removing its hash and redeploying Executor.

## Local

`docker-compose.yml` already defines local-only keys, listed in its comments; generate nothing.
