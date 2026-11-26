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
    limits: Optional[dict[str, int]] = None,
    finish_tool: Optional[str] = None,
    reserve_steps: int = 0,
    allowed_values: Optional[dict[str, dict[str, set]]] = None,
) -> dict:
    """Run one agent until it replies {"final": ...}, runs out of steps or breaks the format 3 times.

    limits:         at most this many calls per tool; later calls get an error instead of running.
    finish_tool:    the call that finishes the job (the judge's submit_scores). Once it returns ok the loop stops,
                    and in the last `reserve_steps` steps it is the only tool that still runs.
    allowed_values: {tool: {arg: allowed values}}: other values are refused before the tool runs.
    """
    limits, allowed_values = limits or {}, allowed_values or {}
    used: dict[str, int] = {}
    finished = nudged = False
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
        if "final" in obj and finish_tool and not nudged:
            nudged = True                                # once: a judge that stops early has scored nothing
            trace.append({"step": step, "error": f"final before {finish_tool}"})
            messages.append({"role": "user", "content": f"NOT_DONE: nothing is saved until {finish_tool} returns ok. Call it now."})
            continue
        if "final" in obj:
            final = obj["final"]
            trace.append({"step": step, "final": final})
            break
        name, args = obj.get("tool"), obj.get("args", {})
        left = max_steps - step - 1                     # steps after this one
        result = _refusal(name, args, tools, used, limits, allowed_values, finish_tool,
                          reserved=bool(finish_tool) and not finished and left < reserve_steps)
        if result is None:
            used[name] = used.get(name, 0) + 1
            result = call_tool(name, args, ctx)
        trace.append({"step": step, "tool": name, "ok": result.get("ok")})
        if name == finish_tool and result.get("ok"):
            finished = True
            final = f"{name} ok"
            break
        note = f" (steps left: {left})" if finish_tool and not finished else ""
        messages.append({"role": "user", "content": f"TOOL_RESULT {name}: {as_json(result)}{note}"})
    return {"final": final, "trace": trace, "messages": messages}


def _refusal(name, args, tools: list[str], used: dict, limits: dict, allowed_values: dict,
             finish_tool: Optional[str], reserved: bool) -> Optional[dict]:
    """Why this call won't run, as a tool result, or None if it may."""
    if name not in tools:
        return {"ok": False, "error": f"tool '{name}' is not available to you. Available: {tools}"}
    if reserved and name != finish_tool:
        return {"ok": False, "error": f"few steps left: call {finish_tool} now with your best answer"}
    if name in limits and used.get(name, 0) >= limits[name]:
        hint = f" Call {finish_tool} instead; it checks everything at once." if finish_tool else ""
        return {"ok": False, "error": f"{name} may be called at most {limits[name]} times.{hint}"}
    for arg, ok_values in allowed_values.get(name, {}).items():
        if isinstance(args, dict) and args.get(arg) not in ok_values:
            return {"ok": False, "error": f"{arg} must be one of {sorted(ok_values)}"}
    return None
