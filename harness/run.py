"""Orchestrator: scenarios x chatbots x seeds -> results/<run_id>/ (RFC 0002, 0009).

Plain code, no LLM decides the order of steps. Usage:
    python -m harness.run --config configs/mock.yaml
    python -m harness.run --config configs/gemma-local.yaml --only dist-003 --seeds 1
    python -m harness.run --config configs/mock.yaml --check-only
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import REPO_ROOT, __version__
from .agents.agent_under_test import AgentUnderTest
from .agents.child_simulator import ChildSimulator
from .agents.judge import Judge
from .context import RunContext
from .llm import ModelError, make_model
from .llm.manual import ManualModel, ManualStop
from .schemas import SITUATION_CODE, Persona, Rubric, Transcript, Turn
from .summary import print_table, summarize
from .tools.referral import detect_referral


def load_rubric(path: Path) -> Rubric:
    return Rubric(**yaml.safe_load(path.read_text()))


def load_personas(pattern: str) -> tuple[dict[str, Persona], dict[str, str]]:
    personas, hashes = {}, {}
    files = sorted(glob.glob(str(REPO_ROOT / pattern), recursive=True))
    if not files:
        raise SystemExit(f"no scenario files match {pattern}")
    for f in files:
        raw = Path(f).read_bytes()
        try:
            p = Persona(**yaml.safe_load(raw))
        except Exception as e:
            raise SystemExit(f"invalid scenario {f}:\n{e}")
        folder = Path(f).parent.name
        if folder != p.situation:
            raise SystemExit(f"{f}: situation '{p.situation}' does not match folder '{folder}'")
        if p.id in personas:
            raise SystemExit(f"duplicate scenario id {p.id}")
        personas[p.id] = p
        hashes[p.id] = hashlib.sha256(raw).hexdigest()[:16]
    return personas, hashes


def git_info() -> dict:
    def run(*a):
        try:
            return subprocess.run(["git", *a], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""
    return {"commit": run("rev-parse", "HEAD") or "unknown", "dirty": bool(run("status", "--porcelain"))}


def coverage(personas: dict[str, Persona]) -> str:
    groups = sorted({p.age_group or str(p.child_age) for p in personas.values()})
    lines = [f"{'':<6}" + "".join(f"{g:>8}" for g in groups)]
    for code in ["S1", "S2", "S3", "S4", "S5", "C"]:
        row = f"{code:<6}"
        for g in groups:
            n = sum(1 for p in personas.values() if p.code == code and (p.age_group or str(p.child_age)) == g)
            row += f"{n:>8}"
        lines.append(row)
    return "\n".join(lines)


BASELINE_SECONDS_PER_CONVERSATION = 70  # one-scenario local trial on a 24 GB Mac (README, Known problems)
RELAY_MINUTES_PER_CONVERSATION = 5      # a person relaying to a free app (RFC 0010, section 7)


def model_usage(models) -> dict[str, int]:
    """Sum the token counters of every model that keeps them (mock models don't)."""
    total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    for m in models:
        if hasattr(m, "usage"):
            for k, v in m.usage().items():
                total[k] += v
    return total


def usage_delta(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {k: after[k] - before.get(k, 0) for k in after}


def preflight(models) -> list[str]:
    """Ask each distinct server/model pair whether it's ready. Returns error messages (empty = all ready)."""
    errors, seen = [], set()
    for m in models:
        key = (getattr(m, "base_url", None), m.model_id)
        if not hasattr(m, "preflight") or key in seen:
            continue
        seen.add(key)
        err = m.preflight()
        if err:
            errors.append(err)
    return errors


def past_timing(results_dir: Path, run_name: str) -> dict | None:
    """Average timing from earlier runs of the same config, newest first, for `--estimate`."""
    talk, score, runs = [], [], 0
    for mf in sorted(results_dir.glob(f"*_{run_name}_*/manifest.json"), reverse=True):
        try:
            t = json.loads(mf.read_text()).get("timing")
        except (OSError, json.JSONDecodeError):
            continue
        if not t or not t.get("conversations"):
            continue
        runs += 1
        talk.append(t["talk_seconds"] / t["conversations"])
        score.append(t["score_seconds"] / t["conversations"])
        if runs == 3:
            break
    if not runs:
        return None
    return {"runs": runs, "talk": sum(talk) / runs, "score": sum(score) / runs}


def estimate(cfg: dict, n_personas: int, n_seeds: int, results_dir: Path) -> None:
    n_agents = len(cfg["agents"])
    n = n_personas * n_agents * n_seeds
    has_rev = bool(cfg["roles"].get("child_reviewer"))
    print(f"\nestimate: {n_personas} scenarios x {n_agents} chatbots x {n_seeds} seeds = {n} conversations")
    print(f"  model calls, at most ~{n * 6 * (2 + (1 if has_rev else 0)) + n * 14} (turn cap 6, judge cap 14 steps)")
    name = cfg.get("run_name", "run")
    past = past_timing(results_dir, name)
    if past:
        total = n * (past["talk"] + past["score"])
        print(f"  time, from the last {past['runs']} '{name}' run(s): ~{past['talk']:.0f} s talking + "
              f"~{past['score']:.0f} s scoring per conversation = about {total / 60:.0f} min")
    elif any(a["model"].get("backend") == "manual" for a in cfg["agents"]):
        print(f"  time: relay conversations are run by a person, about {RELAY_MINUTES_PER_CONVERSATION} min each, "
              f"so about {n * RELAY_MINUTES_PER_CONVERSATION} min of relaying, plus scoring")
    else:
        total = n * BASELINE_SECONDS_PER_CONVERSATION
        print(f"  time: no earlier '{name}' run to learn from; at the baseline's ~{BASELINE_SECONDS_PER_CONVERSATION} s per "
              f"conversation, talking takes about {total / 60:.0f} min, plus scoring")
    print("  cost: not estimated yet (A13 adds per-model prices); local Ollama runs are free")


def converse(persona: Persona, agent: AgentUnderTest, child: ChildSimulator, ctx: RunContext, seed: int) -> Transcript:
    tid = f"{persona.id}__{agent.name}__s{seed}"
    history: list[Turn] = []
    pushed_back = False
    last_beat = persona.beats[-1].turn
    ended = "turn limit"
    complete = True
    relay = isinstance(agent.model, ManualModel)
    for turn in range(1, persona.max_turns + 1):
        try:
            text, used_fallback, attempts, phase = child.utterance(persona, tid, turn, history, seed, pushed_back)
            if phase == "pushback":
                pushed_back = True
            history.append(Turn(turn=turn, speaker="child", text=text, used_fallback=used_fallback, attempts=attempts))
            reply = agent.reply(history, seed)
        except KeyboardInterrupt:
            if not relay:
                raise   # automated runs: Ctrl+C quits at once, as before
            # Relay runs: keep what's finished. Save this conversation as incomplete, skip the rest, still score.
            print("\n  Ctrl+C: this conversation is saved as incomplete, the rest of the run is skipped, and finished "
                  "conversations are scored now. Press Ctrl+C again to quit at once.")
            if history and history[-1].speaker == "child" and history[-1].turn == turn:
                history.pop()   # the app never answered this line
            ended, complete = "stopped with Ctrl+C", False
            agent.model.quit_requested = True
            break
        except ManualStop:
            history.pop()   # the person never sent this child line to the app
            ended, complete = "stopped by the person relaying", False
            break
        except ModelError as e:
            ended = f"agent error: {e}"
            break
        history.append(Turn(turn=turn, speaker="agent", text=reply))
        script_done = turn >= last_beat
        if persona.situation == "control":
            if script_done:
                ended = "script complete"
                break
            continue
        # The stop condition is only checked once every scripted beat has been delivered,
        # so each scenario always reaches its key moment.
        if script_done and detect_referral(reply):
            if persona.pushback_once and not pushed_back:
                continue  # the child resists once on the next turn
            ended = "referred again after push-back" if pushed_back else "referred"
            break
    manual = agent.model if isinstance(agent.model, ManualModel) else None
    return Transcript(transcript_id=tid, run_id=ctx.run_id, scenario_id=persona.id, situation=persona.situation,
                      child_age=persona.child_age, agent_name=agent.name, agent_model=agent.model_id, seed=seed,
                      turns=history, ended_because=ended, complete=complete,
                      channel="app" if manual else "api", app_mode=manual.mode if manual else None,
                      app_model_shown=manual.model_shown if manual else None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the ChildBench pipeline.")
    ap.add_argument("--config", required=True)
    ap.add_argument("--only", nargs="*", help="scenario ids to run")
    ap.add_argument("--seeds", nargs="*", type=int)
    ap.add_argument("--check-only", action="store_true", help="validate scenarios and rubric, then exit")
    ap.add_argument("--out", default="results")
    ap.add_argument("--estimate", action="store_true", help="print conversation count and expected time, then exit")
    ap.add_argument("--skip-preflight", action="store_true", help="don't check that model servers are up before running")
    args = ap.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")

    cfg_path = Path(args.config)
    cfg = yaml.safe_load((cfg_path if cfg_path.is_absolute() else REPO_ROOT / cfg_path).read_text())
    rubric_path = REPO_ROOT / cfg.get("rubric", "rubrics/v0.yaml")
    rubric = load_rubric(rubric_path)
    personas, hashes = load_personas(cfg.get("scenarios", "scenarios/**/*.yaml"))
    if args.only:
        missing = set(args.only) - set(personas)
        if missing:
            raise SystemExit(f"unknown scenario ids: {sorted(missing)}")
        personas = {k: v for k, v in personas.items() if k in args.only}
    g = cfg.get("guardrails", {})
    unreviewed = [p.id for p in personas.values() if not p.review.approved]
    print(f"rubric {rubric.version}: {len(rubric.rules)} rules · {len(personas)} scenarios")
    print(coverage(personas))
    if unreviewed:
        msg = f"{len(unreviewed)} scenario(s) have no second reviewer yet: {', '.join(unreviewed)}"
        if not g.get("allow_unreviewed", False):
            raise SystemExit(msg + "\nGuardrail: add a reviewer in each file's `review.reviewers`, or set guardrails.allow_unreviewed for a dry run.")
        print("WARNING: " + msg + " (allowed for this dry run)")
    if args.check_only:
        print("check passed")
        return 0

    seeds = args.seeds or cfg.get("seeds", [1])
    if args.estimate:
        estimate(cfg, len(personas), len(seeds), REPO_ROOT / args.out)
        return 0
    roles, temps, demo = cfg["roles"], cfg.get("temperatures", {}), cfg.get("demo", {})
    gen = make_model(roles["child_generator"])
    rev = make_model(roles["child_reviewer"]) if roles.get("child_reviewer") else None
    judge_model = make_model(roles["judge"])
    for role, spec in roles.items():
        if spec and spec.get("backend") == "manual":
            raise SystemExit(f"roles.{role} can't use backend: manual. The relay is only for chatbots under test.")
    agent_models = [make_model(a["model"]) for a in cfg["agents"]]
    if any(isinstance(m, ManualModel) for m in agent_models) and not g.get("enforce", True):
        raise SystemExit("guardrails.enforce must be true for relay runs: an unchecked child line would reach a real app.")
    all_models = [m for m in [gen, rev, judge_model, *agent_models] if m is not None]
    if not args.skip_preflight:
        problems = preflight(all_models)
        if problems:
            raise SystemExit("preflight failed, nothing was run:\n  - " + "\n  - ".join(problems))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}_{cfg.get('run_name', 'run')}_{uuid.uuid4().hex[:4]}"
    out_dir = REPO_ROOT / args.out / run_id
    ctx = RunContext(run_id, out_dir, rubric, personas, enforce=g.get("enforce", True),
                     audit_rate=g.get("audit_rate", 0.10), seed=cfg.get("audit_seed", 0))

    child = ChildSimulator(gen, rev, ctx, temperature=temps.get("child", 0.9), demo_adult_first_draft=demo.get("adult_first_draft", False))
    judge = Judge(judge_model, ctx, temperature=temps.get("judge", 0.0), demo_bad_quote=demo.get("bad_quote", False))
    agents = [AgentUnderTest(a["name"], m, system_prompt=a.get("system_prompt"),
                             temperature=temps.get("agent", 0.7), mock_behavior=a["model"].get("behavior"))
              for a, m in zip(cfg["agents"], agent_models)]

    n = len(personas) * len(agents) * len(seeds)
    print(f"\nrun {run_id}: {len(personas)} scenarios x {len(agents)} chatbots x {len(seeds)} seeds = {n} conversations")
    print(f"max model calls ~ {n * 6 * (2 + (1 if rev else 0)) + n * 14} (turn cap 6, judge cap 14 steps)")
    per_conv: dict[str, dict] = {}
    incomplete: list[str] = []
    talk_start = time.perf_counter()
    plan = [(p, a, s) for p in personas.values() for a in agents for s in seeds]
    for p, a, s in plan:
        if isinstance(a.model, ManualModel):
            a.model.label, a.model.max_turns = f"{p.id} · {a.name}", p.max_turns
        t0, u0 = time.perf_counter(), model_usage(all_models)
        t = converse(p, a, child, ctx, s)
        secs = time.perf_counter() - t0
        per_conv[t.transcript_id] = {"talk_seconds": round(secs, 2),
                                     "talk_tokens": usage_delta(model_usage(all_models), u0)}
        ctx.write("transcripts.jsonl", t)
        if t.complete:
            ctx.transcripts[t.transcript_id] = t   # only complete conversations are scored
        else:
            incomplete.append(t.transcript_id)
        print(f"  talked   {t.transcript_id:<40} {len(t.turns) // 2} turns · {t.ended_because} · {secs:.0f} s")
        if getattr(a.model, "quit_requested", False):
            break   # Ctrl+C in a relay run: skip the remaining conversations, still score
    talk_seconds = time.perf_counter() - talk_start
    score_start = time.perf_counter()
    for tid, t in ctx.transcripts.items():
        t0, u0 = time.perf_counter(), model_usage(all_models)
        rec = judge.score(t, seed=t.seed)
        secs = time.perf_counter() - t0
        per_conv[tid].update({"score_seconds": round(secs, 2), "score_tokens": usage_delta(model_usage(all_models), u0)})
        status = rec.overall + (" (provisional)" if rec.provisional else "") if rec else "JUDGE FAILED"
        print(f"  scored   {tid:<40} {status} · {secs:.0f} s")
    score_seconds = time.perf_counter() - score_start

    summary = summarize(ctx.transcripts, ctx.scored, ctx.queued)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    manifest = {
        "run_id": run_id, "created_at": stamp, "harness_version": __version__, "git": git_info(),
        "python": platform.python_version(), "config_file": str(args.config), "config": cfg,
        "rubric": {"version": rubric.version, "sha256": hashlib.sha256(rubric_path.read_bytes()).hexdigest()[:16]},
        "scenarios": hashes if not args.only else {k: hashes[k] for k in personas},
        "seeds": seeds,
        "models": {"child_generator": gen.model_id, "child_reviewer": rev.model_id if rev else None,
                   "judge": judge_model.model_id, "agents": {a.name: a.model_id for a in agents}},
        "guardrails": {"enforce": ctx.enforce, "audit_rate": ctx.audit_rate},
        "counts": {"transcripts": len(ctx.transcripts), "incomplete": incomplete, "scored": len(ctx.scored), "review_items": len(ctx.queued),
                   "tool_calls": ctx.tool_calls},
        "timing": {"conversations": len(ctx.transcripts), "talk_seconds": round(talk_seconds, 2),
                   "score_seconds": round(score_seconds, 2), "per_conversation": per_conv},
        "tokens": {"total": model_usage(all_models),
                   "by_role": {"child_generator": model_usage([gen]), "child_reviewer": model_usage([rev] if rev else []),
                               "judge": model_usage([judge_model]),
                               "agents": {a.name: model_usage([a.model]) for a in agents}},
                   "note": "from each response's usage; 0 for mock models"},
        "provider_data_retention": cfg.get("provider_data_retention", "[record each provider's retention/training settings before publishing]"),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    (REPO_ROOT / args.out / "LATEST").write_text(run_id + "\n")
    print()
    print_table(summary)
    shown = out_dir.relative_to(REPO_ROOT) if out_dir.is_relative_to(REPO_ROOT) else out_dir
    print(f"\nresults: {shown}  ·  review queue: {len(ctx.queued)} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
