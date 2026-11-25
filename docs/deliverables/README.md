# Executor 개발 산출물

기준: `main` `3a43a90` (2026-11-25), 스키마 리비전 `0017`. Executor 단독 산출물이다.

| # | 산출물 | 원본 | 제출본 (`dist/`) |
|---|---|---|---|
| 1 | 전체 시스템 구성도 | `01-전체-시스템-구성도.md`, `diagrams/01-*.drawio` | docx, pdf |
| 2 | 프로그램 목록 | `scripts/deliverables/programs.py` (자동 생성) | xlsx, pdf |
| 3 | 프로그램 명세서 | `03-프로그램-명세서.md`, `diagrams/03-*.drawio`, 부록은 `dev_docs/api-reference/` | docx, pdf |
| 4 | 논리 ERD | `04-논리-ERD.md`, `diagrams/04-논리-ERD.drawio` | docx, pdf |
| 5 | 물리 ERD | `05-물리-ERD.md`, `diagrams/05-물리-ERD.drawio` (자동 생성) | docx, pdf |
| 6 | Table 정의서 | `scripts/deliverables/schema.py` (자동 생성) | xlsx, pdf |
| 7 | 사용자 매뉴얼 | `07-사용자-매뉴얼.md`, `diagrams/07-*.drawio` | docx, pdf |
| 8 | 운영자 매뉴얼 | `08-운영자-매뉴얼.md`, `diagrams/08-*.drawio` | docx, pdf |

Executor는 Agent가 아니므로 "프로그램 목록(Agent workflow)"은 API·MCP 도구·Worker·주기 작업 목록으로, "프로그램 명세서(Agent Graph)"는 처리 흐름도와 상태 전이도로 대응시켰다. Vector DB는 쓰지 않는다.

## 고칠 때

| 바뀐 것 | 할 일 |
|---|---|
| ORM 모델, 마이그레이션 | `scripts/deliverables/column_descriptions.py`에 컬럼 설명을 더하고 `schema.py` 실행. 설명이 없으면 스크립트가 멈춘다 |
| REST 엔드포인트 | `dev_docs/api-reference`를 갱신하고 `programs.py` 실행. 문서와 코드가 다르면 스크립트가 멈춘다 |
| Worker, 주기 작업, Jupyter 확장 | `programs.py`의 목록을 손으로 고친다 |
| 상태 전이 | `states.py` 실행. 새 전이는 직선으로 그려지니 `ROUTE`에 경로를 더한다 |
| 본문 | `0*.md`를 고친다 |
| 손으로 그린 그림 | Draw.io에서 `.drawio`를 고치고 같은 이름의 PNG로 내보낸다 |

```bash
uv run --with openpyxl python scripts/deliverables/schema.py     # 물리 ERD, Table 정의서
uv run --with openpyxl python scripts/deliverables/programs.py   # 프로그램 목록
PYTHONPATH=scripts/deliverables uv run python scripts/deliverables/states.py   # 상태 전이도
uv run --with python-docx python scripts/deliverables/build.py   # docx, pdf (pandoc, LibreOffice 필요)
```

## 그림 규칙

- 원본은 `diagrams/*.drawio`, 문서에 들어가는 것은 같은 이름의 `.png`다.
- `.drawio`는 압축하지 않은 XML로 저장한다(Draw.io의 파일 속성에서 "압축" 끔). Git diff로 변경을 볼 수 있다.
- 자동 생성 그림 두 장(`05-물리-ERD`, `03-상태-전이도`)은 손으로 고치지 않는다. 다음 생성 때 덮어쓴다.
- PNG 내보내기: Draw.io Desktop에서 `drawio --export --format png --scale 2 diagrams/<이름>.drawio`. Desktop은 폐쇄망에서도 동작한다.
- 색은 `scripts/deliverables/drawio.py`의 `PALETTE` 하나를 쓴다: 호출자(주황), Executor(파랑), Runtime(보라), 저장소(초록), 플랫폼(회색), 주의(빨강).
