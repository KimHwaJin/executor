from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.common.agent_run_log_model import AgentRunLogModel
from app.services.task_event_service import TaskEventService


class AgentRunLogService:
    """agent_run_logs 테이블의 멱등 append만 담당합니다."""

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        run_id: UUID,
        event_key: str,
        agent_name: str | None,
        node: str,
        event: str,
        kind: str,
        payload: dict,
    ) -> AgentRunLogModel:
        existing = await db.scalar(select(AgentRunLogModel).where(
            AgentRunLogModel.run_id == run_id,
            AgentRunLogModel.event_key == event_key,
        ))
        if existing is not None:
            return existing
        log = AgentRunLogModel(
            run_id=run_id, event_key=event_key, agent_name=agent_name,
            node=node, event=event, kind=kind, payload=payload,
        )
        db.add(log)
        try:
            await db.commit()
        except IntegrityError:
            # 동일 Graph event 재전송은 UNIQUE(run_id,event_key)로 한 로그에 수렴합니다.
            await db.rollback()
            duplicate = await db.scalar(select(AgentRunLogModel).where(
                AgentRunLogModel.run_id == run_id,
                AgentRunLogModel.event_key == event_key,
            ))
            if duplicate is not None:
                return duplicate
            raise
        await db.refresh(log)
        # E05-T05: 각 Agent 결과를 재연결 가능한 SSE Event Store에도 투영합니다.
        await TaskEventService.append_for_run(
            db,
            run_id=run_id,
            event_type="agent.event",
            payload={
                "agent_name": agent_name,
                "node": node,
                "event": event,
                "kind": kind,
                "payload": payload,
            },
        )
        return log

