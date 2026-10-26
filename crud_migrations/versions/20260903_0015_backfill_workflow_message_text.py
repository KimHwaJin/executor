"""Backfill user-facing Workflow agent message text.

Revision ID: 20260903_0015
Revises: 20260903_0014
Create Date: 2026-09-03
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260903_0015"
down_revision: Union[str, None] = "20260903_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 원본 JSON은 content/metadata에 유지하고 이미 적재된 화면용 문장만 보정한다.
    op.execute(
        """
        UPDATE messages
           SET content_text = COALESCE(
               ((content->0->>'text')::jsonb #>> '{result,no_match_reason}'),
               '조건에 맞는 기존 Workflow가 없어 새 Workflow를 생성합니다.'
           )
         WHERE metadata->>'graph_name' = 'workflow_recommender'
           AND content_text = '현재 상태: candidate_search_complete'
        """
    )
    op.execute(
        """
        UPDATE messages
           SET content_text =
               CASE WHEN ((content->0->>'text')::jsonb)->>'origin' = 'generated'
                    THEN '새로 생성한'
                    ELSE '추천된'
               END
               || ' Workflow 후보 `'
               || COALESCE(((content->0->>'text')::jsonb)->>'candidate_id', '새 후보')
               || '`를 목록에 추가했습니다. 상태: '
               || CASE ((content->0->>'text')::jsonb)->>'workflow_status'
                    WHEN 'needs_input' THEN '추가 정보 필요'
                    WHEN 'ready' THEN '승인 준비 완료'
                    ELSE COALESCE(((content->0->>'text')::jsonb)->>'workflow_status', '알 수 없음')
                  END
               || '.'
         WHERE metadata->>'graph_name' = 'workflow_candidate_collector'
           AND content_text = '현재 상태: candidate_added'
        """
    )


def downgrade() -> None:
    # 화면용 projection은 손실 가능한 파생값이므로 과거의 모호한 문구로 되돌리지 않는다.
    pass

