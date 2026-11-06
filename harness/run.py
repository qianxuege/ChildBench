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


def converse(persona: Persona, agent: AgentUnderTest, child: ChildSimulator, ctx: RunContext, seed: int) -> Transcript:
    tid = f"{persona.id}__{agent.name}__s{seed}"
    history: list[Turn] = []
    pushed_back = False
    last_beat = persona.beats[-1].turn
    ended = "turn limit"
    for turn in range(1, persona.max_turns + 1):
        text, used_fallback, attempts, phase = child.utterance(persona, tid, turn, history, seed, pushed_back)
        if phase == "pushback":
            pushed_back = True
        history.append(Turn(turn=turn, speaker="child", text=text, used_fallback=used_fallback, attempts=attempts))
        try:
            reply = agent.reply(history, seed)
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
    return Transcript(transcript_id=tid, run_id=ctx.run_id, scenario_id=persona.id, situation=persona.situation,
                      child_age=persona.child_age, agent_name=agent.name, agent_model=agent.model_id, seed=seed,
                      turns=history, ended_because=ended)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the ChildBench pipeline.")
    ap.add_argument("--config", required=True)
    ap.add_argument("--only", nargs="*", help="scenario ids to run")
    ap.add_argument("--seeds", nargs="*", type=int)
    ap.add_argument("--check-only", action="store_true", help="validate scenarios and rubric, then exit")
    ap.add_argument("--out", default="results")
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
    roles, temps, demo = cfg["roles"], cfg.get("temperatures", {}), cfg.get("demo", {})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}_{cfg.get('run_name', 'run')}_{uuid.uuid4().hex[:4]}"
    out_dir = REPO_ROOT / args.out / run_id
    ctx = RunContext(run_id, out_dir, rubric, personas, enforce=g.get("enforce", True),
                     audit_rate=g.get("audit_rate", 0.10), seed=cfg.get("audit_seed", 0))

    gen = make_model(roles["child_generator"])
    rev = make_model(roles["child_reviewer"]) if roles.get("child_reviewer") else None
    judge_model = make_model(roles["judge"])
    child = ChildSimulator(gen, rev, ctx, temperature=temps.get("child", 0.9), demo_adult_first_draft=demo.get("adult_first_draft", False))
    judge = Judge(judge_model, ctx, temperature=temps.get("judge", 0.0), demo_bad_quote=demo.get("bad_quote", False))
    agents = [AgentUnderTest(a["name"], make_model(a["model"]), system_prompt=a.get("system_prompt"),
                             temperature=temps.get("agent", 0.7), mock_behavior=a["model"].get("behavior"))
              for a in cfg["agents"]]

    n = len(personas) * len(agents) * len(seeds)
    print(f"\nrun {run_id}: {len(personas)} scenarios x {len(agents)} chatbots x {len(seeds)} seeds = {n} conversations")
    print(f"max model calls ~ {n * 6 * (2 + (1 if rev else 0)) + n * 14} (turn cap 6, judge cap 14 steps)")
    for p in personas.values():
        for a in agents:
            for s in seeds:
                t = converse(p, a, child, ctx, s)
                ctx.transcripts[t.transcript_id] = t
                ctx.write("transcripts.jsonl", t)
                print(f"  talked   {t.transcript_id:<40} {len(t.turns) // 2} turns · {t.ended_because}")
    for tid, t in ctx.transcripts.items():
        rec = judge.score(t, seed=t.seed)
        status = rec.overall + (" (provisional)" if rec.provisional else "") if rec else "JUDGE FAILED"
        print(f"  scored   {tid:<40} {status}")

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
        "counts": {"transcripts": len(ctx.transcripts), "scored": len(ctx.scored), "review_items": len(ctx.queued),
                   "tool_calls": ctx.tool_calls},
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
