"""Run inside the API/worker image to verify the deployed mock workflow contract."""
from pathlib import Path
import json
import sys

# Prefer src/app over the repository-root app.py, independent of current cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    from app.agents.orchestration import mock_llm
    from app.agents.workflow_generator import workflow_generator_tools as resources
    from app.agents.workflow_generator.workflow_standard_compiler import compile_workflow

    print('mock source:', mock_llm.__file__)
    print('registry:', resources.TOOL_REGISTRY_PATH)
    # A new diagnostic process reads disk; an existing worker may retain old cache.
    registry = resources._load_tool_registry_document()['tools']
    plan = mock_llm.quality_plan().model_dump(mode='json')
    available = set()
    invalid = []
    for si, step in enumerate(plan['workflow']['steps']):
        for ti, tool in enumerate(step['tools']):
            outputs = (registry.get(tool['tool'], {}).get('returns') or {}).get('outputs') or {}
            print(f"({si}, {ti}) {tool['tool']}: outputs={list(outputs)}")
            for parameter, argument in tool.get('arguments', {}).items():
                if argument.get('source') != 'tool_output':
                    continue
                reference = (argument['step_index'], argument['tool_index'], argument['output'])
                if reference not in available:
                    invalid.append(f"step_{si}_tool_{ti}.{parameter}: {reference}")
            available.update((si, ti, output) for output in outputs)
    if invalid:
        print('INVALID REFERENCES:', json.dumps(invalid, ensure_ascii=False))
    try:
        compiled = compile_workflow(plan, workflow_id='00000000-0000-0000-0000-000000000001')
    except Exception as exc:
        print(f'FAIL: {type(exc).__name__}: {exc}')
        return 1
    print('PASS: mock workflow compiles; status=' + compiled['workflow']['status'])
    print('needs_input is expected before the load client submits HITL inputs.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
