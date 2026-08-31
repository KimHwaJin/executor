"""Read-only probes inside the isolated interruption test containers.

The private .state.json check is test synchronization, not an Agent API.
Only sealed, DB-referenced manifests are used as terminal evidence.
"""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from executor_service.application.execution_results import (
    ExecutionResultQueryService,
)
from executor_service.infrastructure.db.models import ExecutionORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from executor_service.interfaces._contracts.results import (
    ExecutionResultResponse,
)


def checked_file(root: Path, ref: dict[str, Any]) -> bytes:
    path = (root / ref["relative_path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Result reference escaped its storage root")
    content = path.read_bytes()
    if len(content) != ref["size_bytes"]:
        raise ValueError("Result file size mismatch")
    if hashlib.sha256(content).hexdigest() != ref["checksum_sha256"]:
        raise ValueError("Result file checksum mismatch")
    return content


def progress(root: Path, execution_id: UUID) -> bool:
    for path in (root / "executions" / str(execution_id)).rglob(".state.json"):
        try:
            state = json.loads(path.read_bytes())
            if state["identity"]["sequence"] != 1:
                continue
            kinds = set()
            for output in state["outputs"]:
                for representation in output["representations"]:
                    body = checked_file(path.parent, representation)
                    media = representation["media_type"]
                    if media == "image/png" and body.startswith(b"\x89PNG"):
                        kinds.add("image")
                    if media == "text/plain" and b"before-interrupt" in body:
                        kinds.add("text")
            if kinds == {"image", "text"}:
                return True
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            # Atomic writer may have advanced/replaced files during this read.
            continue
    return False


def manifests(root: Path, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = []
    for operation in bundle["operations"]:
        for step in operation["steps"]:
            ref = step["result"]["result_ref"]
            if ref is None:
                continue
            if any(
                part.endswith(".partial")
                for part in Path(ref["relative_path"]).parts
            ):
                raise ValueError("A mutable partial directory was published")
            manifest = json.loads(checked_file(root, ref))
            identity = manifest["identity"]
            if (
                manifest["complete"] != ref["complete"]
                or identity["execution_id"] != ref["execution_id"]
                or identity["step_id"] != ref["step_id"]
                or identity["execution_attempt_id"] != ref["attempt_id"]
                or identity["fencing_token"] != ref["fencing_token"]
            ):
                raise ValueError("Manifest identity or completeness mismatch")
            checked_file(root, manifest["source"])
            directory = (root / ref["relative_path"]).parent
            text = []
            png_sizes = []
            for output in manifest["outputs"]:
                for representation in output["representations"]:
                    body = checked_file(directory, representation)
                    if representation["media_type"] == "text/plain":
                        text.append(body.decode())
                    if representation["media_type"] == "image/png":
                        if not body.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise ValueError("Invalid PNG evidence")
                        png_sizes.append(len(body))
            evidence.append(
                {
                    "step_id": step["step_id"],
                    "sequence": step["sequence"],
                    "ref": ref,
                    "state": manifest["state"],
                    "complete": manifest["complete"],
                    "error_message": manifest["error_message"],
                    "output_summary": manifest["output_summary"],
                    "text": "".join(text),
                    "png_sizes": png_sizes,
                }
            )
    return evidence


async def snapshot(execution_id: UUID, root: Path) -> dict[str, Any]:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            row = await session.get(ExecutionORM, execution_id)
            if row is None:
                raise ValueError("Execution missing")
            database = {
                "status": row.status.value,
                "lease_owner": row.lease_owner,
                "cancellation_lease_owner": row.cancellation_lease_owner,
                "runtime_session_id": row.runtime_session_id,
                "cleanup_status": row.runtime_session_cleanup_status.value,
            }
        bundle = ExecutionResultResponse.from_bundle(
            await ExecutionResultQueryService(
                SQLAlchemyExecutionQueryService(factory)
            ).execution(execution_id)
        ).model_dump(mode="json")
        messages = []
        for message_id, fields in await redis.xrange("executor.events"):
            if fields.get("execution_id") != str(execution_id):
                continue
            fields["payload"] = json.loads(fields["payload"])
            fields["event_sequence"] = int(fields["event_sequence"])
            messages.append({"stream_id": message_id, "event": fields})
        return {
            "database": database,
            "result": bundle,
            "files": manifests(root, bundle),
            "redis": messages,
        }
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    action, raw_id = sys.argv[1:]
    execution_id = UUID(raw_id)
    root = Path(os.environ["SHARED_STORAGE_ROOT"])
    if action == "progress":
        print(json.dumps({"ready": progress(root, execution_id)}))
    elif action == "snapshot":
        print(json.dumps(asyncio.run(snapshot(execution_id, root))))
    else:
        raise ValueError("Unknown read-only probe")
