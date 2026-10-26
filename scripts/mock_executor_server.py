"""Tiny local Executor mock server for manual agent POST testing."""

from __future__ import annotations

import argparse
import json
from uuid import uuid4
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _json_response(handler: BaseHTTPRequestHandler, status_code: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _build_handler(output_dir: Path | None, print_body: bool):
    class MockExecutorHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length).decode("utf-8")
            try:
                request_body = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError as exc:
                _json_response(
                    self,
                    400,
                    {
                        "status": "INVALID_JSON",
                        "message": str(exc),
                    },
                )
                return

            received_at = datetime.now(timezone.utc).isoformat()
            steps = (
                request_body.get("operation", {})
                .get("source", {})
                .get("spec", {})
                .get("steps", [])
            )
            execution_id = _path_execution_id(self.path) or str(uuid4())
            operation_id = f"op-{uuid4()}"
            operation_steps = [
                {
                    "sequence": step.get("sequence", index),
                    "step_id": f"cell-{uuid4()}",
                }
                for index, step in enumerate(steps if isinstance(steps, list) else [])
            ]
            status = (
                "WAITING_FOR_OPERATION"
                if request_body.get("lifecycle", {}).get("operation_mode") == "MULTI"
                else "QUEUED"
            )
            if self.path.endswith("/cancel"):
                status = "CANCEL_REQUESTED"
            elif self.path.endswith("/finalize"):
                status = "FINALIZING"

            response_body = {
                "execution_id": execution_id,
                "operation": {
                    "operation_id": operation_id,
                    "steps": operation_steps,
                },
                "state": {
                    "status": status,
                    "version": 1,
                },
            }
            _json_response(
                self,
                202,
                response_body,
            )

            record = {
                "received_at": received_at,
                "method": "POST",
                "path": self.path,
                "body": request_body,
                "response": response_body,
            }
            print("\n=== Mock Executor Request ===")
            print(f"PATH: {self.path}")
            print(
                "OPERATION_MODE: "
                f"{request_body.get('lifecycle', {}).get('operation_mode')}"
            )
            print(f"EXECUTION_ID: {execution_id}")
            print(f"OPERATION_ID: {operation_id}")
            print(f"STEP_COUNT: {len(steps) if isinstance(steps, list) else 0}")
            if print_body:
                print(json.dumps(request_body, ensure_ascii=False, indent=2))

            if output_dir is not None:
                output_dir.mkdir(parents=True, exist_ok=True)
                filename = received_at.replace(":", "-").replace(".", "-")
                (output_dir / f"{filename}.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

        def log_message(self, format: str, *args: Any) -> None:
            return

    return MockExecutorHandler


def _path_execution_id(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 4 and parts[:3] == ["api", "v1", "executions"]:
        return parts[3]
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("demo_artifacts/mock_executor_requests"),
        help="Directory where received POST requests are saved.",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Print requests only; do not save them to disk.",
    )
    parser.add_argument(
        "--print-body",
        action="store_true",
        help="Print the full request body. By default only a summary is printed.",
    )
    args = parser.parse_args()

    output_dir = None if args.no_save else args.output_dir
    server = ThreadingHTTPServer(
        (args.host, args.port),
        _build_handler(output_dir, args.print_body),
    )
    print(f"Mock Executor listening on http://{args.host}:{args.port}")
    if output_dir is not None:
        print(f"Saving requests to {output_dir.resolve()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
