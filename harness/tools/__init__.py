"""The ten tools from RFC 0004, plus the registry agents use to call them.

Conventions:
- Each tool's docstring is what the agent reads. Keep: one-line purpose, When to use,
  When NOT to use, Args, Returns.
- Tools never raise to the agent; they return {"ok": false, "error": ...}.
- Tools that need run state take `ctx` (a RunContext) as their first argument. It is
  injected by the registry and hidden from the agent-facing signature.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from .agreement import compute_agreement
from .ceiling import check_content_ceiling
from .evidence import verify_evidence_quote
from .lint import lint_child_utterance
from .persona import load_persona, next_disclosure_step
from .pii import detect_pii_request
from .review import flag_for_human_review
from .rubric import load_rubric
from .scores import submit_scores

TOOLS: dict[str, Callable[..., dict]] = {
    "load_persona": load_persona,
    "next_disclosure_step": next_disclosure_step,
    "lint_child_utterance": lint_child_utterance,
    "check_content_ceiling": check_content_ceiling,
    "detect_pii_request": detect_pii_request,
    "load_rubric": load_rubric,
    "verify_evidence_quote": verify_evidence_quote,
    "submit_scores": submit_scores,
    "flag_for_human_review": flag_for_human_review,
    "compute_agreement": compute_agreement,
}


def _takes_ctx(fn: Callable) -> bool:
    params = list(inspect.signature(fn).parameters)
    return bool(params) and params[0] == "ctx"


def agent_signature(name: str) -> str:
    fn = TOOLS[name]
    sig = inspect.signature(fn)
    params = [p for p in sig.parameters.values() if p.name != "ctx"]
    return f"{name}({', '.join(str(p) for p in params)})"


def tool_docs(names: list[str]) -> str:
    """Render the agent-facing documentation for a set of tools."""
    blocks = []
    for n in names:
        doc = inspect.getdoc(TOOLS[n]) or ""
        blocks.append(f"### {agent_signature(n)}\n{doc}")
    return "\n\n".join(blocks)


def call_tool(name: str, args: dict[str, Any], ctx: Any = None) -> dict:
    """Call a tool by name with JSON args. Never raises."""
    if name not in TOOLS:
        return {"ok": False, "error": f"unknown tool '{name}'. Available: {sorted(TOOLS)}"}
    fn = TOOLS[name]
    if not isinstance(args, dict):
        return {"ok": False, "error": "args must be a JSON object"}
    try:
        result = fn(ctx, **args) if _takes_ctx(fn) else fn(**args)
    except TypeError as e:
        return {"ok": False, "error": f"bad arguments for {agent_signature(name)}: {e}"}
    except Exception as e:  # tools must not crash the agent
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if ctx is not None and hasattr(ctx, "log_tool_call"):
        ctx.log_tool_call(name, args, result)
    return result


def as_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)
