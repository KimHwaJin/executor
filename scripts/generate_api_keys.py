"""Print new Executor API keys and the SHA-256 hashes AUTH_CLIENTS stores."""

from __future__ import annotations

import argparse
import hashlib
import secrets

# Caller name in AUTH_CLIENTS -> (key prefix label, setting that holds the key).
CALLERS = {
    "pr-agentic-service": ("agent", "pr-agentic-service EXECUTOR_API_KEY"),
    "pr-batch": ("batch", "pr-batch EXECUTOR_API_KEY"),
    "ops": ("ops", "pr-agentic-service EXECUTOR_ADMIN_API_KEY"),
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env", choices=("dev", "stg", "prd"))
    parser.add_argument(
        "--caller",
        action="append",
        choices=tuple(CALLERS),
        help="Only this caller, as for a rotation. Repeatable. Default: all.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    for name in args.caller or CALLERS:
        label, destination = CALLERS[name]
        key = f"exe_{args.env}_{label}_{secrets.token_urlsafe(32)}"
        digest = hashlib.sha256(key.encode()).hexdigest()
        print(f"{name}")
        print(f"  config.{args.env}.yml key_sha256: {digest}")
        print(f"  {destination}: {key}")


if __name__ == "__main__":
    main()
