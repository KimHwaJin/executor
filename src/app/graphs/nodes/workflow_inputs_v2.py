"""All-parameter HITL display and deterministic per-run input binding."""
from copy import deepcopy
import ast
import json

from langchain_core.runnables import RunnableConfig

from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError

from app.agents.workflow_generator.workflow_generator_tools import _load_tool_registry_document
from app.agents.workflow_generator.workflow_standard_compiler import compile_workflow, extract_workflow_inputs, PASSTHROUGH_DATA_OUTPUTS
from app.graphs.hitl_protocol import request_human_input
from app.graphs.nodes.workflow_inputs import _type_schema
from app.schemas.common.workflow_output_catalog import output_reference_allowed
from app.schemas.common.workflow_hitl_actions import get_action, transport_actions
from app.schemas.common.workflow_hitl_schema import ToolInputDisplay, ToolInputSubmission, build_tool_input_submission_schema


def input_display(state: dict) -> tuple[ToolInputDisplay, dict]:
    """Preserve catalog authoring; promote editable arguments only in a run copy."""
    original = state.get("workflow_input_definition") or state["workflow_definition"]
    execution_definition = deepcopy(original)
    registry = _load_tool_registry_document()["tools"]
    for step in execution_definition["workflow"]["steps"]:
        for tool in step["tools"]:
            for name, meta in registry[tool["tool"]]["inputs"].items():
                if meta.get("kind") in ("var_keyword", "var_positional"):
                    continue
                if tool["arguments"].get(name, {}).get("source") != "tool_output":
                    tool["arguments"][name] = {"source": "input"}
    fields = {(f["step_index"], f["tool_index"], f["parameter"]): f
              for f in extract_workflow_inputs(execution_definition)}
    bindings = {(v["step_index"], v["tool_index"], v["parameter"]): v
                for v in state.get("execution_inputs", [])}
    tools = []
    output_catalog = []
    for si, step in enumerate(original["workflow"]["steps"]):
        catalog_step = {"step_index": si, "skill": step["skill"], "tools": []}
        output_catalog.append(catalog_step)
        for ti, tool in enumerate(step["tools"]):
            parameters = []
            for name, meta in registry[tool["tool"]]["inputs"].items():
                if meta.get("kind") in ("var_keyword", "var_positional"):
                    continue
                argument = tool["arguments"].get(name, {})
                editable = True
                field = fields.get((si, ti, name))
                schema = _type_schema(ast.parse(meta.get("type") or "object", mode="eval").body)
                if (field and field["nullable"]) or (meta.get("has_default") and meta.get("default") is None):
                    schema = {"anyOf": [schema, {"type": "null"}]}
                parameter = {
                    "name": name, "title": name, "description": meta.get("description", ""),
                    "required": not bool(meta.get("has_default")), "editable": editable,
                    "value_schema": schema, "has_default": bool(meta.get("has_default")),
                    "has_value": False, "value_origin": "unset",
                }
                if parameter["has_default"]:
                    parameter["default_value"] = deepcopy(meta["default"])
                binding = bindings.get((si, ti, name))
                if argument.get("source") == "tool_output":
                    parameter.update(value_origin="tool_output", binding=deepcopy(argument))
                elif binding is not None:
                    parameter.update(has_value=True, value=deepcopy(binding["value"]), value_origin=binding["origin"])
                elif argument.get("source") == "literal":
                    parameter.update(has_value=True, value=deepcopy(argument["value"]), value_origin="workflow_literal")
                elif parameter["has_default"]:
                    parameter.update(has_value=True, value=deepcopy(meta["default"]), value_origin="default")
                parameters.append(parameter)
            tools.append({"step_index": si, "skill": step["skill"], "tool_index": ti,
                          "tool": tool["tool"], "parameters": parameters})
            catalog_tool = {"tool_index": ti, "tool": tool["tool"], "outputs": []}
            catalog_step["tools"].append(catalog_tool)
            for output, meta in (registry[tool["tool"]].get("returns", {}).get("outputs") or {}).items():
                data_source = tool["arguments"].get("data", {}).get("source")
                safe = tool["execution"] == "always" or (
                    output in PASSTHROUGH_DATA_OUTPUTS and data_source in ("input", "tool_output"))
                catalog_tool["outputs"].append({"name": output, "type": meta.get("type"),
                                                "description": meta.get("description", ""), "selectable": safe})
    action = get_action("tool_inputs", "submit_inputs")
    display = ToolInputDisplay(
        workflow_id=state["workflow_id"], workflow_revision=state["workflow_revision"],
        workflow=deepcopy(original), tool_inputs=tools, output_catalog=output_catalog,
        available_actions=[{"intent": action.intent, "label": action.label}],
    )
    return display, execution_definition


def agent_chat_input_form(display: ToolInputDisplay) -> tuple[dict, dict]:
    """Flat text fields for Agent Chat; only enabled by the dev entrypoint."""
    args = {"workflow_id": display.workflow_id, "workflow_revision": display.workflow_revision}
    properties = {
        "workflow_id": {"type": "string", "const": display.workflow_id, "readOnly": True},
        "workflow_revision": {"type": "integer", "const": display.workflow_revision, "readOnly": True},
    }
    for tool in display.tool_inputs:
        for parameter in tool.parameters:
            if not parameter.editable:
                continue
            name = f"step_{tool.step_index}_tool_{tool.tool_index}_{parameter.name}"
            value = ""
            if parameter.binding:
                value = "@output " + json.dumps({k: parameter.binding[k] for k in ("step_index", "tool_index", "output")})
            elif parameter.has_value:
                value = json.dumps(parameter.value, ensure_ascii=False)
                if isinstance(parameter.value, str) and parameter.value:
                    try:
                        json.loads(parameter.value)
                    except json.JSONDecodeError:
                        value = parameter.value
            args[name] = value
            properties[name] = {
                "type": "string",
                "title": f"Step {tool.step_index} / Tool {tool.tool_index} · {tool.tool}.{parameter.name}",
                "description": ("필수" if parameter.required else "선택; 빈칸은 기본값 복원")
                + "; :default는 기본값, @output 뒤에 참조 JSON 입력. 공통 output_catalog에서 선행 Tool을 선택하세요."
                + "; 값 형식: " + json.dumps(parameter.value_schema, ensure_ascii=False),
            }
    return args, {"type": "object", "description": "공통 output_catalog: " + json.dumps(
                      [s.model_dump() for s in display.output_catalog], ensure_ascii=False),
                  "properties": properties, "additionalProperties": False,
                  "required": ["workflow_id", "workflow_revision"]}


def agent_chat_input_submission(response: dict, display: ToolInputDisplay, form_schema: dict) -> dict:
    """Convert edited text fields, then let the unchanged v2 schema validate."""
    Draft202012Validator(form_schema).validate(response)
    inputs = []
    for tool in display.tool_inputs:
        for parameter in tool.parameters:
            if not parameter.editable:
                continue
            name = f"step_{tool.step_index}_tool_{tool.tool_index}_{parameter.name}"
            text = response.get(name, "")
            address = {"step_index": tool.step_index, "tool_index": tool.tool_index, "parameter": parameter.name}
            if text.startswith("@output "):
                inputs.append({**address, "source": "tool_output", "reference": json.loads(text[8:])})
                continue
            if text.strip() == ":default":
                inputs.append({**address, "source": "default"})
                continue
            if not text.strip():
                if parameter.required and parameter.binding is None:
                    raise ValueError(f"{name}: 필수 입력값을 채워주세요. null을 허용하는 인자는 null을 입력하세요.")
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                value = text
            validator = Draft202012Validator(parameter.value_schema)
            if not validator.is_valid(value) and validator.is_valid(text):
                value = text
            if not validator.is_valid(value):
                raise ValueError(f"{name}: 입력값의 타입 또는 허용값이 맞지 않습니다.")
            inputs.append({"step_index": tool.step_index, "tool_index": tool.tool_index,
                           "parameter": parameter.name, "value": value})
    return {"intent": "submit_inputs", "workflow_id": response["workflow_id"],
            "workflow_revision": response["workflow_revision"], "inputs": inputs}


def collect_tool_inputs_v2(state: dict, config: RunnableConfig = None) -> dict:
    display, definition = input_display(state)
    schema = build_tool_input_submission_schema(display)
    use_agent_chat_form = bool((config or {}).get("configurable", {}).get("agent_chat_input_form"))
    args = display.model_dump(mode="json")
    form_schema = schema
    description = "각 Tool의 필수·선택 인자를 확인하고 입력해주세요. Step/Tool 번호는 0부터 시작합니다."
    if state.get("input_errors"):
        description += "\n이전 입력 오류: " + state["input_errors"]
    if use_agent_chat_form:
        args, form_schema = agent_chat_input_form(display)
        description += ("\n각 입력칸을 채운 뒤 Submit하세요. 문자열은 그대로, 숫자·배열·객체·불리언은 JSON으로 입력합니다."
                        " null은 null, 빈 문자열은 \"\"를 입력하세요. :default는 기본값 복원입니다."
                        " @output 뒤에 참조 JSON을 입력하면 선행 Tool 출력에 연결합니다."
                        " 빈칸은 기존 연결이 있으면 연결 유지, 없으면 선택 인자의 기본값을 사용합니다.")
    response = request_human_input(
        action_name="additional_information", description=description,
        args=args, args_schema=form_schema, payload_version=2,
        allowed_decisions=transport_actions("tool_inputs", [a.intent for a in display.available_actions]),
    )
    try:
        if use_agent_chat_form and isinstance(response, dict) and "intent" not in response:
            response = agent_chat_input_submission(response, display, form_schema)
        Draft202012Validator(schema).validate(response)
        answer = ToolInputSubmission.model_validate(response)
        display_data = display.model_dump(mode="json")
        for item in answer.inputs:
            if item.source == "tool_output" and not output_reference_allowed(
                display_data, (item.step_index, item.tool_index, item.parameter), item.reference.model_dump()
            ):
                raise ValueError("사용 가능한 선행 Tool의 반환값을 선택해주세요.")
        submitted = {(i.step_index, i.tool_index, i.parameter): i for i in answer.inputs}
        values = []
        for tool in display.tool_inputs:
            for parameter in tool.parameters:
                if not parameter.editable:
                    continue
                address = (tool.step_index, tool.tool_index, parameter.name)
                item = submitted.get(address)
                arguments = definition["workflow"]["steps"][address[0]]["tools"][address[1]]["arguments"]
                if item is not None and item.source == "tool_output":
                    arguments[parameter.name] = {"source": "tool_output", **item.reference.model_dump()}
                    continue
                if item is None and parameter.binding is not None:
                    # Old clients omit read-only connections; preserve those bindings.
                    arguments[parameter.name] = deepcopy(parameter.binding)
                    continue
                arguments[parameter.name] = {"source": "input"}
                value = item.value if item is not None and item.source == "value" else parameter.default_value
                values.append({"step_index": address[0], "tool_index": address[1], "parameter": address[2],
                               "value": deepcopy(value), "origin": "user_answer", "confirmed": True})
        compiled = compile_workflow(definition, workflow_id=display.workflow_id, execution_inputs=values)
        if compiled["workflow"]["status"] != "ready":
            raise ValueError("필수 입력값을 모두 제출해주세요.")
    except (ValueError, SchemaValidationError) as exc:
        message = exc.message if isinstance(exc, SchemaValidationError) else str(exc)
        return {"input_errors": message, "tool_inputs_confirmed": False, "approval": {}}
    return {"execution_inputs": values, "workflow_input_definition": definition,
            "tool_inputs_confirmed": True, "plan_review_payload_version": 2,
            "input_errors": "", "approval": {}}
