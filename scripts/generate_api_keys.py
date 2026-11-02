"""Print a new Executor API key and its AUTH_CLIENTS entry for one caller."""

from __future__ import annotations

import argparse
import hashlib
import secrets

import yaml
from pydantic import ValidationError

from executor_service.domain.tenants import ANY_TENANT
from executor_service.settings import AuthClient


def _parse_args() -> tuple[argparse.Namespace, argparse.ArgumentParser]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env", choices=("dev", "stg", "prd"))
    parser.add_argument(
        "--kind",
        required=True,
        choices=("master", "normal"),
        help="master: the admin APIs. normal: Executions of any tenant.",
    )
    parser.add_argument(
        "--caller", required=True, help="Caller name, shown in the logs."
    )
    parser.add_argument(
        "--tenant", help="Tenant used when a call sends no X-Tenant-Id."
    )
    return parser.parse_args(), parser


def main() -> None:
    args, parser = _parse_args()
    if args.kind == "master" and args.tenant:
        parser.error("A master key acts for no tenant; drop --tenant.")
    key = f"exe_{args.env}_{args.kind}_{secrets.token_urlsafe(32)}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    permissions = (
        {"admin": True}
        if args.kind == "master"
        else {"relay": True, "allowed_tenants": (ANY_TENANT,)}
    )
    try:
        client = AuthClient(
            name=args.caller,
            key_sha256=(digest,),
            tenant=args.tenant,
            **permissions,
        )
    except ValidationError as exc:
        parser.error(str(exc))
    entry = client.model_dump(mode="json", exclude_defaults=True)
    print(f"# Add under AUTH_CLIENTS in config.{args.env}.yml")
    print(yaml.safe_dump([entry], sort_keys=False, default_flow_style=None))
    print(f"# Key for {args.caller}; shown only once")
    print(key)


if __name__ == "__main__":
    main()
