"""Build the program list workbook.

REST rows come from the running application's routes, so a new endpoint cannot
be missed; their Korean names come from dev_docs/api-reference. Workers,
background passes, events and tools are listed by hand below.

Run from the repository root:
    uv run --with openpyxl python scripts/deliverables/programs.py
"""

import re
from collections.abc import Sequence
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from sheets import lay_out, write_grid

from executor_service.container import ApplicationContainer
from executor_service.interfaces.http.app import create_app
from executor_service.settings import Settings

OUT = Path("docs/deliverables/dist/02-프로그램-목록.xlsx")
REFERENCE = Path("dev_docs/api-reference")
SRC = "src/executor_service/"
WORKER = SRC + "infrastructure/execution_worker/"
EXTENSION = "deploy/jupyter/extension/src/executor_resource_extension/"

# (prefix, 구분, reference file, 권한)
REST_GROUPS = [
    ("EX-API", "실행 API", "01-executions.md", "호출자 키 (relay·일반)"),
    ("EX-ADM", "관리 API", "02-admin.md", "admin 키"),
]

# (구분, 프로그램명, 인터페이스·주기, 설명, 소스)
HEALTH = [
    (
        "헬스체크",
        "liveness",
        "GET /healthz",
        "프로세스가 응답하는지. 인증 없음",
        SRC + "interfaces/http/app.py",
    ),
    (
        "헬스체크",
        "readiness",
        "GET /readyz",
        "DB 스키마 리비전과 Worker 수용 상태. 인증 없음",
        SRC + "interfaces/http/app.py",
    ),
    (
        "헬스체크",
        "Worker 상태",
        "GET /workerz",
        "이 Pod의 Worker 내부 상태. admin 키",
        SRC + "interfaces/http/app.py",
    ),
    (
        "MCP",
        "MCP 엔드포인트",
        "POST /mcp",
        "MCP 도구 호출 진입점. dev에서만 켠다",
        SRC + "interfaces/mcp/server.py",
    ),
]
WORKERS = [
    (
        "Worker",
        "execution-reconciler",
        "모든 Pod, 1초 (명령 접수 시 즉시 깨움)",
        "할 일(취소 > 인계 > 대기)을 claim해 실행 작업으로 띄운다",
        WORKER + "worker.py, work_admission.py, claiming.py",
    ),
    (
        "Worker",
        "SINGLE 실행",
        "claim마다",
        "Operation 하나를 Step 순서대로 실행하고 커널을 정리한다",
        WORKER + "single_runner.py, step_flow.py, step_executor.py",
    ),
    (
        "Worker",
        "MULTI 실행",
        "claim마다",
        "Operation을 실행한 뒤 커널을 유지하고 다음 Operation을 기다린다",
        WORKER + "multi_runner.py, multi_operation_state.py",
    ),
    (
        "Worker",
        "취소 처리",
        "CANCEL_REQUESTED마다",
        "커널 interrupt 후 멈춤을 확인하고 CANCELLED로 확정한다",
        WORKER + "cancellation.py",
    ),
    (
        "Worker",
        "인계 이어받기",
        "인계 대기마다",
        "다른 Worker가 넘긴 Step의 저널을 다시 읽어 이어서 관찰한다",
        WORKER + "handover.py",
    ),
    (
        "Worker",
        "lease heartbeat",
        "실행마다, 15초",
        "lease를 연장하고 소유권을 확인한다",
        WORKER + "lease_heartbeat.py",
    ),
    (
        "Worker",
        "노트북 반영",
        "Operation마다",
        "Step 결과를 실행 노트북에 반영한다",
        WORKER + "notebook_projector.py",
    ),
    (
        "주기 작업",
        "Outbox 발행",
        "모든 Pod, 0.5초",
        "커밋된 이벤트를 Redis Stream으로 순서대로 발행한다",
        SRC + "infrastructure/outbox.py",
    ),
    (
        "주기 작업",
        "lease-recovery",
        "클러스터 1개 Pod, 15초",
        "만료 lease를 회수해 인계 또는 실패로 처리한다",
        WORKER + "lease_recovery.py",
    ),
    (
        "주기 작업",
        "retained-session-cleanup",
        "클러스터 1개 Pod, 15초",
        "보존 기간이 지난 실패 세션을 삭제한다",
        WORKER + "retained_session_cleanup.py",
    ),
    (
        "주기 작업",
        "multi-lifecycle-audit",
        "클러스터 1개 Pod, 15초",
        "대기·수명 초과를 처리한다",
        WORKER + "multi_lifecycle.py",
    ),
    (
        "주기 작업",
        "maintenance-runs",
        "클러스터 1개 Pod, 15초",
        "실행 일괄 중지 Run을 진행한다",
        SRC + "infrastructure/maintenance_runs/reconciler.py",
    ),
    (
        "주기 작업",
        "runtime-health",
        "클러스터 1개 Pod, 15초",
        "Target의 health·세션 수·CPU·메모리를 관측한다",
        SRC + "infrastructure/runtime_registry/monitor.py, probe.py",
    ),
    (
        "주기 작업",
        "events 보존",
        "클러스터 1개 Pod, 600초",
        "이벤트 이력, 발행된 Outbox, Redis Stream 항목을 정리한다",
        SRC + "infrastructure/event_retention.py",
    ),
    (
        "기동 작업",
        "DB 마이그레이션",
        "기동 시 1회",
        "advisory lock을 잡은 Pod 하나가 Alembic upgrade를 실행한다",
        SRC + "infrastructure/db/migrations.py",
    ),
    (
        "기동 작업",
        "startup reconciliation",
        "기동 시 1회",
        "claim을 시작하기 전에 만료 lease를 정리한다",
        WORKER + "session_recovery.py",
    ),
]
JUPYTER = [
    (
        "Jupyter 확장",
        "코드 실행 시작",
        "POST /executor/kernels/{id}/executions",
        "커널에 코드를 제출하고 실행 저널을 만든다",
        EXTENSION + "executions.py",
    ),
    (
        "Jupyter 확장",
        "실행 저널 삭제",
        "DELETE /executor/kernels/{id}/executions/{request_id}",
        "확정된 Step의 저널을 지운다",
        EXTENSION + "executions.py",
    ),
    (
        "Jupyter 확장",
        "실행 저널 스트림",
        "WebSocket /executor/kernels/{id}/executions/{request_id}/stream",
        "저널을 처음부터 이어서 내보낸다",
        EXTENSION + "executions.py",
    ),
    (
        "Jupyter 확장",
        "자원 상태",
        "GET /executor/resource-status",
        "서버의 CPU·메모리·세션 수",
        EXTENSION + "collector.py, cgroups.py",
    ),
    (
        "Jupyter 확장",
        "커널별 사용량",
        "GET /executor/kernel-usage",
        "커널별 메모리와 CPU 시간",
        EXTENSION + "kernel_usage.py",
    ),
    (
        "Jupyter 확장",
        "작업공간·노트북·파일",
        "/executor/storage/{workspaces,notebooks,artifacts,files,manifests}/...",
        "작업공간 준비, 노트북 생성·반영, 산출물 스냅샷, 파일·manifest 읽기",
        EXTENSION + "handlers.py, storage.py, file_download.py",
    ),
]
TOOLS = [
    (
        "운영 도구",
        "API 키 발급",
        "python3 scripts/generate_api_keys.py <env>",
        "호출자별 API 키와 AUTH_CLIENTS에 넣을 SHA-256을 만든다",
        "scripts/generate_api_keys.py",
    ),
    (
        "운영 도구",
        "품질 게이트",
        "uv run python scripts/quality_gate.py",
        "lint, 타입, 테스트, 마이그레이션 검사를 한 번에 돌린다",
        "scripts/quality_gate.py",
    ),
    (
        "운영 도구",
        "산출물 생성",
        "scripts/deliverables/*.py",
        "물리 ERD, Table 정의서, 프로그램 목록, 상태 전이도를 코드에서 만든다",
        "scripts/deliverables/",
    ),
]


def table_rows(markdown: str, heading: str) -> list[list[str]]:
    """Cells of the first pipe table under `heading`, without its header."""
    section = markdown.split(heading, 1)[1]
    lines = []
    for line in section.splitlines()[1:]:
        if line.startswith("|"):
            lines.append([cell.strip() for cell in line.strip("|").split("|")])
        elif lines:
            break
    return lines[2:]


def plain(cell: str) -> str:
    cell = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", cell)
    return cell.replace("`", "").replace("<br>", "\n")


def endpoint_sources() -> dict[str, str]:
    """'METHOD path' -> 'file::function' for every documented REST operation."""
    app = create_app(ApplicationContainer(Settings(runtime_enabled=False)))
    files = {
        match: path
        for path in sorted(Path(SRC, "interfaces/http").rglob("*.py"))
        for match in re.findall(
            r"^\s*(?:async )?def (\w+)\(",
            path.read_text(encoding="utf-8"),
            re.M,
        )
    }
    sources = {}
    for path, item in app.openapi()["paths"].items():
        for method, operation in item.items():
            # FastAPI's default operationId is "<function><path>_<method>" with \W -> "_".
            suffix = re.sub(r"\W", "_", path) + "_" + method
            function = operation["operationId"].removesuffix(suffix)
            sources[f"{method.upper()} {path}"] = (
                f"{files[function]}::{function}"
            )
    return sources


def rest_programs() -> list[list[str]]:
    sources = endpoint_sources()
    programs = []
    for prefix, kind, file, permission in REST_GROUPS:
        rows = table_rows(
            (REFERENCE / file).read_text(encoding="utf-8"), "## 목록"
        )
        for number, interface, mcp, title in rows:
            key = plain(interface)
            if key not in sources:
                raise SystemExit(f"{file}에 있는 {key}가 애플리케이션에 없다")
            programs.append(
                [
                    f"{prefix}-{int(number):03d}",
                    kind,
                    plain(title),
                    key,
                    f"권한: {permission}. MCP 도구: {plain(mcp)}",
                    sources.pop(key),
                ]
            )
    if sources:
        raise SystemExit(
            f"dev_docs/api-reference에 없는 엔드포인트: {sorted(sources)}"
        )
    return programs


def numbered(prefix: str, rows: Sequence[Sequence[str]]) -> list[list[str]]:
    return [[f"{prefix}-{i:03d}", *row] for i, row in enumerate(rows, start=1)]


def main() -> None:
    mcp = [
        (
            "MCP 도구",
            plain(tool),
            plain(tool),
            f"대응 REST: {plain(rest)}",
            SRC + "interfaces/mcp/server.py",
        )
        for _, tool, rest, _ in table_rows(
            (REFERENCE / "04-mcp.md").read_text(encoding="utf-8"),
            "## 도구 목록",
        )
    ]
    events = [
        (
            "이벤트",
            plain(name),
            f"Redis Stream executor.events.<tenant> / {plain(name)}",
            plain(when),
            WORKER + "event_writer/, " + SRC + "infrastructure/events.py",
        )
        for _, name, when in table_rows(
            (REFERENCE / "05-events.md").read_text(encoding="utf-8"),
            "## 이벤트 종류",
        )
    ]
    programs = [
        *rest_programs(),
        *numbered("EX-HLT", HEALTH[:3]),
        *numbered("EX-MCP", [HEALTH[3], *mcp]),
        *numbered("EX-EVT", events),
        *numbered("EX-WRK", WORKERS),
        *numbered("EX-JPX", JUPYTER),
        *numbered("EX-TL", TOOLS),
    ]
    book = Workbook()
    sheet = book.active
    sheet.title = "프로그램 목록"
    sheet["A1"] = "Executor 프로그램 목록"
    sheet["A1"].font = Font(bold=True, size=14)
    counts: dict[str, int] = {}
    for program in programs:
        counts[program[1]] = counts.get(program[1], 0) + 1
    sheet["A2"] = f"전체 {len(programs)}개: " + ", ".join(
        f"{k} {v}" for k, v in counts.items()
    )
    write_grid(
        sheet,
        4,
        [
            "프로그램 ID",
            "구분",
            "프로그램명",
            "인터페이스 · 실행 주기",
            "설명",
            "소스 위치",
        ],
        programs,
    )
    lay_out(sheet, (13, 13, 30, 58, 62, 70))
    sheet.freeze_panes = "A5"
    book.save(OUT)
    print(sheet["A2"].value)


if __name__ == "__main__":
    main()
