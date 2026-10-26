"""Local CPU microbenchmark only: excludes HTTP, DB, workers, LLM and executor.

PYTHONSAFEPATH=1 PYTHONPATH=src python3 scripts/benchmark_workflow_hitl.py --iterations 1000
"""
import argparse
import json
from pathlib import Path
import statistics
import time
from types import SimpleNamespace
from uuid import UUID

from app.schemas.common.workflow_run_schema import WorkflowRunRequest
from app.services.workflow_interactions import public_interactions
from app.services.workflow_run_service import resume_command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    cases = []
    for path in sorted((Path(__file__).resolve().parents[1] / "docs/examples/workflow_hitl_v2").glob("*.json")):
        data = json.loads(path.read_text())
        body = data["rest_response"]["body"]
        run = SimpleNamespace(run_id=UUID(body["current_run_id"]), task_id=UUID(body["task_id"]),
                              interrupt=body["current_run"]["interrupt"])
        for request in data["resume_requests"].values():
            request["body"]["command"]["resume"]["interaction_id"] = public_interactions(run)[0]["interaction_id"]
            resume = WorkflowRunRequest.model_validate(request["body"]).command.resume
            cases.append((run, resume))
    for run, resume in cases:
        resume_command(run, resume)
    samples = []
    for _ in range(args.iterations):
        for run, resume in cases:
            start = time.perf_counter()
            public_interactions(run)
            resume_command(run, resume)
            samples.append((time.perf_counter() - start) * 1000)
    ordered = sorted(samples)
    print(json.dumps({"scope": "serial in-process projection + resume validation only",
                      "cases": len(cases), "operations": len(samples),
                      "mean_ms": statistics.mean(samples),
                      "p95_ms": ordered[int((len(samples)-1)*.95)],
                      "p99_ms": ordered[int((len(samples)-1)*.99)],
                      "operations_per_second": len(samples)/(sum(samples)/1000)}, indent=2))


if __name__ == "__main__":
    main()
