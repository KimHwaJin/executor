"""Build the physical ERD and the table definition workbook from the ORM models.

Run from the repository root:
    uv run --with openpyxl python scripts/deliverables/schema.py
Outputs go to docs/deliverables/. Do not edit them by hand; change the models
(and column_descriptions.py) and run this again.
"""

from pathlib import Path

from column_descriptions import AUDIT, COMMON, DESC, TABLES
from drawio import Diagram
from openpyxl import Workbook
from openpyxl.styles import Font
from sheets import lay_out, write_grid
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql

import executor_service.infrastructure.db.models  # noqa: F401
from executor_service.infrastructure.db.base import Base

OUT = Path("docs/deliverables")
DIALECT = postgresql.dialect()

AREA_KIND = {
    "실행 요청": "service",
    "실행 시도": "caller",
    "인계": "note",
    "결과": "runtime",
    "이벤트": "store",
    "Runtime": "platform",
    "운영": "warn",
    "멱등": "plain",
}
# Physical ERD columns, left to right; tables stack top to bottom.
ERD_COLUMNS = [
    [
        "runtime_targets",
        "runtime_target_purges",
        "command_receipts",
        "event_retention_lease",
    ],
    [
        "maintenance_runs",
        "maintenance_run_targets",
        "executor_maintenance",
        "execution_retries",
        "execution_handovers",
        "execution_event_sequences",
    ],
    ["executions"],
    ["execution_operations", "execution_attempts", "execution_diagnostics"],
    ["execution_steps", "execution_step_attempts", "execution_step_runs"],
    ["execution_events", "outbox_events", "execution_artifacts"],
]
TABLE_WIDTH, COLUMN_GAP, ROW_GAP, ROW_HEIGHT, TOP = 330, 110, 50, 18, 90


def sql_type(column) -> str:
    return column.type.compile(DIALECT).replace(
        "TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ"
    )


def default(column) -> str:
    if column.server_default is not None:
        return str(column.server_default.arg)
    if column.default is not None:
        arg = column.default.arg
        if callable(arg):
            return "앱 생성"
        return str(arg.value if hasattr(arg, "value") else arg)
    return ""


def describe(table: Table, column) -> str:
    if column.name in AUDIT:
        return AUDIT_DESC[column.name]
    text = DESC.get(table.name, {}).get(column.name) or COMMON.get(column.name)
    if not text:
        raise SystemExit(
            f"column_descriptions.py에 {table.name}.{column.name} 설명이 없다"
        )
    return text


AUDIT_DESC = {
    "created_by_client": "행을 만든 호출 클라이언트",
    "created_by": "행을 만든 사용자",
    "updated_by_client": "마지막으로 고친 호출 클라이언트",
    "updated_by": "마지막으로 고친 사용자",
    "created_at": "생성 시각",
    "updated_at": "마지막 변경 시각",
}


def key_marks(table: Table, column) -> str:
    unique = {
        c.name
        for con in table.constraints
        if isinstance(con, UniqueConstraint)
        for c in con.columns
    }
    marks = [
        m
        for m, on in (
            ("PK", column.primary_key),
            ("FK", bool(column.foreign_keys)),
            ("UK", column.name in unique),
        )
        if on
    ]
    return ",".join(marks)


def physical_erd(tables: dict[str, Table]) -> None:
    missing = set(tables) - {name for column in ERD_COLUMNS for name in column}
    if missing:
        raise SystemExit(f"ERD_COLUMNS에 없는 테이블: {sorted(missing)}")
    diagram = Diagram("물리 ERD", 2800, 1600)
    rows: dict[tuple[str, str], str] = {}
    place: dict[str, float] = {}
    middle: dict[tuple[str, str], float] = {}
    for index, names in enumerate(ERD_COLUMNS):
        x, y = 40 + index * (TABLE_WIDTH + COLUMN_GAP), TOP
        for name in names:
            place[name] = x
            table = tables[name]
            columns = [c for c in table.columns if c.name not in AUDIT]
            labels = [
                f"{key_marks(table, c) or '&nbsp;&nbsp;'}&nbsp; <b>{c.name}</b> "
                f"<font color='#666666'>{sql_type(c)}{'' if c.nullable else ' NN'}</font>"
                for c in columns
            ]
            audit = len(table.columns) - len(columns)
            if audit:
                labels.append(f"<i>+ 감사 컬럼 {audit}개</i>")
            cells = diagram.table(
                f"{name} ({TABLES[name][0]})",
                labels,
                x,
                y,
                TABLE_WIDTH,
                AREA_KIND[TABLES[name][1]],
                ROW_HEIGHT,
            )
            for position, column in enumerate(columns):
                rows[name, column.name] = cells[position]
                middle[name, column.name] = (
                    y + 26 + ROW_HEIGHT * (position + 0.5)
                )
            y += 26 + ROW_HEIGHT * len(labels) + ROW_GAP
    # A foreign key to a table more than one column away would cut across the
    # tables between them, so it travels along a lane above the tables instead.
    lanes: dict[str, float] = {}
    for table in tables.values():
        for constraint in table.foreign_key_constraints:
            column = next(iter(constraint.columns))
            target = next(iter(constraint.elements)).column
            style = (
                "endFill=0;startFill=0;strokeColor=#555555;startArrow=ERzeroToMany;"
                f"endArrow={'ERzeroToOne' if column.nullable else 'ERmandOne'};"
            )
            source_x, target_x = place[table.name], place[target.table.name]
            if abs(source_x - target_x) <= TABLE_WIDTH + COLUMN_GAP:
                diagram.edge(
                    rows[table.name, column.name],
                    rows[target.table.name, target.name],
                    style="edgeStyle=entityRelationEdgeStyle;" + style,
                )
                continue
            lane = lanes.setdefault(
                target.table.name, TOP - 20 - 12 * len(lanes)
            )
            diagram.edge(
                rows[table.name, column.name],
                rows[target.table.name, target.name],
                style="exitX=0;exitY=0.5;entryX=1;entryY=0.5;" + style,
                points=[
                    (source_x - 30, middle[table.name, column.name]),
                    (source_x - 30, lane),
                    (target_x + TABLE_WIDTH + 30, lane),
                    (
                        target_x + TABLE_WIDTH + 30,
                        middle[target.table.name, target.name],
                    ),
                ],
            )
    legend_y = TOP
    for area, kind in AREA_KIND.items():
        diagram.node(
            area, 40 + 6 * (TABLE_WIDTH + COLUMN_GAP), legend_y, 120, 26, kind
        )
        legend_y += 34
    diagram.save(OUT / "diagrams" / "05-물리-ERD.drawio")


def constraint_rows(table: Table) -> list[list[str]]:
    result: list[list[str]] = []
    for con in sorted(table.constraints, key=lambda c: str(c.name or "")):
        name = str(con.name)
        if isinstance(con, UniqueConstraint):
            columns = ", ".join(c.name for c in con.columns)
            result.append(["UNIQUE", name, columns, ""])
        elif isinstance(con, ForeignKeyConstraint):
            result.append(
                [
                    "FK",
                    name,
                    ", ".join(c.name for c in con.columns),
                    f"→ {con.referred_table.name} (ON DELETE {con.ondelete or 'NO ACTION'})",
                ]
            )
        elif isinstance(con, CheckConstraint):
            result.append(["CHECK", name, "", str(con.sqltext)])
    for index in sorted(table.indexes, key=lambda i: str(i.name)):
        columns = ", ".join(str(e).split(".")[-1] for e in index.expressions)
        where = index.dialect_options["postgresql"].get("where")
        result.append(
            [
                "UNIQUE INDEX" if index.unique else "INDEX",
                str(index.name),
                columns,
                f"WHERE {where}" if where is not None else "",
            ]
        )
    return result


def table_workbook(tables: dict[str, Table], revision: str) -> None:
    book = Workbook()
    summary = book.active
    summary.title = "테이블 목록"
    summary["A1"] = "Executor Table 정의서"
    summary["A1"].font = Font(bold=True, size=14)
    summary["A2"] = (
        f"DBMS: PostgreSQL / 스키마 리비전(Alembic): {revision} / Vector DB: 사용하지 않음"
    )
    ordered = [name for name in TABLES if name in tables]
    write_grid(
        summary,
        4,
        [
            "No",
            "테이블 ID",
            "테이블명",
            "업무 영역",
            "1건이 뜻하는 것",
            "컬럼 수",
            "정리 정책",
        ],
        [
            [
                i,
                name,
                *TABLES[name][:3],
                len(tables[name].columns),
                TABLES[name][3],
            ]
            for i, name in enumerate(ordered, start=1)
        ],
    )
    lay_out(summary, (5, 30, 18, 12, 44, 8, 16))
    for name in ordered:
        table = tables[name]
        sheet = book.create_sheet(name[:31])
        sheet["A1"] = f"{name} ({TABLES[name][0]})"
        sheet["A1"].font = Font(bold=True, size=13)
        sheet["A2"] = (
            f"{TABLES[name][1]} 영역. {TABLES[name][2]}. 정리 정책: {TABLES[name][3]}"
        )
        after = write_grid(
            sheet,
            4,
            ["No", "컬럼 ID", "타입", "NULL", "키", "기본값", "설명"],
            [
                [
                    i,
                    c.name,
                    sql_type(c),
                    "Y" if c.nullable else "N",
                    key_marks(table, c),
                    default(c),
                    describe(table, c),
                ]
                for i, c in enumerate(table.columns, start=1)
            ],
        )
        write_grid(
            sheet,
            after,
            ["No", "제약·인덱스 이름", "종류", "", "", "컬럼", "내용"],
            [
                [i, name_, kind, "", "", columns, detail]
                for i, (kind, name_, columns, detail) in enumerate(
                    constraint_rows(table), start=1
                )
            ],
        )
        lay_out(sheet, (5, 44, 24, 6, 8, 30, 70))
    other = book.create_sheet("부록 RDB 외 저장소")
    other["A1"] = "RDB 밖에 있지만 DB 행이 가리키거나 서비스가 주고받는 데이터"
    other["A1"].font = Font(bold=True, size=13)
    other["A2"] = "Vector DB는 사용하지 않는다."
    write_grid(
        other,
        4,
        ["No", "저장소", "위치 · 필드", "타입", "", "", "설명"],
        [[i, *row] for i, row in enumerate(OTHER_STORES, start=1)],
    )
    lay_out(other, (5, 22, 76, 14, 3, 3, 70))
    book.save(OUT / "dist" / "06-Table-정의서.xlsx")


STREAM = "Redis Stream executor.events.<tenant>"
SHARED = "공유 PV <STORAGE_ROOT>/executor/executions/<execution_id>/"
JUPYTER = "Jupyter PV tenants/<tenant>/users/<user>/projects/<project>/sessions/<session>/executions/<execution_id>/"
OTHER_STORES = [
    [STREAM, "event_id", "string(uuid)", "", "", "이벤트 ID. 중복 제거 키"],
    [STREAM, "event_type", "string", "", "", "이벤트 종류 8개 중 하나"],
    [STREAM, "schema_version", "string", "", "", "1.0"],
    [STREAM, "execution_id", "string(uuid)", "", "", "Execution ID"],
    [
        STREAM,
        "event_sequence",
        "숫자 문자열",
        "",
        "",
        "Execution별 순번(1부터)",
    ],
    [STREAM, "payload", "JSON 문자열", "", "", "종류별 본문"],
    [STREAM, "occurred_at", "ISO-8601", "", "", "발생 시각"],
    [STREAM, "tenant_id", "string", "", "", "Stream의 테넌트"],
    [
        SHARED,
        "operations/<operation_id>/sources/<step_id>.py",
        "파일",
        "",
        "",
        "소스 스냅샷. execution_steps.source_snapshot_path가 가리킨다",
    ],
    [
        SHARED,
        "operations/<operation_id>/steps/<step_id>/attempts/<attempt_id>/<fencing_token>.partial/",
        "디렉터리",
        "",
        "",
        "쓰는 중인 결과. Worker 하나만 쓴다",
    ],
    [
        SHARED,
        ".../attempts/<attempt_id>/<fencing_token>/manifest.json",
        "파일",
        "",
        "",
        "확정된 Step 결과 목록. execution_step_attempts.result_manifest_path가 가리킨다. 불변",
    ],
    [
        SHARED,
        ".../attempts/<attempt_id>/<fencing_token>/outputs/",
        "디렉터리",
        "",
        "",
        "출력 파일. manifest가 경로, 크기, sha256을 가진다",
    ],
    [
        JUPYTER,
        "notebooks/execution.ipynb",
        "파일",
        "",
        "",
        "실행 노트북. executions.notebook_path",
    ],
    [
        JUPYTER,
        "artifacts/",
        "디렉터리",
        "",
        "",
        "산출물. execution_artifacts.relative_path",
    ],
    [
        JUPYTER,
        "artifacts/manifest.jsonl",
        "파일",
        "",
        "",
        "코드가 선언한 산출물 목록",
    ],
]


def main() -> None:
    tables = dict(Base.metadata.tables)
    if set(tables) != set(TABLES):
        raise SystemExit(
            f"column_descriptions.TABLES와 모델이 다르다: {set(tables) ^ set(TABLES)}"
        )
    revision = max(
        p.name[:4] for p in Path("migrations/versions").glob("0*.py")
    )
    physical_erd(tables)
    table_workbook(tables, revision)


if __name__ == "__main__":
    main()
