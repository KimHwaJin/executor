"""Generate Executor API keys and their AUTH_CLIENTS hashes.

    uv run python scripts/generate_api_keys.py prd           # every caller in config.prd.yml
    uv run python scripts/generate_api_keys.py prd ops       # one caller, e.g. for rotation
    uv run python scripts/generate_api_keys.py --hash < key  # hash an existing key

Keys and hashes go to stdout only; nothing is written to disk. See
docs/api-keys.md for where each value goes.
"""

import argparse
import hashlib
import secrets
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENTS = ("dev", "stg", "prd")


def key_sha256(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(environment: str, client: str) -> str:
    # The prefix only helps a person tell keys apart; 32 random bytes carry the secret.
    return f"exe_{environment}_{client}_{secrets.token_urlsafe(32)}"


def configured_clients(environment: str) -> list[str]:
    config = yaml.safe_load((ROOT / f"config.{environment}.yml").read_text())
    return [client["name"] for client in config["AUTH_CLIENTS"]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("environment", nargs="?", choices=ENVIRONMENTS)
    parser.add_argument(
        "clients", nargs="*", help="default: every AUTH_CLIENTS entry"
    )
    parser.add_argument(
        "--hash",
        action="store_true",
        help="print the SHA-256 of a key read from stdin",
    )
    args = parser.parse_args()
    if args.hash:
        print(key_sha256(sys.stdin.read().strip()))
        return
    if args.environment is None:
        parser.error("environment is required unless --hash is given")

    known = configured_clients(args.environment)
    unknown = sorted(set(args.clients) - set(known))
    if unknown:
        parser.error(
            f"not in config.{args.environment}.yml AUTH_CLIENTS: {unknown}"
        )
    keys = {
        client: new_key(args.environment, client)
        for client in args.clients or known
    }

    print(
        f"# config.{args.environment}.yml AUTH_CLIENTS key_sha256 (safe to commit)"
    )
    for client, key in keys.items():
        print(f"{client}: {key_sha256(key)}")
    print(
        "\n# Keys: hand each to its caller out of band; never commit or paste them"
    )
    for client, key in keys.items():
        print(f"{client}: {key}")


if __name__ == "__main__":
    main()
