import re as _re, json as _json, uuid as _uuid

def _parse_qwen_tool_calls(text):
    pattern = r'<function=(\w+)>(.*?)(?:</function>|</tool_call>)'
    matches = list(_re.finditer(pattern, text, _re.DOTALL))
    if not matches:
        return text, None
    tool_calls = []
    for m in matches:
        fn_name = m.group(1)
        params_block = m.group(2)
        args = {}
        ppat = r'<parameter=(\w+)>\n?(.*?)\n?</parameter>'
        for pm in _re.finditer(ppat, params_block, _re.DOTALL):
            args[pm.group(1)] = pm.group(2).strip()
        tool_calls.append({
            "id": f"call_{_uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": fn_name, "arguments": _json.dumps(args)}
        })
    clean = _re.sub(r'<function=\w+>.*?(?:</function>|</tool_call>)', '', text, flags=_re.DOTALL).strip()
    return clean, tool_calls
