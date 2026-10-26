#!/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11
"""로컬 FastAPI 서버 실행 진입점.

프로젝트 루트에서 ``.venv/bin/python run.py``만 실행하면 됩니다.
DB, host, port, reload 설정은 루트 config.Settings가 담당합니다.
"""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import uvicorn

from config import settings


def main() -> None:
    reload_options = {}
    if settings.server_reload:
        # Agent 산출물을 소스 변경으로 오인해 실행 중 Worker를 재시작하지 않게 합니다.
        reload_options["reload_excludes"] = [
            "src/app/core/demo_artifacts/**",
            "var/workflows/**",
        ]
    uvicorn.run(
        "main:app", app_dir=str(SRC_ROOT),
        host=settings.server_host, port=settings.server_port,
        reload=settings.server_reload, **reload_options,
    )


if __name__ == "__main__":
    main()
