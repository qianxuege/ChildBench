"""Run state shared by the orchestrator, agents and tools, and the results writer.

Tools are the only components that write to the results folder (RFC 0004).
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from .schemas import GuardrailEvent, Persona, Rubric, ScoreRecord, Transcript


class RunContext:
    def __init__(self, run_id: str, out_dir: Path, rubric: Rubric, personas: dict[str, Persona],
                 enforce: bool = True, audit_rate: float = 0.10, seed: int = 0):
        self.run_id = run_id
        self.out_dir = out_dir
        self.rubric = rubric
        self.personas = personas
        self.enforce = enforce                 # False = guardrails in shadow mode (RFC 0006 ablation arm A)
        self.audit_rate = audit_rate
        self.rng = random.Random(seed)
        self.transcripts: dict[str, Transcript] = {}
        self.auto_checks: dict[str, dict] = {}
        self.scored: dict[str, ScoreRecord] = {}
        self.submit_attempts: dict[str, int] = {}
        self.queued: set[tuple[str, str]] = set()
        self.current_judge_model = ""
        self.tool_calls = 0
        out_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- writing
    def write(self, filename: str, record: Any) -> None:
        line = record.model_dump_json() if isinstance(record, BaseModel) else json.dumps(record, ensure_ascii=False)
        with open(self.out_dir / filename, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def guardrail(self, item_id: str, check: str, result: str, reason: str = "", enforced: Optional[bool] = None) -> None:
        self.write("guardrail_log.jsonl", GuardrailEvent(
            run_id=self.run_id, item_id=item_id, check=check, result=result, reason=reason,
            enforced=self.enforce if enforced is None else enforced,
        ))

    def log_tool_call(self, name: str, args: dict, result: dict) -> None:
        self.tool_calls += 1
        self.write("tool_calls.jsonl", {"tool": name, "args": _short(args), "ok": result.get("ok"),
                                        "result": _short(result)})


def _short(obj: Any, limit: int = 600) -> Any:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return obj if len(s) <= limit else s[:limit] + "...(truncated)"
