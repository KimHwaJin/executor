"""Generate an Executor API key and its AUTH_CLIENTS entry.

    python3 scripts/generate_api_keys.py prd ops --admin
    python3 scripts/generate_api_keys.py prd pr-agentic-service --tenant dtest-agent-service --relay '*'
    python3 scripts/generate_api_keys.py prd pr-batch --relay '*'
    python3 scripts/generate_api_keys.py prd report-service --tenant report
    pbpaste | python3 scripts/generate_api_keys.py --hash

Prints the entry to paste into config.{env}.yml and the key for the caller;
nothing is written to disk. Standard library only, so any python3 runs it.
See docs/api-keys.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys

ENVIRONMENTS = ("dev", "stg", "prd")
# Same rules as executor_service.domain.tenants; tests keep them in step.
TENANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")
RESERVED_TENANT_IDS = ("dlq", "work")
ANY_TENANT = "*"


def key_sha256(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(env: str, name: str) -> str:
    # The prefix only helps people tell keys apart; the random part is the secret.
    return f"exe_{env}_{name}_{secrets.token_urlsafe(32)}"


def client_entry(
    name: str,
    digest: str,
    *,
    admin: bool,
    tenant: str | None,
    relay: list[str],
) -> list[str]:
    lines = [f"  - name: {name}", f'    key_sha256: ["{digest}"]']
    if tenant is not None:
        lines.append(f"    tenant: {tenant}")
    if relay:
        lines.append("    relay: true")
        lines.append(f"    allowed_tenants: {json.dumps(relay)}")
    if admin:
        lines.append("    admin: true")
    return lines


def _tenant(value: str) -> str:
    if not TENANT_ID.fullmatch(value) or value in RESERVED_TENANT_IDS:
        raise argparse.ArgumentTypeError(
            f"{value!r} must match {TENANT_ID.pattern} and not be dlq or work"
        )
    return value


def _relay(value: str) -> list[str]:
    if value == ANY_TENANT:
        return [ANY_TENANT]
    return [_tenant(tenant.strip()) for tenant in value.split(",")]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="An admin key calls only the admin API; a service key calls "
        "the Execution API for its tenant, or for --relay tenants through X-Tenant-Id.",
    )
    parser.add_argument("env", nargs="?", choices=ENVIRONMENTS)
    parser.add_argument("name", nargs="?", help="caller name, 1-64 characters")
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument(
        "--admin",
        action="store_true",
        help="admin key: Runtime Targets and Pools, maintenance, /workerz",
    )
    parser.add_argument(
        "--tenant",
        type=_tenant,
        help="service key: tenant used without X-Tenant-Id",
    )
    kind.add_argument(
        "--relay",
        type=_relay,
        default=[],
        metavar="TENANTS",
        help="service key: tenants it may name in X-Tenant-Id, comma-separated, or '*'",
    )
    parser.add_argument(
        "--hash",
        action="store_true",
        help="print the SHA-256 of a key read from stdin",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.hash:
        print(key_sha256(sys.stdin.read().strip()))
        return
    if args.env is None or args.name is None:
        parser.error("env and name are required unless --hash is given")
    if not 1 <= len(args.name) <= 64 or any(c.isspace() for c in args.name):
        parser.error("name must be 1-64 characters without spaces")
    if args.admin and args.tenant is not None:
        parser.error(
            "an admin key takes no tenant; give the caller a separate service key"
        )
    if not args.admin and args.tenant is None and not args.relay:
        parser.error(
            "give --admin, or --tenant and/or --relay for a service key"
        )

    key = new_key(args.env, args.name)
    print(f"# Add to AUTH_CLIENTS in config.{args.env}.yml (safe to commit).")
    print(
        "# Rotating? Put this hash next to the old one in that caller's key_sha256."
    )
    print(
        "\n".join(
            client_entry(
                args.name,
                key_sha256(key),
                admin=args.admin,
                tenant=args.tenant,
                relay=args.relay,
            )
        )
    )
    print(
        f"\n# Key for {args.name}: send as X-Api-Key. Shown once; never commit or paste it."
    )
    print(key)


if __name__ == "__main__":
    main()
