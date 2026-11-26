"""Tools for the scenario-authoring agent (RFC 0010, A7): check a draft scenario, and save it to drafts/.

A draft is never run: it has no reviewer, no config reads drafts/, and moving a reviewed draft into
scenarios/ is a person's step (skills/add-scenario/SKILL.md).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ..schemas import Persona
from .ceiling import scenario_content_problems
from .lint import lint_child_utterance

MAX_FAILED_VALIDATIONS = 6
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,47}$")
DRAFT_HEADER = ("# DRAFT written by the scenario-authoring agent. Not reviewed, and no run reads drafts/.\n"
                "# A person checks it, adds their name to review.reviewers and moves it into scenarios/<situation>/.\n")
_PLACEHOLDER_REVIEW = {"author": "authoring agent", "reviewers": [], "client_approved": False, "approval_basis": None}


def _parse(yaml_text: Any) -> tuple[dict | None, list[str]]:
    if isinstance(yaml_text, dict):                  # small models sometimes send the mapping itself
        return yaml_text, []
    if not isinstance(yaml_text, str) or not yaml_text.strip():
        return None, ["yaml_text must be the whole scenario as a non-empty YAML string"]
    if len(yaml_text) > 8000:
        return None, ["yaml_text longer than 8000 chars: keep the scenario short"]
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        mark, problem = getattr(e, "problem_mark", None), getattr(e, "problem", None) or str(e).splitlines()[0]
        where = ""
        if mark is not None:
            lines = yaml_text.splitlines()
            bad = lines[mark.line].strip()[:80] if mark.line < len(lines) else ""
            where = f" at line {mark.line + 1} ({bad!r})"
        return None, [f"not valid YAML{where}: {problem}. Put quotes around a line that contains ': ' or starts with a quote"]
    if not isinstance(data, dict):
        return None, ["the YAML must be one mapping of fields (id:, situation:, turns:, ...)"]
    return data, []


def _schema_problems(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        where = ".".join(str(x) for x in err.get("loc", ()))
        msg = err.get("msg", "").removeprefix("Value error, ")
        out.append(f"{where}: {msg}" if where else msg)
    return out


def _existing_ids(ctx) -> set[str]:
    ids = set(getattr(ctx, "personas", {}) or {})
    root = getattr(ctx, "drafts_root", None)
    if root is not None:
        ids |= {p.stem for p in Path(root, "scenarios").glob("*/*.yaml")}
    return ids


def _check(ctx, yaml_text: Any) -> tuple[Persona | None, dict | None, list[str]]:
    """Every problem with a draft, as short reasons. Writes nothing."""
    data, problems = _parse(yaml_text)
    if data is None:
        return None, None, problems
    data = dict(data)
    data.setdefault("review", dict(_PLACEHOLDER_REVIEW))      # save_scenario_draft sets the real review
    sid = data.get("id")
    if not isinstance(sid, str) or not ID_PATTERN.match(sid):
        problems.append("id: use lowercase letters, digits, '-' and '.', e.g. 'draft-s4-001'")
    elif sid in _existing_ids(ctx):
        problems.append(f"id: '{sid}' already exists in scenarios/ or drafts/; pick another id")
    try:
        p = Persona(**data)
    except ValidationError as e:
        return None, data, problems + _schema_problems(e)
    except Exception as e:  # noqa: BLE001 - a draft must never crash the agent
        return None, data, problems + [f"{type(e).__name__}: {e}"]
    # Every scenario in scenarios/ follows this; Persona doesn't enforce it, and a model got it wrong (docs/findings.md, F-019).
    if p.is_referral and p.referral.expectation == "not_required":
        problems.append(f"referral.expectation: {p.situation} is a referral situation ({p.code}): use required or "
                        "recommended, with trigger_turn and adults")
    elif not p.is_referral and p.referral.expectation != "not_required":
        problems.append(f"referral.expectation: {p.situation} is not a referral situation: use not_required")
    problems += scenario_content_problems(p)       # content ceiling and realistic personal data
    allowed = p.allowed_words + [p.child_name]
    lines = [(f"turn {t.turn}", t.line) for t in p.turns] + [("pushback_line", p.pushback_line), ("age_cue", p.age_cue)]
    for name, text in lines:
        if not text:
            continue
        lint = lint_child_utterance(None, text, p.child_age, allowed)
        if not lint.get("ok"):
            problems.append(f"{name}: {lint.get('error')}")
        elif not lint["pass"]:
            problems.append(f"{name}: doesn't sound like a {p.child_age}-year-old: {'; '.join(lint['reasons'])}")
    return p, data, problems


def _count_failure(ctx) -> int:
    n = getattr(ctx, "failed_validations", 0) + 1
    if ctx is not None:
        ctx.failed_validations = n
    return n


def _stopped(ctx) -> dict | None:
    if getattr(ctx, "failed_validations", 0) >= MAX_FAILED_VALIDATIONS:
        return {"ok": False, "stopped": True,
                "error": f"{MAX_FAILED_VALIDATIONS} failed validations: stopping. Nothing is saved; reply with final."}
    return None


def validate_scenario(ctx, yaml_text: str) -> dict:
    """Check a draft scenario file and list every problem to fix. Never writes anything.

    When to use: after writing or changing a draft, before save_scenario_draft. Fix every problem
    it lists, then call it again with the whole corrected YAML.
    When NOT to use: to check one child line on its own (use lint_child_utterance or
    check_content_ceiling), or to save (use save_scenario_draft, which runs this same check).

    Args:
        yaml_text: The whole scenario as YAML text, in the format of the template you were given
            (id, source, situation, child_age, child_name, turns with line and must_say, max_turns,
            referral, good, bad, ...). Max 8000 chars. `review` may be left out; saving sets it.

    Returns:
        {"ok": true, "pass": bool, "problems": [str], "failed_validations": int}
        Each problem is short and says what to change, e.g.
        "turn 2: the line doesn't say [['broke', 'broken']]",
        "turn 1: doesn't sound like a 4-year-old: adult words: unfortunately",
        "max_turns: must be 3 to 5". After 6 failed checks you must stop: nothing is saved.
        On bad input: {"ok": false, "error": "..."}.
    """
    stop = _stopped(ctx)
    if stop:
        return stop
    _, _, problems = _check(ctx, yaml_text)
    n = _count_failure(ctx) if problems else getattr(ctx, "failed_validations", 0)
    return {"ok": True, "pass": not problems, "problems": problems, "failed_validations": n}


def save_scenario_draft(ctx, yaml_text: str) -> dict:
    """Save a draft scenario that passes validate_scenario, to drafts/scenarios/<situation>/<id>.yaml.

    When to use: once, after validate_scenario returned pass: true for this exact YAML.
    When NOT to use: to check a draft (use validate_scenario), or to change an existing scenario:
    it refuses an id that already exists in scenarios/ or drafts/ and never writes anywhere else.

    Args:
        yaml_text: The whole scenario as YAML text, the same text that passed validate_scenario.

    Returns:
        {"ok": true, "path": "drafts/scenarios/<situation>/<id>.yaml", "id": str}
        The review fields are set by this tool, whatever the draft says: the author is the authoring
        agent, no reviewers, not client-approved. No run uses the draft until a person reviews it.
        If the draft fails a check: {"ok": false, "error": "...", "problems": [str]}.
    """
    stop = _stopped(ctx)
    if stop:
        return stop
    root = getattr(ctx, "drafts_root", None)
    if root is None:
        return {"ok": False, "error": "no drafts folder in this run (only the authoring agent saves drafts)"}
    p, data, problems = _check(ctx, yaml_text)
    if problems or p is None:
        n = _count_failure(ctx)
        return {"ok": False, "error": "the draft does not pass validate_scenario; fix these and validate again",
                "problems": problems, "failed_validations": n}
    data["review"] = {"author": f"authoring agent ({getattr(ctx, 'model_id', 'unknown model')})",
                      "reviewers": [], "client_approved": False, "approval_basis": None}
    Persona(**data)                                           # still valid with the reset review
    out = Path(root, "scenarios", p.situation, f"{p.id}.yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "x", encoding="utf-8") as f:              # "x": never overwrite
        f.write(DRAFT_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110))
    if ctx is not None:
        ctx.saved_path = out
    shown = out.relative_to(Path(root).parent) if Path(root).name == "drafts" else out
    return {"ok": True, "path": str(shown), "id": p.id}
