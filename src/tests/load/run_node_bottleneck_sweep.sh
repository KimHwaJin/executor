#!/bin/sh
# Locust sweep for graph node timing from first message to Executor submission.
#
# Start the API server separately, then run:
#   LOCUST_HOST=http://127.0.0.1:8000 sh src/tests/load/run_node_bottleneck_sweep.sh
set -eu

repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)"
cd "$repo_root"

host="${LOCUST_HOST:-${SERVER_URL:-http://127.0.0.1:8000}}"
user_levels="${SWEEP_USERS:-1 5 10 20 50 100 150}"
repeat_levels="${SWEEP_REPEATS:-1 2 3 4 5}"
spawn_rate="${SWEEP_SPAWN_RATE:-5}"
stamp="$(date +%Y%m%d-%H%M%S)"
out="${BOTTLENECK_OUT:-var/load/node-timing-${stamp}}"
python_cmd="${PYTHON:-.venv/bin/python}"

PYTHONUNBUFFERED=1 \
MOCK_LLM_SLEEP_SECONDS="${MOCK_LLM_SLEEP_SECONDS:-0}" \
"$python_cmd" src/tests/load/locust_graph_mock_llm.py \
  --host "$host" \
  --users "$user_levels" \
  --repeats "$repeat_levels" \
  --spawn-rate "$spawn_rate" \
  --mock-llm-sleep "${MOCK_LLM_SLEEP_SECONDS:-0}" \
  --message-field content_text \
  --out-dir "$out"
