"""A provider-neutral tool-calling loop.

Instead of each provider's native function-calling API, the agent replies with one
JSON object per message:
    {"tool": "<name>", "args": {...}}     call a tool
    {"final": "<short summary>"}          stop
This works the same on Gemma, Gemini, Claude, GPT and the mock, which matters
because small local models often lack native tool calling. The tool docs the
agent reads are the tools' own docstrings (course requirement 3).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from ..llm import ChatModel
from ..tools import as_json, call_tool, tool_docs

PROTOCOL = """\
## How to use tools
Reply with EXACTLY ONE JSON object and nothing else, in one of two forms:
  {"tool": "<tool name>", "args": {<arguments>}}
  {"final": "<one-sentence summary>"}
After each tool call you will receive a message starting with TOOL_RESULT.
Only use the tools listed below. Do not invent tool results.

## Tools
"""


def extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of a model reply (tolerates code fences and chatter)."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start: i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def run_tool_agent(
    model: ChatModel,
    system: str,
    user: str,
    tools: list[str],
    ctx: Any,
    *,
    max_steps: int = 14,
    temperature: float = 0.0,
    seed: Optional[int] = None,
    max_tokens: int = 2048,
    context: Optional[dict] = None,
) -> dict:
    messages = [
        {"role": "system", "content": system + "\n\n" + PROTOCOL + tool_docs(tools)},
        {"role": "user", "content": user},
    ]
    trace: list[dict] = []
    bad_format = 0
    final = None
    for step in range(max_steps):
        reply = model.complete(messages, temperature=temperature, seed=seed, max_tokens=max_tokens,
                               context={**(context or {}), "step": step, "messages": messages})
        messages.append({"role": "assistant", "content": reply})
        obj = extract_json(reply)
        if obj is None:
            bad_format += 1
            trace.append({"step": step, "error": "no JSON object", "raw": reply[:300]})
            if bad_format >= 3:
                break
            messages.append({"role": "user", "content": "FORMAT_ERROR: reply with exactly one JSON object, as described."})
            continue
        if "final" in obj:
            final = obj["final"]
            trace.append({"step": step, "final": final})
            break
        name, args = obj.get("tool"), obj.get("args", {})
        if name not in tools:
            result = {"ok": False, "error": f"tool '{name}' is not available to you. Available: {tools}"}
        else:
            result = call_tool(name, args, ctx)
        trace.append({"step": step, "tool": name, "ok": result.get("ok")})
        messages.append({"role": "user", "content": f"TOOL_RESULT {name}: {as_json(result)}"})
    return {"final": final, "trace": trace, "messages": messages}
