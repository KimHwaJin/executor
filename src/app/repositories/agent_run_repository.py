from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AgentRunStatus, DeleteYN, MessageStatus, MessageType
from app.models.common.agent_run_model import AgentRunModel
from app.models.common.message_model import MessageModel
from app.services.helpers import utc_now


class AgentRunRepository:
    """Pod Agent 전송부터 Redis 결과 해석까지의 영속화 경계."""

    @staticmethod
    async def get(db: AsyncSession, *, session_id: UUID, run_id: UUID) -> AgentRunModel | None:
        return await db.scalar(
            select(AgentRunModel).where(
                AgentRunModel.session_id == session_id,
                AgentRunModel.run_id == run_id,
            )
        )

    @staticmethod
    async def create_workflow(
        db: AsyncSession,
        *,
        session_id: UUID,
        trigger_message_id: UUID,
        idempotency_key: str,
        request_payload: dict,
        redis_key: str,
        metadata: dict | None = None,
    ) -> AgentRunModel:
        run = AgentRunModel(
            session_id=session_id,
            trigger_message_id=trigger_message_id,
            input_json={"messages": request_payload.get("messages", [])},
            command_json=None,
            metadata_json=metadata or {},
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            redis_key=redis_key,
            workflow_stage="prepared",
            status=AgentRunStatus.PENDING,
        )
        db.add(run)
        await db.flush()
        return run

    @staticmethod
    async def mark_dispatched(db: AsyncSession, run: AgentRunModel) -> None:
        run.workflow_stage = "dispatched"
        run.status = AgentRunStatus.RUNNING
        run.dispatched_at = utc_now()
        run.started_at = run.started_at or run.dispatched_at
        await db.flush()

    @staticmethod
    async def store_agent_result(
        db: AsyncSession,
        run: AgentRunModel,
        *,
        response: dict,
        agent_message_id: UUID | None = None,
    ) -> None:
        run.agent_response = response
        run.agent_message_id = agent_message_id
        run.workflow_stage = "agent_completed"
        run.agent_completed_at = utc_now()
        await db.flush()

    @staticmethod
    async def save_agent_message(
        db: AsyncSession,
        run: AgentRunModel,
        *,
        content_text: str,
        result: dict,
    ) -> MessageModel:
        """Agent의 직접 처리 결과를 tool Message로 저장합니다."""
        message = MessageModel(
            session_id=run.session_id,
            message_type=MessageType.TOOL,
            content=[{"type": "agent_result", "data": result}],
            content_text=content_text,
            message_status=MessageStatus.COMPLETED,
            metadata_json={"agent_run_id": str(run.run_id), "workflow_stage": "agent_completed"},
            delete_yn=DeleteYN.N,
        )
        db.add(message)
        await db.flush()
        await AgentRunRepository.store_agent_result(
            db, run, response=result, agent_message_id=message.message_id
        )
        return message

    @staticmethod
    async def store_redis_result(db: AsyncSession, run: AgentRunModel, *, result: dict) -> None:
        run.redis_result = result
        run.workflow_stage = "redis_received"
        run.redis_received_at = utc_now()
        await db.flush()

    @staticmethod
    async def mark_interpreting(db: AsyncSession, run: AgentRunModel) -> None:
        run.workflow_stage = "interpreting"
        await db.flush()

    @staticmethod
    async def complete(
        db: AsyncSession,
        run: AgentRunModel,
        *,
        interpreted_message_id: UUID,
    ) -> None:
        now = utc_now()
        run.interpreted_message_id = interpreted_message_id
        run.workflow_stage = "completed"
        run.status = AgentRunStatus.SUCCESS
        run.interpreted_at = now
        run.completed_at = now
        await db.flush()

    @staticmethod
    async def save_interpreted_message(
        db: AsyncSession,
        run: AgentRunModel,
        *,
        content_text: str,
        llm_run_id: UUID | None = None,
    ) -> MessageModel:
        """Redis 결과를 LLM이 해석한 최종 답변을 assistant Message로 저장합니다."""
        message = MessageModel(
            session_id=run.session_id,
            message_type=MessageType.ASSISTANT,
            content=[{"type": "text", "text": content_text}],
            content_text=content_text,
            message_status=MessageStatus.COMPLETED,
            metadata_json={
                "agent_run_id": str(run.run_id),
                "llm_run_id": str(llm_run_id) if llm_run_id else None,
                "workflow_stage": "completed",
            },
            delete_yn=DeleteYN.N,
        )
        db.add(message)
        await db.flush()
        await AgentRunRepository.complete(
            db, run, interpreted_message_id=message.message_id
        )
        return message

    @staticmethod
    async def fail(db: AsyncSession, run: AgentRunModel, *, code: str, message: str) -> None:
        run.workflow_stage = "failed"
        run.status = AgentRunStatus.ERROR
        run.failure = {"code": code, "message": message}
        run.completed_at = utc_now()
        await db.flush()

