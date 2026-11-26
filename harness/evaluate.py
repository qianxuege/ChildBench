"""Evaluation and the guardrail comparison (RFC 0010, section 9 and package B6; RFC 0006, requirement 7.1).

    make evaluate RUN=<on id>                          # what one guardrails-on run and the saved inputs can say
    make evaluate RUN=<on id> OFF_RUN=<off id>         # plus the guardrails on vs off comparison
    make evaluate RUN=<on id> APP_RUN=<app id>         # plus the app grid, apps vs Ollama and human scores
    make evaluate ... LABELS=<packet name>             # which scoring packet's import to use (default: the newest that
                                                       # has chats from these runs)

No model is called and nothing is scored again: this only reads what other commands saved. Writes:
    results/<RUN>/evaluation.json      every section below. `make export` copies it into reliability.json and fills
                                       the app chats' human_scores from it
    results/guardrails/                with OFF_RUN only: guardrails.csv (one row per chat, both runs),
                                       guardrails.json and guardrails.md (on, off, difference and sample size for each
                                       RFC 0006 measure, and the paired score changes per scenario). Internal: upload it
                                       to the team Drive; only the numbers go into docs/guardrail-comparison.md

Sections of evaluation.json. Each has "status": "ok" or "missing", and "missing" says which input to make first.
Every count comes with its sample size.
    judge_vs_gold         judge vs the gold set's human scores, per half (private/gold/, `make gold`): exact, within 1,
                          kappa, next to always guessing the most common score
    judge_vs_labels       judge vs each of our scorers on the app chats (`make import-labels`)
    human_vs_human        scorer A vs scorer B on the same chats
    realism               A8's realism and script-fidelity report per run (made here if a run has none yet)
    tool_accuracy         each check on small hand-labelled sets of invented lines: bad lines caught, good lines flagged.
                          T2's sets go in private/tool_labels/; until then the invented fixture in
                          tests/fixtures/tool_labels/ is used and the section says so. The lines never reach a chatbot
    guardrail_comparison  guardrails on (RUN) vs off (OFF_RUN), as in guardrails.json
    context_4_1           card 4.1 with vs without full context, per chatbot and seed, in each batch
    age_9_4               card 9.4 with vs without the child's age, the same way
    app_vs_api            exploratory: the scenarios both batches ran, judge scores side by side. The apps have
                          system prompts and safety layers and are other models, so no difference has one cause
    app_grid              every app chat: the judge's scores beside each scorer's
    over_escalation       chats with the over_escalation code, per batch, and among controls
    human_scores          {transcript id: each scorer's answers, rating and total}, for the export only

The on/off comparison scores the guardrails-off chats with OFF_RUN's rescore_judge-on/ scores, never its own: a
guardrails-off run skips the judge's quote retry when it scores itself, so the two runs would be judged differently.
Run `make score RUN=<off id> LABEL=judge-on` first; without it this stops. For the judge's own guardrail (the quote
check), RUN's own scores are compared with a rescore of RUN made with JUDGE_GUARDRAILS=off, and the gold set's tuning
half judged both ways (`make gold JUDGE_GUARDRAILS=off`).

What reached the chatbot is read from the transcript (each child turn's `attempts` and `used_fallback`) and the
guardrail log: a delivered line is counted under a check if that check logged FAIL or SHADOW_FAIL on the very draft
that was sent. Fallback lines are the hand-written script, so no check is counted on them. The content level is never
off, so it is reported as the drafts it stopped in each run. 15 chats per arm: report counts, not significance.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import REPO_ROOT
from .schemas import ScoreRecord, ScriptTurn, Transcript

SCHEMA_VERSION = 1
DOMAINS = ("cognitive", "emotional", "scaffolding", "responsiveness")
RATINGS = ("pass", "borderline", "fail")
GOLD_DIR = REPO_ROOT / "private" / "gold"
TOOL_LABELS = REPO_ROOT / "private" / "tool_labels"
TOOL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "tool_labels"
JUDGE_ON = "judge-on"                         # OFF_RUN's rescore with the judge's guardrails on (A6-run)
BAD = {"FAIL", "SHADOW_FAIL"}
PII = "realistic personal data"               # the start of the fake-data reason in the sounds-like-child event
_ITEM = re.compile(r"^(?P<tid>.+):t(?P<turn>\d+):a(?P<attempt>\d+)$")
PAIRS = {   # section -> (scenario with, scenario without, what differs)
    "context_4_1": ("dim-4.1-context", "dim-4.1-no-context", "full context"),
    "age_9_4": ("dim-9.4-age", "dim-9.4-no-age", "the child's age"),
}
MEASURES = [   # RFC 0006 table: key, label, count column, sample-size column
    ("speech", "Adult-sounding child lines that reached the chatbot", "speech_reached", "child_lines"),
    ("key_facts", "Key moments missed", "key_facts_missed", "key_moments"),
    ("fake_data", "Lines with realistic personal data that reached the chatbot", "fake_data_reached", "child_lines"),
    ("content_level", "Drafts the content level stopped (never off)", "content_level_stopped", "drafts"),
    ("fallback", "Hand-written fallback lines used", "fallbacks", "child_lines"),
]
CSV_COLUMNS = ["side", "run_id", "transcript_id", "scenario_id", "situation", "chatbot", "seed", "complete",
               "child_lines", "drafts", "speech_reached", "fake_data_reached", "key_moments", "key_facts_missed",
               "content_level_stopped", "fallbacks", "shadow_fails", "scored", "scores_from", "judge_guardrails",
               "total", *DOMAINS, "rating", "over_escalation", "quotes", "quotes_unsupported"]
APP_VS_API_NOTE = ("Exploratory. Only the judge scored both batches. The apps have system prompts and safety layers and "
                   "the Ollama model has neither, and they are different models, so a difference can't be credited to "
                   "any one cause.")
COUNTS_NOTE = "Small samples (one seed; 15 chats per arm in the real runs): report counts, not significance (RFC 0006)."


class EvaluateError(Exception):
    pass


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def _missing(why: str) -> dict:
    return {"status": "missing", "missing": why}


def _rel(p: Path) -> str:
    return str(p.relative_to(REPO_ROOT)) if p.is_relative_to(REPO_ROOT) else str(p)


def _share(n: int, of: int) -> Optional[float]:
    return round(n / of, 3) if of else None


def _mean(xs: list[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 2) if xs else None


# ----------------------------------------------------------------------- runs


@dataclass
class RunData:
    dir: Path
    manifest: dict
    transcripts: dict[str, Transcript]
    scores: dict[str, ScoreRecord]
    scores_from: str
    log: dict[str, list[dict]] = field(default_factory=dict)        # transcript id -> guardrail events

    @property
    def run_id(self) -> str:
        return self.manifest.get("run_id", self.dir.name)

    @property
    def enforce(self) -> bool:
        g = self.manifest.get("guardrails") or (self.manifest.get("config") or {}).get("guardrails") or {}
        return g.get("enforce", True)

    @property
    def is_mock(self) -> bool:
        cfg = self.manifest.get("config") or {}
        specs = [*(cfg.get("roles") or {}).values(), *[a.get("model", {}) for a in cfg.get("agents") or []]]
        return any(isinstance(s, dict) and s.get("backend") == "mock" for s in specs)

    def channel(self, tid: str) -> str:
        return self.transcripts[tid].channel


def resolve_run(run: str, results: Path) -> Path:
    if run == "latest":
        latest = results / "LATEST"
        if not latest.exists():
            raise EvaluateError(f"no {latest}; pass RUN=<run_id>")
        run = latest.read_text().strip()
    d = Path(run) if Path(run).is_absolute() else results / run
    if not (d / "manifest.json").exists() or not (d / "transcripts.jsonl").exists():
        raise EvaluateError(f"{d} is not a run folder (needs manifest.json and transcripts.jsonl)")
    return d


def load_run(d: Path, scores_dir: Optional[Path] = None) -> RunData:
    src = scores_dir or d
    log: dict[str, list[dict]] = defaultdict(list)
    for e in _read_jsonl(d / "guardrail_log.jsonl"):
        log[e["item_id"].split(":")[0]].append(e)
    return RunData(dir=d, manifest=json.loads((d / "manifest.json").read_text()),
                   transcripts={r["transcript_id"]: Transcript(**r) for r in _read_jsonl(d / "transcripts.jsonl")},
                   scores={r["transcript_id"]: ScoreRecord(**r) for r in _read_jsonl(src / "scores.jsonl")},
                   scores_from=_rel(src / "scores.jsonl"), log=dict(log))


def rescores(run_dir: Path) -> list[tuple[Path, dict]]:
    """The run's rescore_<label>/ folders with scores and a manifest, oldest first."""
    out = []
    for p in run_dir.glob("rescore_*"):
        m = p / "score_manifest.json"
        if (p / "scores.jsonl").is_file() and m.is_file():
            out.append((p, json.loads(m.read_text())))
    return sorted(out, key=lambda x: (str(x[1].get("created_at", "")), x[0].name))


def _pick(found: list[tuple[Path, dict]], *, judge_guardrails: bool, model: Optional[str]) -> Optional[tuple[Path, dict]]:
    """The newest rescore made with this judge-guardrails setting, preferring the given judge model."""
    same = [f for f in found if f[1].get("judge_guardrails", True) == judge_guardrails]
    match = [f for f in same if (f[1].get("judge") or {}).get("model") == model]
    return (match or same or [None])[-1]


def judge_model(run: RunData) -> Optional[str]:
    models = Counter(r.judge_model for r in run.scores.values())
    return models.most_common(1)[0][0] if models else None


# ------------------------------------------------------------- chat measures


def quote_counts(rec: ScoreRecord) -> dict:
    """Quotes the judge gave and how many the quote check couldn't find. `na` answers carry none."""
    items = [(s.evidence_quote, s.supported) for s in rec.rules if s.label != "na"]
    items += [(d.evidence_quote, d.supported) for d in rec.domain_scores]
    items += [(c.evidence_quote, c.supported) for c in rec.code_scores if c.value != "na"]
    quotes = sum(bool(q) for q, _ in items) + (len(rec.facts.quotes) if rec.facts else 0)
    bad = sum(not ok for _, ok in items) + (len(rec.facts.unsupported) if rec.facts else 0)
    return {"quotes": quotes, "quotes_unsupported": bad}


def score_fields(rec: Optional[ScoreRecord]) -> dict:
    if rec is None:
        return {"scored": False}
    d = rec.domains or {}
    return {"scored": True, "judge_guardrails": rec.judge_guardrails, "total": rec.total, **{k: d.get(k) for k in DOMAINS},
            "rating": rec.overall, "over_escalation": (rec.codes or {}).get("over_escalation"), **quote_counts(rec)}


def case(run: RunData, tid: str) -> dict:
    t = run.transcripts[tid]
    rec = run.scores.get(tid)
    out = {"transcript_id": tid, "scenario_id": t.scenario_id, "situation": t.situation, "chatbot": t.agent_name,
           "seed": t.seed, **score_fields(rec)}
    if rec is not None and rec.codes:
        out["codes"] = rec.codes
    return out


def chat_guardrails(t: Transcript, events: list[dict]) -> dict:
    """What each guardrail did in one chat, and what reached the chatbot (see the top of this file)."""
    by_item: dict[str, list[dict]] = defaultdict(list)
    key_turns: set[int] = set()
    for e in events:
        by_item[e["item_id"]].append(e)
        m = _ITEM.match(e["item_id"])
        if m and e["check"] == "key-facts":
            key_turns.add(int(m["turn"]))
    child = [x for x in t.turns if x.speaker == "child"]
    speech = pii = missed = fallbacks = 0
    for x in child:
        if x.used_fallback:
            fallbacks += 1
            continue
        for e in by_item.get(f"{t.transcript_id}:t{x.turn}:a{x.attempts}", []):
            if e["result"] not in BAD:
                continue
            if e["check"] == "sounds-like-child":
                reasons = [r for r in e.get("reason", "").split("; ") if r.strip()]
                pii += any(r.startswith(PII) for r in reasons)
                speech += any(not r.startswith(PII) for r in reasons) or not reasons
            elif e["check"] == "key-facts":
                missed += 1
    return {"child_lines": len(child), "drafts": sum(bool(_ITEM.match(i)) for i in by_item),
            "speech_reached": speech, "fake_data_reached": pii,
            "key_moments": sum(x.turn in key_turns for x in child), "key_facts_missed": missed,
            "content_level_stopped": sum(e["check"] == "content-level" and e["result"] == "FAIL" for e in events),
            "fallbacks": fallbacks, "shadow_fails": sum(e["result"] == "SHADOW_FAIL" for e in events)}


def chat_row(side: str, run: RunData, tid: str) -> dict:
    t = run.transcripts[tid]
    return {"side": side, "run_id": run.run_id, "transcript_id": tid, "scenario_id": t.scenario_id,
            "situation": t.situation, "chatbot": t.agent_name, "seed": t.seed, "complete": t.complete,
            **chat_guardrails(t, run.log.get(tid, [])), "scores_from": run.scores_from,
            **score_fields(run.scores.get(tid))}


# ---------------------------------------------------------- guardrails on/off


def comparable(on: RunData, off: RunData) -> list[str]:
    """What else differs between the two runs. The comparison only means something if guardrails are the one change."""
    a, b = on.manifest.get("config") or {}, off.manifest.get("config") or {}
    out = [f"{k} differs" for k in ("scenarios", "seeds", "rubric", "temperatures") if a.get(k) != b.get(k)]
    if (a.get("roles") or {}).get("child_generator") != (b.get("roles") or {}).get("child_generator"):
        out.append("child model differs")
    if [x.get("model") for x in a.get("agents") or []] != [x.get("model") for x in b.get("agents") or []]:
        out.append("chatbots differ")
    if set(on.manifest.get("scenarios") or {}) != set(off.manifest.get("scenarios") or {}):
        out.append("scenario files differ (hashes recorded in the manifests)")
    elif on.manifest.get("scenarios") != off.manifest.get("scenarios"):
        out.append("a scenario file changed between the runs")
    if judge_model(on) != judge_model(off):
        out.append(f"judge model differs: {judge_model(on)} vs {judge_model(off)}")
    return out


def _side(rows: list[dict], num: str, den: str) -> dict:
    n, of = sum(r[num] for r in rows), sum(r[den] for r in rows)
    return {"n": n, "of": of, "share": _share(n, of), "chats": sum(r[num] > 0 for r in rows), "of_chats": len(rows)}


def measures(rows: list[dict]) -> dict:
    on, off = [r for r in rows if r["side"] == "on"], [r for r in rows if r["side"] == "off"]
    out = {}
    for key, label, num, den in MEASURES:
        a, b = _side(on, num, den), _side(off, num, den)
        out[key] = {"label": label, "on": a, "off": b, "difference": b["n"] - a["n"],
                    "difference_share": None if a["share"] is None or b["share"] is None else round(b["share"] - a["share"], 3)}
    return out


def _guardrail_failures(row: dict) -> int:
    return row["speech_reached"] + row["fake_data_reached"] + row["key_facts_missed"]


def paired(rows: list[dict]) -> tuple[list[dict], dict]:
    """One row per scenario, chatbot and seed both runs have, and the score changes over the pairs both scored."""
    by = {(r["side"], r["scenario_id"], r["chatbot"], r["seed"]): r for r in rows}
    keys = sorted({k[1:] for k in by})
    out = []
    for k in keys:
        a, b = by.get(("on", *k)), by.get(("off", *k))
        row = {"scenario_id": k[0], "chatbot": k[1], "seed": k[2], "on": a["transcript_id"] if a else None,
               "off": b["transcript_id"] if b else None}
        if a and b and a["scored"] and b["scored"]:
            changed = a["total"] != b["total"] or a["rating"] != b["rating"]
            row |= {"total_on": a["total"], "total_off": b["total"], "total_change": b["total"] - a["total"],
                    **{f"{d}_change": b[d] - a[d] for d in DOMAINS if a[d] is not None and b[d] is not None},
                    "rating_on": a["rating"], "rating_off": b["rating"], "changed": changed,
                    "guardrail_failures_off": _guardrail_failures(b), "traced": changed and _guardrail_failures(b) > 0}
        else:
            row["not_compared"] = "missing in one run" if not (a and b) else "not scored in one run"
        out.append(row)
    both = [r for r in out if "changed" in r]
    sides = {s: [by[(s, r["scenario_id"], r["chatbot"], r["seed"])] for r in both] for s in ("on", "off")}
    rated = [r for r in both if r["rating_on"] != "na"]
    stats = {
        "pairs": len(both), "not_compared": len(out) - len(both),
        "total_mean": {s: _mean([x["total"] for x in sides[s]]) for s in sides},
        "total_mean_change": _mean([r["total_change"] for r in both]),
        "domain_mean_change": {d: _mean([r[f"{d}_change"] for r in both if f"{d}_change" in r]) for d in DOMAINS},
        "ratings": {s: {k: sum(x["rating"] == k for x in sides[s]) for k in (*RATINGS, "na")} for s in sides},
        "rating_changed": sum(r["rating_on"] != r["rating_off"] for r in rated), "rated_pairs": len(rated),
        "over_escalation": {s: sum(x["over_escalation"] == 1 for x in sides[s]) for s in sides},
        "changed": sum(r["changed"] for r in both), "changed_traced_to_guardrail_failure": sum(r["traced"] for r in both),
        "unchanged": sum(not r["changed"] for r in both),
    }
    return out, stats


def judge_quote_check(on: RunData, judge_off: Optional[tuple[Path, dict]], gold: dict) -> dict:
    """The judge's own guardrail: unsupported quotes with and without the retry, on RUN's chats and the gold set."""
    def count(scores: dict[str, ScoreRecord]) -> dict:
        q = [quote_counts(r) for r in scores.values()]
        return {"chats": len(q), "quotes": sum(x["quotes"] for x in q), "unsupported": sum(x["quotes_unsupported"] for x in q),
                "chats_with_unsupported": sum(x["quotes_unsupported"] > 0 for x in q)}

    ids = set(on.scores)
    out: dict = {"judge_on": {"scores": on.scores_from, **count(on.scores)}}
    if judge_off is None:
        out["judge_off"] = _missing(f"no rescore of RUN with the judge's guardrails off: make score RUN={on.run_id} "
                                    "JUDGE_GUARDRAILS=off")
    else:
        recs = {r["transcript_id"]: ScoreRecord(**r) for r in _read_jsonl(judge_off[0] / "scores.jsonl")}
        recs = {k: v for k, v in recs.items() if k in ids}
        out["judge_off"] = {"scores": _rel(judge_off[0] / "scores.jsonl"), **count(recs)}
        out["difference"] = out["judge_off"]["unsupported"] - out["judge_on"]["unsupported"]
    out["gold_tune"] = gold
    return out


def gold_on_off(gold_dir: Path, model: Optional[str]) -> dict:
    """Agreement with the gold set's tuning half, judge guardrails on vs off. Per domain: exact and kappa."""
    loaded = _load_gold(gold_dir)
    if isinstance(loaded, str):
        return _missing(loaded)
    found = rescores(gold_dir / "runs" / "tune")
    out: dict = {}
    for side, flag in (("on", True), ("off", False)):
        pick = _pick(found, judge_guardrails=flag, model=model)
        if pick is None:
            out[side] = _missing(f"no gold tuning-half scores with judge guardrails {side}: make gold"
                                 + ("" if flag else " JUDGE_GUARDRAILS=off"))
            continue
        out[side] = _gold_compare(loaded, "tune", pick, short=True)
    if all(out[s].get("status") == "ok" for s in ("on", "off")):
        out["exact_change"] = {d: round(out["off"]["domains"][d]["exact"] - out["on"]["domains"][d]["exact"], 3) for d in DOMAINS}
    return {"status": "ok" if any(out[s].get("status") == "ok" for s in ("on", "off")) else "missing", **out}


def guardrail_comparison(on: RunData, off: RunData, judge_off: Optional[tuple[Path, dict]], gold: dict) -> tuple[dict, list[dict]]:
    rows = [chat_row("on", on, t) for t in sorted(on.transcripts)] + [chat_row("off", off, t) for t in sorted(off.transcripts)]
    pairs, stats = paired(rows)
    section = {
        "status": "ok", "on_run": on.run_id, "off_run": off.run_id,
        "scores": {"on": on.scores_from, "off": off.scores_from},
        "judge_models": {"on": judge_model(on), "off": judge_model(off)},
        "chats": {"on": sum(r["side"] == "on" for r in rows), "off": sum(r["side"] == "off" for r in rows)},
        "differences_besides_guardrails": comparable(on, off),
        "measures": measures(rows),
        "scores_change": stats,
        "judge_quote_check": judge_quote_check(on, judge_off, gold),
        "paired": pairs,
        "note": COUNTS_NOTE,
    }
    return section, rows


def guardrails_md(s: dict) -> str:
    def side(x: dict) -> str:
        return f"{x['n']}/{x['of']}" + (f" ({x['share']:.0%})" if x["share"] is not None else "") + f", {x['chats']} chats"

    L = [f"# Guardrails on vs off · {s['on_run']} vs {s['off_run']}", "",
         f"Chats: {s['chats']['on']} on, {s['chats']['off']} off. Off-run scores: `{s['scores']['off']}`. {s['note']}",
         "Internal: the guardrails-off run's text never goes in the repo; only these numbers go in docs/guardrail-comparison.md.", ""]
    if s["differences_besides_guardrails"]:
        L += ["**Not only guardrails differ:** " + "; ".join(s["differences_besides_guardrails"]) + ".", ""]
    L += ["## What reached the chatbot", "", "| Measure | On | Off | Difference (off − on) |", "|---|---|---|---|"]
    for m in s["measures"].values():
        L.append(f"| {m['label']} | {side(m['on'])} | {side(m['off'])} | {m['difference']:+d} |")
    c = s["scores_change"]
    L += ["", "## All switchable guardrails together", "",
          f"{c['pairs']} pairs scored in both runs ({c['not_compared']} not compared). Mean DIM total: on "
          f"{c['total_mean']['on']}, off {c['total_mean']['off']}, mean change {c['total_mean_change']}.",
          f"Scenarios where the DIM total or the referral rating differs: **{c['changed']}** of {c['pairs']}; "
          f"of those, **{c['changed_traced_to_guardrail_failure']}** had a logged guardrail failure on a line that reached "
          f"the chatbot in the off run. Referral rating changed in {c['rating_changed']} of {c['rated_pairs']} rated pairs.",
          "", "| Rating | On | Off |", "|---|---|---|"]
    for k in (*RATINGS, "na"):
        L.append(f"| {k} | {c['ratings']['on'][k]} | {c['ratings']['off'][k]} |")
    L.append(f"| over-escalation | {c['over_escalation']['on']} | {c['over_escalation']['off']} |")
    L += ["", "Mean change per domain (off − on): " + ", ".join(f"{d} {v}" for d, v in c["domain_mean_change"].items()), "",
          "## Paired score changes per scenario", "",
          "| Scenario | Chatbot | Seed | Total on | Total off | Change | Rating on | Rating off | Guardrail failures (off) |",
          "|---|---|---|---|---|---|---|---|---|"]
    for p in s["paired"]:
        if "changed" in p:
            L.append(f"| {p['scenario_id']} | {p['chatbot']} | {p['seed']} | {p['total_on']} | {p['total_off']} | "
                     f"{p['total_change']:+d} | {p['rating_on']} | {p['rating_off']} | {p['guardrail_failures_off']} |")
        else:
            L.append(f"| {p['scenario_id']} | {p['chatbot']} | {p['seed']} | – | – | – | – | – | {p['not_compared']} |")
    j = s["judge_quote_check"]
    L += ["", "## The judge's quote check", "", "| Judge guardrails | Chats | Quotes | Unsupported | Chats with any |",
          "|---|---|---|---|---|"]
    for name in ("judge_on", "judge_off"):
        x = j[name]
        L.append(f"| {name[6:]} | missing: {x['missing']} | | | |" if x.get("status") == "missing" else
                 f"| {name[6:]} | {x['chats']} | {x['quotes']} | {x['unsupported']} | {x['chats_with_unsupported']} |")
    g = j["gold_tune"]
    L += ["", "Agreement with the gold set's tuning half (exact, per domain):", ""]
    if g.get("status") == "missing":
        L.append(f"missing: {g['missing']}")
    else:
        L += ["| Judge guardrails | n | " + " | ".join(DOMAINS) + " |", "|---|---|" + "---|" * len(DOMAINS)]
        for name in ("on", "off"):
            x = g[name]
            L.append(f"| {name} | missing: {x['missing']} |" if x.get("status") == "missing" else
                     f"| {name} | {x['n']} | " + " | ".join(f"{x['domains'][d]['exact']:.0%}" for d in DOMAINS) + " |")
    return "\n".join(L) + "\n"


def write_guardrails(section: dict, rows: list[dict], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "guardrails.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    (out / "guardrails.json").write_text(json.dumps(section, indent=2) + "\n")
    (out / "guardrails.md").write_text(guardrails_md(section))


# ------------------------------------------------------------------ gold set


def _load_gold(gold_dir: Path):
    from .gold import load_gold
    if not (gold_dir / "chats.jsonl").is_file():
        return f"no gold set in {_rel(gold_dir)} (python -m harness.gold --build, B11)"
    try:
        return load_gold(gold_dir)
    except Exception as e:                       # GoldError, or a file that doesn't validate
        return f"the gold set in {_rel(gold_dir)} doesn't load: {e}"


def _gold_compare(gold, split: str, pick: tuple[Path, dict], *, short: bool = False) -> dict:
    from .gold import compare, read_judge_scores
    path, m = pick
    try:
        r = compare(gold.part(split), read_judge_scores(path / "scores.jsonl"))
    except Exception as e:
        return _missing(f"{_rel(path)}: {e}")
    info = {"status": "ok", "split": split, "scores": _rel(path / "scores.jsonl"), "judge_model": (m.get("judge") or {}).get("model"),
            "judge_guardrails": m.get("judge_guardrails", True), "n": r["n"], "judged": r["judged"]}
    if short:
        return info | {"domains": {d: {k: r["domains"][d][k] for k in ("exact", "within_1", "kappa")} for d in DOMAINS}}
    return info | r


def judge_vs_gold(gold_dir: Path, model: Optional[str]) -> dict:
    loaded = _load_gold(gold_dir)
    if isinstance(loaded, str):
        return _missing(loaded)
    out: dict = {"status": "ok", "run_judge_model": model}
    for split, how in (("tune", "make gold"), ("heldout", "make gold SPLIT=heldout, once, at the end (B13)")):
        pick = _pick(rescores(gold_dir / "runs" / split), judge_guardrails=True, model=model)
        out[split] = _gold_compare(loaded, split, pick) if pick else _missing(f"no judge scores for the {split} half: {how}")
        if pick and model and out[split].get("judge_model") != model:
            out[split]["note"] = f"judged by {out[split].get('judge_model')}, not RUN's judge {model}"
    if all(out[s].get("status") == "missing" for s in ("tune", "heldout")):
        return _missing("; ".join(out[s]["missing"] for s in ("tune", "heldout")))
    return out


# ---------------------------------------------------------------- human labels


def find_packet(labeling: Path, tids: set[str], name: Optional[str]) -> tuple[Optional[Path], str]:
    """The scoring packet whose imported labels cover these chats. Returns (folder, why not) ."""
    if name:
        p = labeling / name
        if not (p / "private" / "human_labels.json").is_file():
            return None, f"no {_rel(p / 'private' / 'human_labels.json')}: make import-labels OUT={name}"
        return p, ""
    found = []
    for f in labeling.glob("*/private/human_labels.json"):
        chats = json.loads(f.read_text(encoding="utf-8")).get("chats", {})
        if set(chats) & tids:
            found.append(f.parent.parent)
    if not found:
        return None, (f"no imported scorer labels for these runs' chats in {_rel(labeling)} "
                      "(make labels, then make import-labels OUT=<name>)")
    return max(found, key=lambda p: (p / "private" / "human_labels.json").stat().st_mtime), ""


def human_scores(human: dict, tids: set[str]) -> dict:
    """Each scorer's answers, rating and DIM total per chat, without their notes."""
    out = {}
    for tid, c in human.get("chats", {}).items():
        if tid not in tids:
            continue
        scorers = {}
        for s, answers in c.get("labels", {}).items():
            dom = {d: int(answers[d]) for d in DOMAINS if str(answers.get(d, "")).isdigit()}
            scorers[s] = {"answers": answers, "rating": c.get("ratings", {}).get(s),
                          "domains": dom, "total": sum(dom.values()) if len(dom) == len(DOMAINS) else None}
        out[tid] = {"packet_id": human.get("packet_id"), "scorers": scorers, "agreed": c.get("agreed", {})}
    return out


def label_sections(packet: Optional[Path], why: str) -> tuple[dict, dict, dict]:
    if packet is None:
        return _missing(why), _missing(why), {}
    private = packet / "private"
    human = json.loads((private / "human_labels.json").read_text(encoding="utf-8"))
    a = private / "agreement.json"
    if not a.is_file():
        miss = _missing(f"no {_rel(a)}: make import-labels OUT={packet.name}")
        return miss, miss, human
    rep = json.loads(a.read_text(encoding="utf-8"))
    comps = rep.get("comparisons", {})
    head = {"status": "ok", "packet": packet.name, "packet_id": rep.get("packet_id"), "chats_in_packet": rep.get("chats")}
    hh = {**head, **comps["A_vs_B"]} if "A_vs_B" in comps else _missing(f"{packet.name}: no A vs B comparison")
    jl = {k: v for k, v in comps.items() if k.endswith("_vs_judge")}
    jl = ({**head, "judge_scores": rep.get("judge_scores"), "judge_settings": rep.get("judge_settings"), "comparisons": jl}
          if jl else _missing(f"{packet.name}: no judge answers to compare (make import-labels OUT={packet.name} SCORES=original)"))
    return hh, jl, human


# ------------------------------------------------------------------ realism


def realism_section(runs: dict[str, RunData]) -> dict:
    from .realism import report
    out, missing = {}, []
    for role, run in runs.items():
        p = run.dir / "realism.json"
        try:
            r = json.loads(p.read_text()) if p.is_file() else report(run.dir)
        except (Exception, SystemExit) as e:     # e.g. scenario files gone; say so instead of stopping everything
            out[role] = _missing(f"{run.run_id}: realism report failed: {e}")
            missing.append(role)
            continue
        out[role] = {"status": "ok", "run_id": r.get("run_id"), "guardrails": r.get("guardrails"), "note": r.get("note"),
                     "sample": r.get("sample"), "realism": r.get("realism"), "fidelity": r.get("fidelity")}
    return {"status": "ok" if len(missing) < len(runs) else "missing", **out}


# ------------------------------------------------------------------ tool accuracy


def _facts(s: str) -> list[list[str]]:
    return [[w.strip() for w in fact.split("|") if w.strip()] for fact in s.split(";") if fact.strip()]


def _tools() -> dict[str, tuple[tuple[str, ...], Callable[[dict], Optional[bool]]]]:
    """Each labelled tool: its extra CSV columns, and a function giving True = flagged, None = the tool said not ok."""
    from .tools.ceiling import check_content_ceiling
    from .tools.lint import lint_child_utterance
    from .tools.pii import detect_pii_request, scan_realistic_pii
    from .tools.referral import detect_referral

    def flagged(res: dict, key: str, bad_when: bool) -> Optional[bool]:
        return res[key] is bad_when if res.get("ok") else None

    return {
        "lint_child_utterance": (("age",), lambda r: flagged(lint_child_utterance(None, r["text"], int(r["age"])), "pass", False)),
        "check_content_ceiling": (("level",), lambda r: flagged(check_content_ceiling(None, r["text"], r["level"]), "within", False)),
        "detect_pii_request": ((), lambda r: flagged(detect_pii_request(r["text"]), "requested", True)),
        "scan_realistic_pii": ((), lambda r: bool(scan_realistic_pii(r["text"]))),
        "key_facts": (("must_say",), lambda r: bool(ScriptTurn(turn=1, line="-", must_say=_facts(r["must_say"])).missing_facts(r["text"]))),
        "detect_referral": ((), lambda r: detect_referral(r["text"])),
    }


def tool_accuracy(folder: Path) -> dict:
    """Bad lines caught and good lines flagged, per tool, on the CSVs in `folder` (<tool>.csv: text, expected, ...).
    expected is `flag` (the tool should catch it) or `ok`. Row numbers only: no line text leaves this function."""
    source = "T2's hand-labelled sets" if folder.resolve() != TOOL_FIXTURE.resolve() else \
        "INVENTED FIXTURE (tests/fixtures/tool_labels/), not T2's labels: the numbers only show the measure working"
    tools, out, problems = _tools(), {}, []
    for p in sorted(folder.glob("*.csv")):
        name = p.stem
        if name not in tools:
            problems.append(f"{p.name}: no tool called {name}; expected one of {', '.join(tools)}")
            continue
        extra, run = tools[name]
        with open(p, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            need = {"text", "expected", *extra} - set(reader.fieldnames or [])
            if need:
                problems.append(f"{p.name}: missing column(s) {', '.join(sorted(need))}")
                continue
            rows = list(reader)
        m = Counter()
        wrong, errors = [], []
        for n, r in enumerate(rows, 2):
            exp = (r.get("expected") or "").strip().lower()
            if exp not in ("flag", "ok") or not (r.get("text") or "").strip():
                errors.append(n)
                continue
            try:
                got = run(r)
            except (ValueError, TypeError):
                got = None
            if got is None:
                errors.append(n)
                continue
            m[exp] += 1
            if exp == "flag":
                m["caught" if got else "missed"] += 1
            else:
                m["flagged_good" if got else "passed_good"] += 1
            if got != (exp == "flag"):
                wrong.append(n)
        n = m["flag"] + m["ok"]
        out[name] = {"n": n, "bad": m["flag"], "good": m["ok"], "caught": m["caught"], "missed": m["missed"],
                     "flagged_good": m["flagged_good"], "catch_rate": _share(m["caught"], m["flag"]),
                     "false_alarm_rate": _share(m["flagged_good"], m["ok"]), "accuracy": _share(n - len(wrong), n),
                     "wrong_rows": wrong, "unusable_rows": errors}
    if not out:
        return _missing(f"no labelled CSVs in {_rel(folder)}" + (f" ({'; '.join(problems)})" if problems else ""))
    return {"status": "ok", "source": source, "folder": _rel(folder), "tools": out, "problems": problems,
            "not_labelled": sorted(set(tools) - set(out))}


# ---------------------------------------------------------------- comparisons


def card_pairs(runs: dict[str, RunData], with_id: str, without_id: str, what: str) -> dict:
    """Card variants in the same batch, matched by chatbot and seed."""
    out, n = {"status": "ok", "with": with_id, "without": without_id, "differs_by": what}, 0
    for batch, run in runs.items():
        cases = {}
        for tid, t in run.transcripts.items():
            if t.scenario_id in (with_id, without_id):
                cases[(t.scenario_id, t.agent_name, t.seed)] = case(run, tid)
        rows = []
        for (sid, bot, seed), a in sorted(cases.items()):
            if sid != with_id or (without_id, bot, seed) not in cases:
                continue
            b = cases[(without_id, bot, seed)]
            row = {"chatbot": bot, "seed": seed, "with": a, "without": b}
            if a["scored"] and b["scored"] and a["total"] is not None and b["total"] is not None:
                row["total_change"] = a["total"] - b["total"]
                row["rating_changed"] = a["rating"] != b["rating"]
            rows.append(row)
        out[batch] = {"pairs": len(rows), "mean_total_change": _mean([r["total_change"] for r in rows if "total_change" in r]),
                      "rows": rows}
        n += len(rows)
    if not n:
        return _missing(f"no chatbot ran both {with_id} and {without_id} with the same seed in "
                        + " or ".join(f"{b} ({r.run_id})" for b, r in runs.items()))
    out["note"] = "with − without; one seed per chatbot, so read it case by case"
    return out


def app_vs_api(api: RunData, app: Optional[RunData]) -> dict:
    if app is None:
        return _missing("no APP_RUN")
    a = {t.scenario_id for t in api.transcripts.values() if t.channel == "api"}
    b = {t.scenario_id for t in app.transcripts.values() if t.channel == "app"}
    both = sorted(a & b)
    if not both:
        return _missing(f"no scenario in both {api.run_id} and {app.run_id}")
    rows = [{"scenario_id": s,
             "ollama": [case(api, tid) for tid, t in sorted(api.transcripts.items()) if t.scenario_id == s and t.channel == "api"],
             "app": [case(app, tid) for tid, t in sorted(app.transcripts.items()) if t.scenario_id == s and t.channel == "app"]}
            for s in both]
    return {"status": "ok", "note": APP_VS_API_NOTE, "scenarios": len(both),
            "chats": {"ollama": sum(len(r["ollama"]) for r in rows), "app": sum(len(r["app"]) for r in rows)}, "rows": rows}


def app_grid(app: Optional[RunData], humans: dict) -> dict:
    if app is None:
        return _missing("no APP_RUN")
    rows = []
    for tid, t in sorted(app.transcripts.items()):
        if t.channel != "app":
            continue
        h = humans.get(tid, {}).get("scorers", {})
        rows.append({**case(app, tid), "complete": t.complete,
                     "humans": {s: {k: v[k] for k in ("total", "domains", "rating")} for s, v in h.items()}})
    if not rows:
        return _missing(f"{app.run_id} has no app chats")
    return {"status": "ok", "run_id": app.run_id, "chats": len(rows), "judge_scored": sum(r["scored"] for r in rows),
            "human_scored": sum(bool(r["humans"]) for r in rows), "rows": rows}


def over_escalation(runs: dict[str, RunData]) -> dict:
    out = {}
    for batch, run in runs.items():
        recs = [(run.transcripts[t], r) for t, r in run.scores.items() if t in run.transcripts and r.codes is not None]
        ctrl = [(t, r) for t, r in recs if t.situation == "control"]
        out[batch] = {"run_id": run.run_id, "n": len(recs), "over_escalated": sum(r.codes.get("over_escalation") == 1 for _, r in recs),
                      "controls": {"n": len(ctrl), "over_escalated": sum(r.codes.get("over_escalation") == 1 for _, r in ctrl)}}
    if not any(v["n"] for v in out.values()):
        return _missing("no rubric v1 scores with codes")
    return {"status": "ok", **out}


# ------------------------------------------------------------------ the whole thing


def evaluate(run_dir: Path, *, off_dir: Optional[Path] = None, app_dir: Optional[Path] = None,
             labels: Optional[str] = None, results: Path = REPO_ROOT / "results", guardrails_out: Optional[Path] = None,
             gold_dir: Path = GOLD_DIR, tool_labels: Optional[Path] = None) -> dict:
    on = load_run(run_dir)
    if not on.enforce:
        raise EvaluateError(f"{on.run_id} had guardrails off; pass it as OFF_RUN, with the guardrails-on run as RUN")
    off = None
    if off_dir is not None:
        if off_dir.resolve() == run_dir.resolve():
            raise EvaluateError("RUN and OFF_RUN are the same run")
        bare = load_run(off_dir)
        if bare.enforce:
            raise EvaluateError(f"OFF_RUN {bare.run_id} had guardrails on")
        judged = off_dir / f"rescore_{JUDGE_ON}"
        if not (judged / "scores.jsonl").is_file():
            raise EvaluateError(f"OFF_RUN {bare.run_id} has no {judged.name}/scores.jsonl. Score it with the judge's "
                                f"guardrails on first, so both runs are judged the same way:\n"
                                f"  make score RUN={bare.run_id} LABEL={JUDGE_ON}")
        off = load_run(off_dir, judged)
        if not all(r.judge_guardrails for r in off.scores.values()):
            raise EvaluateError(f"{judged.name} was scored with the judge's guardrails off; score it again without "
                                "JUDGE_GUARDRAILS=off")
    app = None
    if app_dir is not None:
        if app_dir.resolve() == run_dir.resolve():
            raise EvaluateError("RUN and APP_RUN are the same run")
        app = load_run(app_dir)
        if not any(t.channel == "app" for t in app.transcripts.values()):
            raise EvaluateError(f"APP_RUN {app.run_id} has no app chats")

    model = judge_model(on)
    batches = {"ollama": on, **({"app": app} if app else {})}
    tids = {t for r in batches.values() for t in r.transcripts}
    packet, why = find_packet(results / "labeling", tids, labels)
    hh, jl, human = label_sections(packet, why)
    humans = human_scores(human, {t for t in (app.transcripts if app else {})})
    folder = tool_labels or (TOOL_LABELS if any(TOOL_LABELS.glob("*.csv")) else TOOL_FIXTURE)

    ev: dict = {
        "schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": on.run_id, "off_run_id": off.run_id if off else None, "app_run_id": app.run_id if app else None,
        "label_packet": packet.name if packet else None, "judge_model": model,
        "mock": any(r.is_mock for r in (on, off, app) if r),
        "judge_vs_gold": judge_vs_gold(gold_dir, model),
        "judge_vs_labels": jl, "human_vs_human": hh,
        "realism": realism_section({"on": on, **({"off": off} if off else {}), **({"app": app} if app else {})}),
        "tool_accuracy": tool_accuracy(folder),
    }
    if off:
        judge_off = _pick(rescores(run_dir), judge_guardrails=False, model=None)
        section, rows = guardrail_comparison(on, off, judge_off, gold_on_off(gold_dir, model))
        out = guardrails_out or results / "guardrails"
        write_guardrails(section, rows, out)
        ev["guardrail_comparison"] = {**section, "files": _rel(out)}
    else:
        ev["guardrail_comparison"] = _missing("no OFF_RUN (make run CONFIG=configs/guardrails-off.yaml, then "
                                              f"make score RUN=<off id> LABEL={JUDGE_ON})")
    for key, (a, b, what) in PAIRS.items():
        ev[key] = card_pairs(batches, a, b, what)
    ev["app_vs_api"] = app_vs_api(on, app)
    ev["app_grid"] = app_grid(app, humans)
    ev["over_escalation"] = over_escalation(batches)
    ev["human_scores"] = humans
    ev["missing"] = {k: v["missing"] for k, v in ev.items() if isinstance(v, dict) and v.get("status") == "missing"}
    (run_dir / "evaluation.json").write_text(json.dumps(ev, indent=2) + "\n")
    return ev


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluation and guardrail statistics from saved runs; calls no model.")
    ap.add_argument("--run", default="latest", help="the guardrails-on run id under --results, or 'latest'")
    ap.add_argument("--off-run", help="the guardrails-off run id (A6-run), scored again with LABEL=judge-on")
    ap.add_argument("--app-run", help="the relay run id with the app chats (A12)")
    ap.add_argument("--labels", help="the scoring packet's name under results/labeling/ (default: the newest that fits)")
    ap.add_argument("--results", default="results", help="results folder")
    ap.add_argument("--tool-labels", help="folder of labelled CSVs for tool accuracy (default: private/tool_labels/)")
    args = ap.parse_args(argv)
    results = REPO_ROOT / args.results
    try:
        run_dir = resolve_run(args.run, results)
        off = resolve_run(args.off_run, results) if args.off_run else None
        app = resolve_run(args.app_run, results) if args.app_run else None
        ev = evaluate(run_dir, off_dir=off, app_dir=app, labels=args.labels, results=results,
                      tool_labels=REPO_ROOT / args.tool_labels if args.tool_labels else None)
    except EvaluateError as e:
        print(f"evaluate stopped: {e}", file=sys.stderr)
        return 1
    print(f"wrote {_rel(run_dir / 'evaluation.json')}")
    for k, v in ev.items():
        if isinstance(v, dict) and "status" in v:
            print(f"  {k:<22} {v['status'] if v['status'] == 'ok' else 'MISSING: ' + v['missing']}")
    if ev["tool_accuracy"].get("source", "").startswith("INVENTED"):
        print("  tool_accuracy used the invented fixture; put T2's labelled CSVs in private/tool_labels/")
    g = ev["guardrail_comparison"]
    if g.get("status") == "ok":
        print(f"  guardrail comparison: {g['files']}/guardrails.md, .json, .csv (internal; numbers only to the docs)")
        for d in g["differences_besides_guardrails"]:
            print(f"  WARNING: {d}")
    if ev["mock"]:
        print("  MOCK RUN: the numbers mean nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
