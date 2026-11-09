"""Export a run to static JSON for the website, after the publishing check (RFC 0010, section 10; package B5).

    make export RUN=<id>                     # site/data/ from reviewed scenarios only
    make export RUN=<id> APP_RUN=<id>        # plus a relay run (A11) as the leaderboard's app_sample block
    make export RUN=<id> PREVIEW=1           # internal preview: unreviewed scenarios too; never commit or deploy

The site reads only site/data/ (gitignored), never results/. Files written:
    meta.json              run id, rubric version, the ages for the age filter, dates, schema_version, preview, counts
    leaderboard.json       run details; per chatbot: pass / borderline / fail per situation, age-appropriate rate,
                           over-escalation on controls, mean first-referral turn, n. App conversations go only in
                           the separate app_sample block, never into these rates.
    scenarios.json         each exported scenario: id, situation, age, setting, beats (plans only, no fallback
                           lines), review status, its transcript ids
    transcripts/<id>.json  turns, automatic checks, rule answers with quotes, overall rating, guardrail reasons;
                           app conversations also carry the app, its private mode and the date
    reliability.json       evaluation.json's sections once B6 writes them, plus the run's quote-check and audit counts

The publishing check (RFC 0006; RFC 0010, sections 8 and 10). A transcript is left out of every file, numbers
included, for any of these reasons:
    NOT_REVIEWED                its scenario has no second reviewer                      (included with PREVIEW=1)
    NOT_APPROVED_BY_RAN         a distress scenario without review.ran_approved: true    (included with PREVIEW=1)
    HELD_OUT                    a hold-out scenario, kept private (RFC 0006)             (included with PREVIEW=1)
    SCAN_HIT_NEEDS_PERSON       a turn, the child's or the chatbot's, or a judge quote, the reason it ended or the
                                model name an app showed, is above the scenario's content level or has realistic
                                personal data, and no person has approved it. A person reads it and adds its id to
                                results/<run>/publish_approved.txt, which clears scan hits only.
    SCENARIO_CHANGED_SINCE_RUN  the scenario file changed after the run in more than its review block
    ENDED_IN_ERROR              a model call failed mid-conversation, so the chatbot was cut off ("agent error: ...")
    SCENARIO_FILE_MISSING, NOT_SCORED, INCOMPLETE (a relay conversation stopped early, so it was never scored)
The full result goes to results/<run>/publish_scan.json.

Refused: runs with guardrails off; mock runs, unless PREVIEW=1 or --allow-mock; an APP_RUN with API conversations.
Chatbot replies are never edited: a transcript is exported as it is, or not at all.
Until A2 and B3 land there are no age groups: meta.json lists the scenarios' ages for the age filter, and the
over-escalation rate is the share of controls where the judge's C1 label is no.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from . import REPO_ROOT, __version__
from .run import load_personas, load_rubric
from .schemas import SITUATION_CODE, Persona, Rubric, ScoreRecord, Transcript
from .summary import summarize
from .tools.ceiling import check_content_ceiling
from .tools.pii import scan_realistic_pii

SCHEMA_VERSION = 1
NEEDS_RAN = {"distressing_disclosure"}
PREVIEW_LIFTS = ("NOT_REVIEWED", "NOT_APPROVED_BY_RAN", "HELD_OUT")   # the only reasons PREVIEW=1 lets through
PREVIEW_NOTE = "Internal preview: unreviewed scenarios, local models, not for publishing"
CONTENT_NOTE = ("Some conversations involve a pretend child who is sad or worried. All children, names and "
                "details are invented. Chatbot replies are shown exactly as the chatbot wrote them.")
APP_NOTE = ("Run by hand in each free app's private chat mode and scored the same way. A small, dated snapshot: "
            "app models can change without notice. Never mixed into the API rates.")
OVER_ESCALATION = "share of control conversations where the judge's C1 label is no (referred without need)"
_CHUNK, _STEP = 4000, 3800                      # check_content_ceiling takes at most 4000 characters


class ExportError(Exception):
    pass


@dataclass
class Run:
    dir: Path
    manifest: dict
    cfg: dict
    raw: dict[str, dict]                         # transcript id -> the line as saved, with any newer fields
    transcripts: dict[str, Transcript]
    scored: dict[str, ScoreRecord]
    queued: set[tuple[str, str]]
    rubric: Rubric
    approved: set[str]
    is_mock: bool
    guardrail_log: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return self.manifest.get("run_id", self.dir.name)


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]     # the same hash run.py records for scenarios and the rubric


def _dump(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def resolve_run(run: str, results_dir: Path) -> Path:
    if run == "latest":
        latest = results_dir / "LATEST"
        if not latest.exists():
            raise ExportError(f"no {latest}; run the pipeline first or pass RUN=<run_id>")
        run = latest.read_text().strip()
    d = results_dir / run
    if not (d / "manifest.json").exists() or not (d / "transcripts.jsonl").exists():
        raise ExportError(f"{d} is not a run folder (needs manifest.json and transcripts.jsonl)")
    return d


# ------------------------------------------------------------- loading a run


def _is_mock(manifest: dict, cfg: dict) -> bool:
    models = manifest.get("models", {})
    ids = [models.get("child_generator"), models.get("child_reviewer"), models.get("judge"),
           *(models.get("agents") or {}).values()]
    specs = [*(cfg.get("roles") or {}).values(), *(a.get("model", {}) for a in cfg.get("agents", []))]
    return (any(str(m).startswith("mock:") for m in ids if m)
            or any(isinstance(s, dict) and s.get("backend") == "mock" for s in specs))


def load_run(run_dir: Path, *, preview: bool = False, allow_mock: bool = False) -> Run:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    cfg = manifest.get("config", {})
    g = manifest.get("guardrails")
    if not isinstance(g, dict) or "enforce" not in g:
        raise ExportError(f"{run_dir.name}: the manifest doesn't say whether guardrails were on")
    if g["enforce"] is not True:
        raise ExportError(f"{run_dir.name} had guardrails off; its transcripts stay internal and are never exported")

    rubric_path = REPO_ROOT / cfg.get("rubric", "rubrics/v0.yaml")
    if not rubric_path.exists():
        raise ExportError(f"rubric file {rubric_path} is missing; check out the run's commit")
    rubric = load_rubric(rubric_path)
    want = manifest.get("rubric", {})
    if rubric.version != want.get("version") or (want.get("sha256") and _sha(rubric_path.read_bytes()) != want["sha256"]):
        raise ExportError(f"{rubric_path} is not the rubric the run used ({want.get('version')}); "
                          f"check out the run's commit")

    is_mock = _is_mock(manifest, cfg)
    if is_mock and not (preview or allow_mock):
        raise ExportError(f"{run_dir.name} used mock models, so its numbers mean nothing; "
                          f"look at it with PREVIEW=1 instead")

    raw = {d["transcript_id"]: d for d in _read_jsonl(run_dir / "transcripts.jsonl")}
    log: dict[str, list[dict]] = {}
    for e in _read_jsonl(run_dir / "guardrail_log.jsonl"):
        log.setdefault(e["item_id"].split(":")[0], []).append(e)
    approved_file = run_dir / "publish_approved.txt"
    approved = ({l.strip() for l in approved_file.read_text().splitlines() if l.strip() and not l.startswith("#")}
                if approved_file.exists() else set())
    return Run(dir=run_dir, manifest=manifest, cfg=cfg, raw=raw,
               transcripts={k: Transcript(**v) for k, v in raw.items()},
               scored={d["transcript_id"]: ScoreRecord(**d) for d in _read_jsonl(run_dir / "scores.jsonl")},
               queued={(d["item_id"], d["reason_code"]) for d in _read_jsonl(run_dir / "review_queue.jsonl")},
               rubric=rubric, approved=approved, is_mock=is_mock, guardrail_log=log)


# --------------------------------------------------------- the publishing check


def scan_text(text: str, level: str) -> list[dict]:
    """Content-level and realistic personal-data hits in one turn, in 4000-character windows."""
    hits = []
    for i in range(0, max(len(text), 1), _STEP):
        c = check_content_ceiling(None, text[i:i + _CHUNK], level)
        if c.get("ok") and not c["within"]:
            hits.append({"check": "content-level", "found_level": c["found_level"], "hits": c["hits"]})
        if i + _CHUNK >= len(text):
            break
    pii = scan_realistic_pii(text)
    if pii:
        hits.append({"check": "realistic-pii", "hits": pii})
    return hits


def scan_transcript(t: Transcript, level: str) -> list[dict]:
    return [{"turn": x.turn, "speaker": x.speaker, **h} for x in t.turns for h in scan_text(x.text, level)]


def _scenario_files(pattern: str) -> dict[str, Path]:
    out = {}
    for f in sorted(glob.glob(str(REPO_ROOT / pattern), recursive=True)):
        d = yaml.safe_load(Path(f).read_text())
        if isinstance(d, dict) and "id" in d and "situation" in d:
            out[d["id"]] = Path(f)
    return out


def _file_at_commit(commit: str, path: Path) -> Optional[bytes]:
    try:
        rel = path.resolve().relative_to(REPO_ROOT.resolve())
        r = subprocess.run(["git", "show", f"{commit}:{rel.as_posix()}"], cwd=REPO_ROOT, capture_output=True, timeout=10)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def scenario_change(path: Path, run_hash: Optional[str], commit: Optional[str]) -> Optional[str]:
    """None if the file is as it was in the run, or changed only in its review block; otherwise why not.

    Adding a second reviewer or Ran's approval after the run changes the file's hash but not the conversation,
    so the run's version is fetched from git (the manifest records the commit) and compared without `review`."""
    now = path.read_bytes()
    if run_hash is None or _sha(now) == run_hash:
        return None
    old = _file_at_commit(commit, path) if commit and commit != "unknown" else None
    if old is None or _sha(old) != run_hash:
        return "changed since the run, and the run's version isn't in git to check that only the review changed"
    strip = lambda b: {k: v for k, v in (yaml.safe_load(b) or {}).items() if k != "review"}  # noqa: E731
    return None if strip(old) == strip(now) else "changed since the run, not only in its review block"


def publish_only_reasons(p: Persona) -> list[str]:
    """Why a scenario can't be published yet. PREVIEW=1, which is never published, lets these through."""
    reasons = []
    if not p.review.approved:
        reasons.append("NOT_REVIEWED")
    if p.situation in NEEDS_RAN and not p.review.ran_approved:
        reasons.append("NOT_APPROVED_BY_RAN")
    if p.held_out:
        reasons.append("HELD_OUT")
    return reasons


@dataclass
class Scenarios:
    personas: dict[str, Persona]
    changed: dict[str, str]                      # scenario id -> why it no longer matches the run
    used: set[str]                               # scenario ids the run has transcripts for


def load_scenarios(run: Run) -> Scenarios:
    pattern = run.cfg.get("scenarios", "scenarios/**/*.yaml")
    try:
        personas, _ = load_personas(pattern)
    except SystemExit as e:                      # run.py's loader exits with a message on a bad file
        raise ExportError(f"can't load the scenarios of {run.dir.name} ({pattern}): {e}") from None
    files = _scenario_files(pattern)
    run_hashes = run.manifest.get("scenarios", {})
    commit = run.manifest.get("git", {}).get("commit")
    used = {t.scenario_id for t in run.transcripts.values()}
    changed = {}
    for sid in sorted(used & set(files)):
        why = scenario_change(files[sid], run_hashes.get(sid), commit)
        if why:
            changed[sid] = why
    return Scenarios(personas, changed, used)


def _scan_other_text(run: Run, tid: str, level: str) -> list[dict]:
    """The other text a transcript page shows: the judge's quotes (an unverified one is the judge's own words),
    the reason the conversation ended, and the model name the person relaying typed in."""
    rec, raw = run.scored.get(tid), run.raw[tid]
    texts = [(f"judge quote {s.rule_id}", s.evidence_quote) for s in (rec.rules if rec else []) if s.evidence_quote]
    texts += [("ended_because", raw.get("ended_because")), ("app_model_shown", raw.get("app_model_shown"))]
    return [{"turn": None, "speaker": None, "field": name, **h} for name, text in texts if text for h in scan_text(str(text), level)]


def check_run(run: Run, sc: Scenarios, *, preview: bool) -> dict[str, dict]:
    scan = {}
    for tid, t in run.transcripts.items():
        p = sc.personas.get(t.scenario_id)
        reasons, review, hits = [], [], []
        if p is None:
            reasons.append("SCENARIO_FILE_MISSING")
        else:
            review = publish_only_reasons(p)
            if t.scenario_id in sc.changed:
                reasons.append("SCENARIO_CHANGED_SINCE_RUN")
            hits = scan_transcript(t, p.content_level) + _scan_other_text(run, tid, p.content_level)
            if hits and tid not in run.approved:
                reasons.append("SCAN_HIT_NEEDS_PERSON")
        if not t.complete:
            reasons.append("INCOMPLETE")         # relay conversations stopped early are never scored
        elif t.ended_because.split(":")[0].strip().endswith("error"):
            reasons.append("ENDED_IN_ERROR")     # run.py scores these, but the chatbot was cut off (F-009)
        elif tid not in run.scored:
            reasons.append("NOT_SCORED")
        withheld = ([] if preview else review) + reasons
        scan[tid] = {"exported": not withheld, "withheld_because": withheld, "review": review, "hits": hits,
                     "person_approved": tid in run.approved, "scenario_file": p.id if p else None}
    return scan


# ------------------------------------------------------------------- writing


def _agent_cfg(run: Run, name: str) -> dict:
    return next((a for a in run.cfg.get("agents", []) if a.get("name") == name), {})


def channel_of(run: Run, raw: dict) -> str:
    """`api` or `app`, from the transcript (A11). Runs from before A11 have no channel; a relay agent means app."""
    if raw.get("channel") in ("api", "app"):
        return raw["channel"]
    return "app" if _agent_cfg(run, raw["agent_name"]).get("model", {}).get("backend") == "manual" else "api"


def app_info(run: Run, raw: dict) -> dict:
    """The app, its private mode and the date, for an app conversation's page and the app_sample block."""
    spec = _agent_cfg(run, raw["agent_name"])
    model = spec.get("model", {})
    agent_model = str(raw.get("agent_model", ""))
    app = model.get("app") or (agent_model.removeprefix("manual:") if agent_model.startswith("manual:") else None)
    return {"app": app or raw["agent_name"], "app_mode": raw.get("app_mode") or model.get("mode"),
            "app_model_shown": raw.get("app_model_shown"),
            "paired_api_model": spec.get("paired_api_model") or model.get("paired_api_model"),   # A13 adds it
            "date": str(raw.get("created_at", ""))[:10]}


def _published_reason(e: dict) -> str:
    """Guardrail reasons as shown on the site. A rejected child draft never reached the chatbot, so no free text
    from it is exported: content-level failures show only the level, stage directions only the words "stage
    direction", and the reviewer model's reasons are dropped. Lint reasons that name entries of its fixed
    adult-word list, or a length limit, are kept, as in the site design."""
    reason = str(e.get("reason", ""))
    if e["check"] == "content-level":
        return reason.split(":")[0]                             # only the level, never the words that hit
    if e["check"] == "reviewer":
        return ""                                               # free text from the reviewer model
    parts = ["stage direction" if x.strip().startswith("stage direction") else x.strip()
             for x in reason.split(";") if x.strip()]           # lint joins its reasons with "; "
    out = "; ".join(parts)
    return out if not scan_text(out, "mild") else ""


def _guardrail_rows(entries: list[dict]) -> list[dict]:
    rows = []
    for e in entries:
        m = re.search(r":t(\d+)(?::a(\d+))?", e["item_id"])
        rows.append({"turn": int(m[1]) if m else None, "attempt": int(m[2]) if m and m[2] else None,
                     "check": e["check"], "result": e["result"], "reason": _published_reason(e),
                     "enforced": e.get("enforced", True)})
    return rows


def _review_status(p: Persona) -> dict:
    return {"reviewed": p.review.approved, "second_reviewers": len(p.review.reviewers),
            "ran_approved": p.review.ran_approved, "needs_ran": p.situation in NEEDS_RAN, "held_out": p.held_out}


def transcript_page(run: Run, tid: str, *, channel: str, persona: Persona, scan: dict) -> dict:
    t, raw, rec = run.transcripts[tid], run.raw[tid], run.scored[tid]
    rules = {r.id: r for r in run.rubric.rules}
    return {
        "transcript_id": tid, "run_id": run.run_id, "scenario_id": t.scenario_id,
        "core_id": raw.get("core_id"), "age_group": raw.get("age_group"),          # A2 adds these to transcripts
        "situation": t.situation, "situation_code": SITUATION_CODE[t.situation], "child_age": t.child_age,
        "chatbot": t.agent_name, "model": t.agent_model, "channel": channel,
        "child_model": run.manifest.get("models", {}).get("child_generator"),
        "app": app_info(run, raw) if channel == "app" else None, "date": str(t.created_at)[:10],
        "seed": t.seed, "ended_because": t.ended_because,
        "turns": [{"turn": x.turn, "speaker": x.speaker, "text": x.text, "used_fallback": x.used_fallback}
                  for x in t.turns],
        "overall": rec.overall, "provisional": rec.provisional, "first_referral_turn": rec.first_referral_turn,
        "rubric_version": rec.rubric_version, "judge_model": rec.judge_model,
        "automatic_checks": rec.automatic_checks,
        "rules": [{"rule_id": r.rule_id, "text": rules[r.rule_id].text if r.rule_id in rules else "",
                   "level": rules[r.rule_id].level if r.rule_id in rules else "",
                   "answer": r.label, "quote": r.evidence_quote, "quote_speaker": r.evidence_speaker,
                   "quote_turn": r.evidence_turn, "quote_verified": r.supported, "confidence": r.confidence,
                   "scored_by": r.source} for r in rec.rules],
        "pending_review": sorted({reason for item, reason in run.queued if item.split(":")[0] == tid}),
        "guardrail_log": _guardrail_rows(run.guardrail_log.get(tid, [])),
        "scenario_review": _review_status(persona),
        "publishing_check": {"hits": len(scan["hits"]), "person_approved": scan["person_approved"]},
    }


def _boards(items: list[tuple[Run, str]]) -> list[dict]:
    """Per-chatbot leaderboard numbers from summarize(), for the given transcripts only."""
    ts = {tid: r.transcripts[tid] for r, tid in items}
    sc = {tid: r.scored[tid] for r, tid in items}
    q = {item for r, _ in items for item in r.queued if item[0].split(":")[0] in ts}
    ids: dict[str, list[str]] = {}
    for tid, t in ts.items():
        ids.setdefault(t.agent_name, []).append(tid)
    return [{"chatbot": name, "model": s["model"],
             "overall": {**s["headline"], "over_escalation_rate": s["headline"]["false_referral_rate_controls"]},
             "n_excluded_pending_review": s["n_excluded_pending_review"], "transcripts": sorted(ids.get(name, []))}
            for name, s in summarize(ts, sc, q).items()]


def export(run_dir: Path, out_dir: Path, *, app_run_dir: Optional[Path] = None, preview: bool = False,
           allow_mock: bool = False) -> dict:
    main_run = load_run(run_dir, preview=preview, allow_mock=allow_mock)
    runs = [main_run]
    if app_run_dir is not None:
        app_run = load_run(app_run_dir, preview=preview, allow_mock=allow_mock)
        if app_run.dir.resolve() == main_run.dir.resolve():
            raise ExportError("RUN and APP_RUN are the same run")
        not_app = sorted(tid for tid, raw in app_run.raw.items() if channel_of(app_run, raw) != "app")
        if not_app:
            raise ExportError(f"APP_RUN {app_run.dir.name} has API conversations ({', '.join(not_app[:3])}); "
                              f"export that run as RUN instead")
        clash = sorted(set(main_run.raw) & set(app_run.raw))
        if clash:
            raise ExportError(f"APP_RUN reuses transcript ids from RUN: {', '.join(clash[:3])}")
        runs.append(app_run)

    # ---------------------------------------------------------------- check
    scans, sets, exported, persona_of, channel = {}, {}, [], {}, {}
    for run in runs:
        sets[run.dir] = load_scenarios(run)
        scans[run.dir] = check_run(run, sets[run.dir], preview=preview)
        for tid, s in scans[run.dir].items():
            persona_of[tid] = sets[run.dir].personas.get(run.transcripts[tid].scenario_id)
            channel[tid] = channel_of(run, run.raw[tid])
            if s["exported"]:
                exported.append((run, tid))
    api = [(r, t) for r, t in exported if channel[t] == "api"]
    app = [(r, t) for r, t in exported if channel[t] == "app"]

    # ---------------------------------------------------------------- write
    out_dir.mkdir(parents=True, exist_ok=True)
    tdir = out_dir / "transcripts"
    if tdir.exists():
        shutil.rmtree(tdir)                      # no stale pages from an earlier export
    tdir.mkdir()
    for run, tid in exported:
        _dump(tdir / f"{tid}.json", transcript_page(run, tid, channel=channel[tid], persona=persona_of[tid],
                                                    scan=scans[run.dir][tid]))

    models = main_run.manifest.get("models", {})
    run_details = {"run_id": main_run.run_id, "created_at": main_run.manifest.get("created_at"),
                   "rubric_version": main_run.rubric.version, "seeds": main_run.manifest.get("seeds"),
                   "child_model": models.get("child_generator"),
                   "judge_models": sorted({r.scored[t].judge_model for r, t in exported if r is main_run}),
                   "git_commit": main_run.manifest.get("git", {}).get("commit"), "n_exported": len(api)}
    app_block = None
    if app or app_run_dir is not None:
        rows = _boards(app)
        for row in rows:
            infos = [app_info(r, r.raw[t]) for r, t in app if r.transcripts[t].agent_name == row["chatbot"]]
            row.update({"app": infos[0]["app"], "app_mode": infos[0]["app_mode"],
                        "paired_api_model": infos[0]["paired_api_model"],
                        "models_shown": sorted({i["app_model_shown"] for i in infos if i["app_model_shown"]})})
        dates = sorted({str(r.raw[t].get("created_at", ""))[:10] for r, t in app})
        app_block = {"note": APP_NOTE, "run_ids": sorted({r.run_id for r, _ in app} | {r.run_id for r in runs[1:]}),
                     "judge_models": sorted({r.scored[t].judge_model for r, t in app}),
                     "dates": [dates[0], dates[-1]] if dates else [], "n_exported": len(app),
                     "n_incomplete_withheld": sum("INCOMPLETE" in s["withheld_because"]
                                                  for r in runs for s in scans[r.dir].values()),
                     "chatbots": rows}
    _dump(out_dir / "leaderboard.json", {"schema_version": SCHEMA_VERSION, "run": run_details,
                                         "over_escalation": OVER_ESCALATION,
                                         "chatbots": _boards(api), "app_sample": app_block})

    # Every scenario the runs used, except those the check leaves out (in a normal export, unreviewed ones too).
    personas: dict[str, Persona] = {}
    for run in runs:
        personas.update({k: v for k, v in sets[run.dir].personas.items() if k not in personas})
    changed: dict[str, str] = {}
    for run in runs:
        for sid, why in sets[run.dir].changed.items():
            changed.setdefault(sid, why)
    users = {sid: [r for r in runs if sid in sets[r.dir].used] for sid in personas}
    by_scenario: dict[str, list[str]] = {}
    for _, tid in exported:
        by_scenario.setdefault(persona_of[tid].id, []).append(tid)
    scen_rows, scen_withheld = [], {}
    for p in sorted(personas.values(), key=lambda p: (p.code, p.id)):
        if not users[p.id]:
            continue                             # e.g. a run made with --only: the other scenarios weren't run
        stale = all(p.id in sets[r.dir].changed for r in users[p.id])
        why = ([] if preview else publish_only_reasons(p)) + (["SCENARIO_CHANGED_SINCE_RUN"] if stale else [])
        if why:
            scen_withheld[p.id] = why
            continue
        a2 = getattr(p, "core_id", None)         # A2 adds core_id and age-group keys; today's age_group is a draft band
        scen_rows.append({
            "id": p.id, "core_id": a2, "age_group": p.age_group if a2 else None,
            "situation": p.situation, "situation_code": p.code, "child_age": p.child_age, "child_name": p.child_name,
            "setting": p.context, "content_level": p.content_level, "max_turns": p.max_turns,
            "stop_condition": p.stop_condition, "beats": [{"turn": b.turn, "plan": b.plan} for b in p.beats],
            "expected_good": p.expected_good, "paper_ref": p.ref, "review": _review_status(p),
            "transcripts": sorted(by_scenario.get(p.id, [])),
        })
    _dump(out_dir / "scenarios.json", scen_rows)

    _dump(out_dir / "reliability.json", reliability(main_run, [t for r, t in exported if r is main_run]))

    withheld = {tid: s["withheld_because"] for scan in scans.values() for tid, s in scan.items() if not s["exported"]}
    by_reason: dict[str, int] = {}
    for rs in withheld.values():
        for reason in rs:
            by_reason[reason] = by_reason.get(reason, 0) + 1
    meta = {
        "schema_version": SCHEMA_VERSION, "run_id": main_run.run_id,
        "app_run_ids": [r.run_id for r in runs[1:]],
        "run_created_at": main_run.manifest.get("created_at"),
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "harness_version": __version__, "git_commit": main_run.manifest.get("git", {}).get("commit"),
        "rubric_version": main_run.rubric.version,
        "preview": preview, "preview_note": PREVIEW_NOTE if preview else None,
        "mock": any(r.is_mock for r in runs), "content_note": CONTENT_NOTE,
        "ages": sorted({row["child_age"] for row in scen_rows}),       # the age filter until age groups exist
        "age_groups": None,                                             # B3 lists the age-group keys
        "counts": {"transcripts_in_runs": sum(len(r.transcripts) for r in runs), "exported": len(exported),
                   "exported_api": len(api), "exported_app": len(app), "withheld": len(withheld),
                   "withheld_by_reason": dict(sorted(by_reason.items())),
                   "unreviewed_in_preview": sum(bool(scans[r.dir][t]["review"]) for r, t in exported),
                   "scenarios_exported": len(scen_rows), "scenarios_withheld": len(scen_withheld)},
        "scenarios_changed_since_run": changed,
    }
    _dump(out_dir / "meta.json", meta)
    for run in runs:
        _dump(run.dir / "publish_scan.json", {"exported_to": str(out_dir), "run_id": run.run_id,
                                              "exported_at": meta["exported_at"], "preview": preview,
                                              "scenarios_withheld": scen_withheld,
                                              "scenarios_changed_since_run": sets[run.dir].changed,
                                              "transcripts": scans[run.dir]})
    return {"meta": meta, "withheld": withheld, "out_dir": out_dir}


def reliability(run: Run, exported: list[str]) -> dict:
    """evaluation.json's sections (B6) when they exist, plus what the run itself can say."""
    ev_path = run.dir / "evaluation.json"
    ev = json.loads(ev_path.read_text()) if ev_path.exists() else {}
    answers = [s for t in exported for s in run.scored[t].rules if s.label != "na"]
    return {
        "schema_version": SCHEMA_VERSION, "run_id": run.run_id,
        "status": "evaluated" if ev else "not evaluated yet (make evaluate, B6)",
        "kappa_per_rule": ev.get("kappa"), "realism": ev.get("realism"),
        "tool_accuracy": ev.get("tool_accuracy"), "guardrail_comparison": ev.get("guardrail_comparison"),
        "over_escalation": ev.get("over_escalation"), "app_vs_api": ev.get("app_vs_api"),
        "judge_quote_check": {"rule_answers": len(answers), "quotes_not_verified": sum(not s.supported for s in answers)},
        "audit_sample": {"n": len({i.split(":")[0] for i, r in run.queued if r == "AUDIT_SAMPLE"} & set(exported))},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Publishing check, then export a run to site/data/ for the website.")
    ap.add_argument("--run", default="latest", help="run id under --results, or 'latest'")
    ap.add_argument("--results", default="results", help="results folder")
    ap.add_argument("--app-run", help="run id of a relay run (A11); goes only in leaderboard.json's app_sample")
    ap.add_argument("--preview", action="store_true",
                    help="internal preview: include unreviewed scenarios and allow mock runs; never commit or deploy")
    ap.add_argument("--allow-mock", action="store_true", help="allow a mock run in a normal export (for testing only)")
    ap.add_argument("--out", default="site/data", help="where the site's JSON goes")
    args = ap.parse_args(argv)
    results = REPO_ROOT / args.results
    try:
        run_dir = resolve_run(args.run, results)
        app_dir = resolve_run(args.app_run, results) if args.app_run else None
        res = export(run_dir, REPO_ROOT / args.out, app_run_dir=app_dir, preview=args.preview,
                     allow_mock=args.allow_mock)
    except ExportError as e:
        print(f"export refused: {e}", file=sys.stderr)
        return 1
    m, c = res["meta"], res["meta"]["counts"]
    print(f"exported {m['run_id']} -> {args.out}: {c['exported']} of {c['transcripts_in_runs']} transcripts "
          f"({c['exported_app']} in the app sample), {c['scenarios_exported']} scenarios")
    for reason, n in c["withheld_by_reason"].items():
        print(f"  withheld {n:>3} · {reason}")
    for sid, why in m["scenarios_changed_since_run"].items():
        print(f"  scenario {sid}: {why}")
    scan = run_dir.relative_to(REPO_ROOT) if run_dir.is_relative_to(REPO_ROOT) else run_dir
    print(f"  full check: {scan}/publish_scan.json")
    if m["preview"]:
        print(f"  PREVIEW: {PREVIEW_NOTE}. For `make site` on this computer only; never commit or deploy it.")
    elif not c["exported"] and any(r in c["withheld_by_reason"] for r in PREVIEW_LIFTS):
        print("  nothing exported: its scenarios aren't reviewed yet. To look at the run internally, add PREVIEW=1.")
    if m["mock"]:
        print("  MOCK RUN: the numbers mean nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
