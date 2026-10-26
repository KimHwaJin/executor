"""Validate output references against the immutable HITL display catalog."""


def output_reference_allowed(display: dict, address: tuple[int, int, str], reference: dict) -> bool:
    source = (reference["step_index"], reference["tool_index"])
    if source >= address[:2]:
        return False
    if "output_catalog" in display:
        for step in display["output_catalog"]:
            if step["step_index"] != source[0]:
                continue
            for tool in step["tools"]:
                if tool["tool_index"] == source[1]:
                    return any(output["name"] == reference["output"] and output["selectable"]
                               for output in tool["outputs"])
        return False
    # Previously persisted v2 interactions supplied per-parameter options.
    for tool in display.get("tool_inputs", []):
        if (tool["step_index"], tool["tool_index"]) != address[:2]:
            continue
        for parameter in tool["parameters"]:
            if parameter["name"] == address[2]:
                return any(all(option[key] == reference[key] for key in reference)
                           for option in parameter.get("output_options", []))
    return False
