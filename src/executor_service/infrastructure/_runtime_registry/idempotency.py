"""Runtime Target command fingerprints and durable receipts."""

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.errors import IdempotencyConflictError
from executor_service.infrastructure.db.models import CommandReceiptORM


class RuntimeCommandReceipts:
    async def repeated_result(
        self,
        session: AsyncSession,
        idempotency_key: str,
        command_type: str,
        request_fingerprint: str,
    ) -> UUID | None:
        receipt = await session.scalar(
            select(CommandReceiptORM).where(
                CommandReceiptORM.idempotency_key == idempotency_key
            )
        )
        if receipt is None:
            return None
        if (
            receipt.command_type != command_type
            or receipt.request_fingerprint != request_fingerprint
        ):
            raise IdempotencyConflictError(
                "idempotency_key was already used with a different command."
            )
        return UUID(receipt.result["target_id"])

    @staticmethod
    def add(
        session: AsyncSession,
        idempotency_key: str,
        command_type: str,
        request_fingerprint: str,
        target_id: UUID,
    ) -> None:
        session.add(
            CommandReceiptORM(
                idempotency_key=idempotency_key,
                command_type=command_type,
                request_fingerprint=request_fingerprint,
                result={"target_id": str(target_id)},
            )
        )


def fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def secret_hash(secret: str | None) -> str | None:
    if secret is None:
        return None
    return hashlib.sha256(secret.encode()).hexdigest()
