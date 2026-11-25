"""Draw the Execution state diagram from the transition table.

Run from the repository root:
    uv run python scripts/deliverables/states.py
"""

from pathlib import Path

from drawio import Diagram

from executor_service.domain.enums import ExecutionStatus as S
from executor_service.domain.transitions import EXECUTION_TRANSITIONS

OUT = Path("docs/deliverables/diagrams/03-상태-전이도.drawio")

# Where each state sits; a state missing here stops the script.
PLACE = {
    S.QUEUED: (420, 90),
    S.FINALIZING: (1000, 90),
    S.SUCCEEDED: (40, 300),
    S.RUNNING: (420, 300),
    S.WAITING_FOR_OPERATION: (1000, 300),
    S.CANCEL_REQUESTED: (420, 580),
    S.FAILED: (1000, 580),
    S.CANCELLED: (420, 770),
}
# Hand routing: (exit x, exit y, entry x, entry y, waypoints). A transition
# missing here is drawn as a straight line, which is the cue to route it.
ROUTE: dict[
    tuple[S, S], tuple[float, float, float, float, list[tuple[float, float]]]
] = {
    (S.QUEUED, S.RUNNING): (0.3, 1, 0.3, 0, []),
    (S.RUNNING, S.QUEUED): (0.7, 0, 0.7, 1, []),
    (S.QUEUED, S.CANCEL_REQUESTED): (0, 0.5, 0, 0.5, [(392, 125), (392, 615)]),
    (S.QUEUED, S.FAILED): (1, 0.3, 0, 0.2, [(720, 111), (720, 594)]),
    (S.RUNNING, S.SUCCEEDED): (0, 0.5, 1, 0.5, []),
    (S.RUNNING, S.FAILED): (1, 0.8, 0, 0.8, [(680, 356), (680, 636)]),
    (S.RUNNING, S.WAITING_FOR_OPERATION): (1, 0.2, 0, 0.2, []),
    (S.RUNNING, S.CANCEL_REQUESTED): (0.5, 1, 0.5, 0, []),
    (S.WAITING_FOR_OPERATION, S.QUEUED): (
        0,
        0.8,
        1,
        0.7,
        [(800, 356), (800, 139)],
    ),
    (S.WAITING_FOR_OPERATION, S.FINALIZING): (0.7, 0, 0.7, 1, []),
    (S.WAITING_FOR_OPERATION, S.CANCEL_REQUESTED): (
        0.2,
        1,
        0.75,
        0,
        [(1044, 440), (585, 440)],
    ),
    (S.WAITING_FOR_OPERATION, S.FAILED): (0.6, 1, 0.6, 0, []),
    (S.FINALIZING, S.RUNNING): (0, 0.5, 1, 0.5, [(960, 125), (960, 335)]),
    (S.FINALIZING, S.CANCEL_REQUESTED): (
        1,
        0.5,
        0.8,
        1,
        [(1290, 125), (1290, 700), (596, 700)],
    ),
    (S.FINALIZING, S.FAILED): (1, 0.8, 1, 0.3, [(1260, 146), (1260, 601)]),
    (S.CANCEL_REQUESTED, S.CANCELLED): (0.5, 1, 0.5, 0, []),
    (S.CANCEL_REQUESTED, S.FAILED): (1, 0.5, 0, 0.5, []),
    (S.FAILED, S.QUEUED): (
        0.35,
        0,
        0.9,
        1,
        [(1077, 490), (760, 490), (760, 200), (618, 200)],
    ),
}
# Why a transition happens and where the note sits (its centre). The note
# is its own cell because a label left to the edge lands on other lines.
# A transition without a note is drawn unlabeled.
WHY = {
    (S.QUEUED, S.RUNNING): ("Worker claim", 486, 215),
    (S.QUEUED, S.CANCEL_REQUESTED): ("취소 요청", 392, 470),
    (S.QUEUED, S.FAILED): ("QUEUE_TIMEOUT 등", 900, 594),
    (S.RUNNING, S.SUCCEEDED): ("모든 Step 성공", 325, 335),
    (S.RUNNING, S.FAILED): ("Step 실패 · 시간 초과 · 장애", 900, 636),
    (S.RUNNING, S.WAITING_FOR_OPERATION): ("MULTI Operation 완료", 885, 314),
    (S.RUNNING, S.CANCEL_REQUESTED): ("취소 요청", 530, 500),
    (S.RUNNING, S.QUEUED): ("보존 세션 재시도가<br>Target을 기다림", 574, 255),
    (S.WAITING_FOR_OPERATION, S.QUEUED): ("다음 Operation 추가", 800, 180),
    (S.WAITING_FOR_OPERATION, S.FINALIZING): ("finalize", 1154, 230),
    (S.WAITING_FOR_OPERATION, S.CANCEL_REQUESTED): ("취소 요청", 900, 440),
    (S.WAITING_FOR_OPERATION, S.FAILED): ("대기 시간 초과", 1132, 530),
    (S.FINALIZING, S.RUNNING): ("정리 실행", 960, 215),
    (S.FINALIZING, S.CANCEL_REQUESTED): ("취소 요청", 900, 700),
    (S.FINALIZING, S.FAILED): ("정리 실패", 1260, 400),
    (S.CANCEL_REQUESTED, S.CANCELLED): ("Worker가 중단 확인", 530, 730),
    (S.CANCEL_REQUESTED, S.FAILED): ("최대 수명 초과로 중지", 830, 615),
    (S.FAILED, S.QUEUED): ("SINGLE 재시도", 900, 490),
}
WIDTH, HEIGHT = 220, 70


def main() -> None:
    diagram = Diagram("Execution 상태 전이", 1340, 940)
    diagram.text(
        "<b>Execution 상태 전이도</b> (원본: domain/transitions.py)",
        20,
        10,
        700,
        28,
        "fontSize=15;align=left;",
    )
    cells = {}
    for state in S:
        terminal = not EXECUTION_TRANSITIONS.get(state)
        x, y = PLACE[state]
        cells[state] = diagram.node(
            f"<b>{state.value}</b>",
            x,
            y,
            WIDTH,
            HEIGHT,
            "store"
            if terminal
            else "warn"
            if state is S.FAILED
            else "service",
            "rounded=1;arcSize=40;" + ("strokeWidth=3;" if terminal else ""),
        )
    start = diagram.node(
        "", 517, 36, 26, 26, "plain", "ellipse;fillColor=#333333;"
    )
    diagram.edge(start, cells[S.QUEUED], "제출")
    for source, targets in EXECUTION_TRANSITIONS.items():
        for target in sorted(targets):
            exit_x, exit_y, entry_x, entry_y, points = ROUTE.get(
                (source, target), (0.5, 0.5, 0.5, 0.5, [])
            )
            diagram.edge(
                cells[source],
                cells[target],
                "",
                f"exitX={exit_x};exitY={exit_y};entryX={entry_x};entryY={entry_y};",
                points,
            )
    for note, x, y in WHY.values():
        diagram.text(
            note,
            x - 100,
            y - 16,
            200,
            32,
            "align=center;labelBackgroundColor=#ffffff;",
        )
    diagram.text(
        "굵은 테두리 = 종료 상태. FAILED는 SINGLE이고 재시도 가능할 때만 QUEUED로 돌아간다.<br>"
        "API와 Worker 모두 이 표로 전이를 검사한다(require_execution_transition).",
        20,
        870,
        800,
        40,
    )
    diagram.save(OUT)


if __name__ == "__main__":
    main()
