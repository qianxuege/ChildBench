"""Score a saved run again, without calling any chatbot or the pretend child (RFC 0010, packages B1 and B12).

    make score RUN=<id>                          # the run's own rubric and judge
    make score RUN=<id> RUBRIC=rubrics/my.yaml   # another rubric
    make score RUN=<id> JUDGE_GUARDRAILS=off     # no quote retry: what the judge says first is kept (RFC 0006)
    make score RUN=<id> JUDGE_CONFIG=configs/x.yaml LABEL=qwen   # another config's judge, and a folder name
    make score RUN=<id> ONLY="dist-003__gemma-270m__s1"          # some transcripts only
    make score RUN=<id> PROFILE=rubrics/profiles/my.yaml         # new weights, levels, on/off: recompute, no AI
    make score RUN=<id> PROFILE=... FROM=<label>                 # recompute from rescore_<label>/, not the run's own scores
    make score RUN=<id> RUBRIC=... PROFILE=...                   # run the judge, then score with this profile

PROFILE without RUBRIC, JUDGE_CONFIG or JUDGE_GUARDRAILS=off only recomputes saved labels, and refuses if the rubric's
wording has changed since. Profiles apply from rubric v1; without PROFILE, the default one is used.

Writes results/<run>/rescore_<label>/: scores.jsonl, judge_traces.jsonl, review_queue.jsonl, tool_calls.jsonl,
summary.json and score_manifest.json. The run's own files and results/LATEST are never changed, and an existing
folder is never overwritten. Incomplete conversations, and those cut off by a model error, are not scored.

With guardrails off, only the quote retry is skipped. Quotes are still checked and marked unsupported, so the two
can be compared, and code still overrides S2.1 and computes the rating. The judge's guardrails are on by default
here, whatever the run used, so a guardrails-off run can be scored the same way as a guardrails-on run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

from . import REPO_ROOT, __version__
from .agents.judge import Judge
from .context import RunContext
from .llm import ModelError, make_model
from .rating import DEFAULT_PROFILE, apply_profile, load_profile
from .run import git_info, load_personas, load_rubric, model_usage, preflight
from .schemas import SITUATION_CODE, Persona, Profile, Rubric, ScoreRecord, Transcript
from .summary import print_table, summarize
from .tools.review import flag_for_human_review

# Review items about the conversation itself, not about a judge's labels. They still apply to a new scoring.
FROM_THE_CHAT = {"FALLBACK_USED", "PERSONA_TOO_INTENSE", "PERSONA_DRIFT", "REVIEWER_UNSURE"}


class ScoreError(Exception):
    pass


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]     # the same hash run.py records


def _path(p: str) -> Path:
    path = Path(p) if Path(p).is_absolute() else REPO_ROOT / p
    if not path.is_file():
        raise ScoreError(f"no file {p}")
    return path


def resolve_run(run: str, results: Path) -> Path:
    if run == "latest":
        latest = results / "LATEST"
        if not latest.exists():
            raise ScoreError(f"no {latest}; pass RUN=<run_id>")
        run = latest.read_text().strip()
    d = results / run
    if not (d / "manifest.json").exists() or not (d / "transcripts.jsonl").exists():
        raise ScoreError(f"{d} is not a run folder (needs manifest.json and transcripts.jsonl)")
    return d


def why_not_scored(t: Transcript) -> Optional[str]:
    """Why a conversation is never scored, or None. Before A15, a model error was saved as a complete conversation."""
    if not t.complete:
        return "incomplete"
    if t.ended_because.split(":")[0].strip().endswith("error"):
        return "cut off by a model error"
    return None


def get_profile(profile_file: Optional[str], rubric: Rubric) -> Optional[Profile]:
    """The profile to score with: the file given, else the default one. None for v0, which has no profiles."""
    if not rubric.has_domains and not profile_file:
        return None
    try:
        return load_profile(_path(profile_file) if profile_file else DEFAULT_PROFILE, rubric)
    except ValueError as e:
        raise ScoreError(str(e)) from e


def profile_info(profile: Optional[Profile], rubric: Rubric, file: Optional[str]) -> Optional[dict]:
    if profile is None:
        return None
    return {"file": file or str(DEFAULT_PROFILE), "name": profile.name, "hash": profile.settings_hash(),
            "custom": profile.settings_hash() != get_profile(None, rubric).settings_hash()}


def run_scenarios(manifest: dict) -> dict[str, Persona]:
    """The run's own scenarios, for the referral expectation and codes. A run that names none (the gold set) gets none."""
    used = manifest.get("scenarios") or {}
    if not used:
        return {}
    try:
        personas, hashes = load_personas(manifest["config"]["scenarios"])
    except SystemExit as e:
        raise ScoreError(f"can't load the run's scenarios: {e}") from e
    if gone := sorted(set(used) - set(personas)):
        raise ScoreError(f"scenario files missing: {', '.join(gone)}; check out the run's commit")
    if changed := sorted(k for k in used if used[k] != hashes[k]):
        print(f"note: changed since this run, so its referral expectation or codes may differ: {', '.join(changed)}")
    return {k: personas[k] for k in used}


def recompute(run_dir: Path, profile_file: str, *, source: Optional[str] = None,
              label: Optional[str] = None, only: Optional[list[str]] = None) -> Path:
    """Apply another profile to labels that already exist. No model is called. Returns the folder it wrote."""
    src = run_dir / f"rescore_{source}" if source else run_dir
    if not (src / "scores.jsonl").is_file():
        raise ScoreError(f"no scores in {src}")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    made = json.loads((src / "score_manifest.json").read_text()) if source else {}
    rubric_path = _path(made.get("rubric", {}).get("file") or manifest["config"].get("rubric", "rubrics/v0.yaml"))
    rubric = load_rubric(rubric_path)
    profile = get_profile(profile_file, rubric)
    records = [ScoreRecord(**d) for d in _read_jsonl(src / "scores.jsonl")]
    if only:
        if unknown := sorted(set(only) - {r.transcript_id for r in records}):
            raise ScoreError(f"not scored in {src.name}: {', '.join(unknown)}")
        records = [r for r in records if r.transcript_id in only]
    try:
        scored = {r.transcript_id: apply_profile(r, rubric, profile) for r in records}
    except ValueError as e:
        raise ScoreError(str(e)) from e

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    label, out = _out_dir(run_dir, label or f"{stamp}_{rubric.version}_profile-{re.sub(r'[^A-Za-z0-9._-]+', '-', profile.name)}")
    out.mkdir(parents=True)
    with open(out / "scores.jsonl", "w", encoding="utf-8") as f:
        f.writelines(r.model_dump_json() + "\n" for r in scored.values())
    queue = _read_jsonl(src / "review_queue.jsonl")           # the labels are the same, so what needs a person is too
    (out / "review_queue.jsonl").write_text("".join(json.dumps(d) + "\n" for d in queue), encoding="utf-8")
    flags = {(d["item_id"], d["reason_code"]) for d in queue}
    if source:
        flags |= {(d["item_id"], d["reason_code"]) for d in _read_jsonl(run_dir / "review_queue.jsonl") if d["reason_code"] in FROM_THE_CHAT}
    transcripts = {d["transcript_id"]: Transcript(**d) for d in _read_jsonl(run_dir / "transcripts.jsonl") if d["transcript_id"] in scored}
    summary = summarize(transcripts, scored, flags)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    (out / "score_manifest.json").write_text(json.dumps({
        "run_id": manifest["run_id"], "label": label, "created_at": stamp, "harness_version": __version__, "git": git_info(),
        "recomputed_from": src.name if source else "the run's own scores",
        "rubric": {"file": str(rubric_path), "version": rubric.version, "sha256": _sha(rubric_path)},
        "profile": profile_info(profile, rubric, profile_file),
        "judge": made.get("judge") or {"model": manifest.get("models", {}).get("judge")},
        "judge_guardrails": all(r.judge_guardrails for r in scored.values()), "run_guardrails": manifest.get("guardrails"),
        "transcripts_sha256": _sha(run_dir / "transcripts.jsonl"), "counts": {"scored": len(scored)},
    }, indent=2))
    print(f"recomputed {len(scored)} chats · rubric {rubric.version} · profile {profile.name} · no model called")
    print_table(summary)
    print(f"\nwrote {out}")
    return out


def _out_dir(run_dir: Path, label: str) -> tuple[str, Path]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
        raise ScoreError(f"label '{label}' may only have letters, digits, '.', '_' and '-'")
    out = run_dir / f"rescore_{label}"
    if out.exists():
        raise ScoreError(f"{out} already exists; choose another LABEL")
    return label, out


def rescore(run_dir: Path, *, rubric_file: Optional[str] = None, judge_config: Optional[str] = None,
            guardrails: bool = True, label: Optional[str] = None, only: Optional[list[str]] = None,
            skip_preflight: bool = False, profile_file: Optional[str] = None) -> Path:
    """Run the judge on a saved run's transcripts. Returns the folder it wrote."""
    manifest = json.loads((run_dir / "manifest.json").read_text())
    run_cfg = manifest["config"]
    rubric_path = _path(rubric_file or run_cfg.get("rubric", "rubrics/v0.yaml"))
    rubric = load_rubric(rubric_path)
    profile = get_profile(profile_file, rubric)
    run_rubric = manifest.get("rubric", {})
    if not rubric_file and run_rubric.get("sha256") not in (None, _sha(rubric_path)):
        print(f"note: {rubric_path.name} has changed since this run, so labels may differ for that reason too")

    judge_cfg = yaml.safe_load(_path(judge_config).read_text()) if judge_config else run_cfg
    spec = judge_cfg["roles"]["judge"]
    if spec.get("backend") == "manual":
        raise ScoreError("the judge can't use backend: manual")

    transcripts = {d["transcript_id"]: Transcript(**d) for d in _read_jsonl(run_dir / "transcripts.jsonl")}
    if only:
        unknown = sorted(set(only) - set(transcripts))
        if unknown:
            raise ScoreError(f"not in this run: {', '.join(unknown)}")
        transcripts = {k: t for k, t in transcripts.items() if k in only}
    skipped = {k: why for k, t in transcripts.items() if (why := why_not_scored(t))}
    for k, t in transcripts.items():
        if k not in skipped and not rubric.scores_situation(SITUATION_CODE[t.situation]):
            skipped[k] = f"rubric {rubric.version} has no rules for '{t.situation}'"
    todo = {k: t for k, t in transcripts.items() if k not in skipped}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    label, out = _out_dir(run_dir, label or f"{stamp}_{rubric.version}{'' if guardrails else '_judge-guardrails-off'}")
    personas = run_scenarios(manifest) if rubric.has_domains else {}

    model = make_model(spec)
    if not skip_preflight:
        problems = preflight([model])
        if problems:
            raise ScoreError("preflight failed, nothing was scored:\n  - " + "\n  - ".join(problems))

    ctx = RunContext(manifest["run_id"], out, rubric, personas, enforce=guardrails,
                     audit_rate=manifest.get("guardrails", {}).get("audit_rate", 0.10), seed=judge_cfg.get("audit_seed", 0))
    ctx.transcripts = todo
    ctx.profile = profile
    judge = Judge(model, ctx, temperature=judge_cfg.get("temperatures", {}).get("judge", 0.0),
                  demo_bad_quote=judge_cfg.get("demo", {}).get("bad_quote", False))
    print(f"scoring {len(todo)} of {len(transcripts)} conversations · rubric {rubric.version} · judge {model.model_id} · "
          f"judge guardrails {'on' if guardrails else 'off'}")
    for tid, why in skipped.items():
        print(f"  skipped  {tid:<40} {why}")
    start = time.perf_counter()
    for tid, t in todo.items():
        t0 = time.perf_counter()
        try:
            rec = judge.score(t, seed=t.seed)
        except ModelError as e:
            # As in run.py: one failed call must not stop the rest, and the server address isn't saved.
            print(f"  judge model error in {tid}: {e}", file=sys.stderr)
            rec = ctx.scored.get(tid)
            if rec is None:
                flag_for_human_review(ctx, tid, "JUDGE_FAILED", f"judge model error: {type(e.__cause__ or e).__name__}")
        status = rec.overall + (" (provisional)" if rec.provisional else "") if rec else "JUDGE FAILED"
        print(f"  scored   {tid:<40} {status} · {time.perf_counter() - t0:.0f} s")

    chat_flags = {(d["item_id"], d["reason_code"]) for d in _read_jsonl(run_dir / "review_queue.jsonl")
                  if d["reason_code"] in FROM_THE_CHAT}
    summary = summarize(todo, ctx.scored, ctx.queued | chat_flags)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    (out / "score_manifest.json").write_text(json.dumps({
        "run_id": manifest["run_id"], "label": label, "created_at": stamp, "harness_version": __version__,
        "git": git_info(), "rubric": {"file": str(rubric_path), "version": rubric.version, "sha256": _sha(rubric_path)},
        "profile": profile_info(profile, rubric, profile_file),
        "judge": {"model": model.model_id, "config": judge_config, "spec": spec},
        "judge_guardrails": guardrails, "run_guardrails": manifest.get("guardrails"),
        "transcripts_sha256": _sha(run_dir / "transcripts.jsonl"),
        "counts": {"transcripts": len(transcripts), "scored": len(ctx.scored), "failed": len(todo) - len(ctx.scored),
                   "skipped": skipped, "review_items": len(ctx.queued)},
        "score_seconds": round(time.perf_counter() - start, 2), "tokens": model_usage([model]),
    }, indent=2))
    print()
    print_table(summary)
    print(f"\nwrote {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the judge again on a saved run's transcripts.")
    ap.add_argument("--run", default="latest", help="run id under --results, or 'latest'")
    ap.add_argument("--results", default="results")
    ap.add_argument("--rubric", help="rubric file (default: the run's)")
    ap.add_argument("--judge-config", help="config to take the judge from (default: the run's)")
    ap.add_argument("--judge-guardrails", choices=["on", "off"], default="on", help="off skips the quote retry")
    ap.add_argument("--profile", help="scoring profile; alone, it recomputes saved labels without a model")
    ap.add_argument("--from", dest="source", help="with --profile: recompute rescore_<label>/ instead of the run's own scores")
    ap.add_argument("--label", help="folder name after rescore_ (default: time and rubric version)")
    ap.add_argument("--only", nargs="*", help="transcript ids")
    ap.add_argument("--skip-preflight", action="store_true")
    a = ap.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")
    try:
        only_recompute = a.profile and not a.rubric and not a.judge_config and a.judge_guardrails == "on"
        if a.source and not only_recompute:
            raise ScoreError("FROM only goes with PROFILE, when nothing is judged again")
        if only_recompute:
            recompute(resolve_run(a.run, REPO_ROOT / a.results), a.profile, source=a.source, label=a.label, only=a.only)
            return 0
        rescore(resolve_run(a.run, REPO_ROOT / a.results), rubric_file=a.rubric, judge_config=a.judge_config,
                guardrails=a.judge_guardrails == "on", label=a.label, only=a.only, skip_preflight=a.skip_preflight, profile_file=a.profile)
    except (ScoreError, ModelError) as e:
        print(f"not scored: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
