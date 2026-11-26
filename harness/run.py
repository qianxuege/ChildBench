"""Orchestrator: scenarios x chatbots x seeds -> results/<run_id>/ (RFC 0002, 0009).

Plain code, no LLM decides the order of steps. Usage:
    python -m harness.run --config configs/mock.yaml
    python -m harness.run --config configs/gemma-local.yaml --only dist-003 --seeds 1
    python -m harness.run --config configs/mock.yaml --check-only
    python -m harness.run --resume <run_id>     # finish an unscored run: its unfinished chats, then scoring (F5)
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
from .tools.ceiling import scenario_content_problems
from .tools.referral import detect_referral
from .tools.review import flag_for_human_review


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
        problems = scenario_content_problems(p)
        if problems:
            raise SystemExit(f"{f}: " + "; ".join(problems))
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
    ages = sorted({p.child_age for p in personas.values()})
    lines = [f"{'':<6}" + "".join(f"{a:>8}" for a in ages)]
    for code in ["S1", "S2", "S3", "S4", "S5", "C", "E"]:
        row = f"{code:<6}"
        for a in ages:
            n = sum(1 for p in personas.values() if p.code == code and p.child_age == a)
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


GUARDRAILS_OFF_SUFFIX = "-guardrails-off"


def run_name(cfg: dict) -> str:
    """The config's run_name, with "-guardrails-off" added when guardrails.enforce is false (A6).

    The suffix keeps a guardrails-off run's folder from passing as a normal run, so the export
    and the comparison can tell them apart by name.
    """
    name = cfg.get("run_name", "run")
    enforce = (cfg.get("guardrails") or {}).get("enforce", True)
    if not enforce and not name.endswith(GUARDRAILS_OFF_SUFFIX):
        name += GUARDRAILS_OFF_SUFFIX
    return name


def estimate(cfg: dict, n_personas: int, n_seeds: int, results_dir: Path) -> None:
    n_agents = len(cfg["agents"])
    n = n_personas * n_agents * n_seeds
    has_rev = bool(cfg["roles"].get("child_reviewer"))
    print(f"\nestimate: {n_personas} scenarios x {n_agents} chatbots x {n_seeds} seeds = {n} conversations")
    print(f"  model calls, at most ~{n * 6 * (2 + (1 if has_rev else 0)) + n * 14} (turn cap 6, judge cap 14 steps)")
    name = run_name(cfg)
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
    last_scripted = len(persona.turns)
    ended = "turn limit"
    complete = True
    relay = isinstance(agent.model, ManualModel)
    for turn in range(0 if persona.caregiver_message else 1, persona.max_turns + 1):
        speaking = "child"
        try:
            if turn == 0:   # sent word for word, before the child says anything; the reply is kept but not scored
                history.append(Turn(turn=0, speaker="caregiver", text=persona.caregiver_message))
                speaking = "chatbot"
                history.append(Turn(turn=0, speaker="agent", text=agent.reply(history, seed)))
                continue
            text, used_fallback, attempts, phase = child.utterance(persona, tid, turn, history, seed, pushed_back)
            if phase == "pushback":
                pushed_back = True
            history.append(Turn(turn=turn, speaker="child", text=text, used_fallback=used_fallback, attempts=attempts))
            speaking = "chatbot"
            reply = agent.reply(history, seed)
        except KeyboardInterrupt:
            if not relay:
                raise   # automated runs: Ctrl+C quits at once, as before
            # Relay runs: keep what's finished. Save this conversation as incomplete, skip the rest, still score.
            print("\n  Ctrl+C: this conversation is saved as incomplete, the rest of the run is skipped, and finished "
                  "conversations are scored now. Press Ctrl+C again to quit at once.")
            if history and history[-1].speaker != "agent" and history[-1].turn == turn:
                history.pop()   # the app never answered this line
            ended, complete = "stopped with Ctrl+C", False
            agent.model.quit_requested = True
            break
        except ManualStop:
            history.pop()   # the person never sent this message to the app
            ended, complete = "stopped by the person relaying", False
            break
        except ModelError as e:
            # Cut off, so not scored (F-009). The message can hold the server address: show it here, save only its type.
            print(f"  {speaking} model error in {tid}: {e}", file=sys.stderr)
            ended, complete = f"{speaking} model error: {type(e.__cause__ or e).__name__}", False
            break
        history.append(Turn(turn=turn, speaker="agent", text=reply))
        script_done = turn >= last_scripted
        if not persona.is_referral:
            if script_done:
                ended = "script complete"
                break
            continue
        # The stop condition is only checked once every scripted turn has been delivered,
        # so each scenario always reaches its key moment.
        if script_done and detect_referral(reply):
            if persona.pushback_line and not pushed_back:
                continue  # the child resists once on the next turn
            ended = "referred again after push-back" if pushed_back else "referred"
            break
    manual = agent.model if isinstance(agent.model, ManualModel) else None
    return Transcript(transcript_id=tid, run_id=ctx.run_id, scenario_id=persona.id, situation=persona.situation,
                      child_age=persona.child_age, agent_name=agent.name, agent_model=agent.model_id, seed=seed,
                      turns=history, ended_because=ended, complete=complete,
                      channel="app" if manual else "api", app_mode=manual.mode if manual else None,
                      app_model_shown=manual.model_shown if manual else None)


PLAN_FILE = "run_plan.json"      # written when a run starts; lets `--resume` and `make app` reopen the folder (F5)
TALK_LOG = "talk_log.jsonl"      # one line per chat attempt, so timing survives a resume


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def load_scenarios(cfg: dict, only: list[str] | None, *, quiet: bool = False) -> tuple[Rubric, Path, dict[str, Persona], dict[str, str]]:
    """The rubric and the scenarios a config names, narrowed to `only`. Exits on an unknown id or, unless the
    config allows it, on a scenario without a second reviewer."""
    rubric_path = REPO_ROOT / cfg.get("rubric", "rubrics/v0.yaml")
    rubric = load_rubric(rubric_path)
    personas, hashes = load_personas(cfg.get("scenarios", "scenarios/**/*.yaml"))
    if only:
        missing = set(only) - set(personas)
        if missing:
            raise SystemExit(f"unknown scenario ids: {sorted(missing)}")
        personas = {k: v for k, v in personas.items() if k in only}
    g = cfg.get("guardrails", {})
    unreviewed = [p.id for p in personas.values() if not p.review.approved]
    if not quiet:
        print(f"rubric {rubric.version}: {len(rubric.rules)} rules · {len(personas)} scenarios")
        print(coverage(personas))
    if unreviewed:
        msg = f"{len(unreviewed)} scenario(s) have no second reviewer yet: {', '.join(unreviewed)}"
        if not g.get("allow_unreviewed", False):
            raise SystemExit(msg + "\nGuardrail: add a reviewer in each file's `review.reviewers`, or set guardrails.allow_unreviewed for a dry run.")
        if not quiet:
            print("WARNING: " + msg + " (allowed for this dry run)")
    return rubric, rubric_path, personas, hashes


class Run:
    """One run folder: its models, its plan of chats, and its results.

    Chats are talked one at a time with `talk` (the terminal loops over `todo()`; `make app` asks for one chat per
    click), then scored once with `score`. Opened again with `resume_id`, it skips chats that already have a complete
    transcript and starts unfinished ones again; the unfinished attempt stays in transcripts.jsonl, marked incomplete.
    """

    def __init__(self, cfg: dict, config_file: str, rubric: Rubric, rubric_path: Path, personas: dict[str, Persona],
                 hashes: dict[str, str], seeds: list[int], *, out_root: Path, only: list[str] | None = None,
                 agent_names: list[str] | None = None, resume_id: str | None = None, skip_preflight: bool = False):
        self.cfg, self.config_file, self.rubric, self.rubric_path = cfg, config_file, rubric, rubric_path
        self.personas, self.hashes, self.seeds, self.only, self.out_root = personas, hashes, seeds, only, out_root
        g = cfg.get("guardrails", {})
        roles, temps, demo = cfg["roles"], cfg.get("temperatures", {}), cfg.get("demo", {})
        for role, spec in roles.items():
            if spec and spec.get("backend") == "manual":
                raise SystemExit(f"roles.{role} can't use backend: manual. The relay is only for chatbots under test.")
        agent_cfgs = cfg["agents"]
        if agent_names:
            unknown = set(agent_names) - {a["name"] for a in agent_cfgs}
            if unknown:
                raise SystemExit(f"unknown chatbot names: {sorted(unknown)}")
            agent_cfgs = [a for a in agent_cfgs if a["name"] in agent_names]
        self.agent_names = [a["name"] for a in agent_cfgs] if agent_names else None
        self.gen = make_model(roles["child_generator"])
        self.rev = make_model(roles["child_reviewer"]) if roles.get("child_reviewer") else None
        self.judge_model = make_model(roles["judge"])
        agent_models = [make_model(a["model"]) for a in agent_cfgs]
        if any(isinstance(m, ManualModel) for m in agent_models) and not g.get("enforce", True):
            raise SystemExit("guardrails.enforce must be true for relay runs: an unchecked child line would reach a real app.")
        self.all_models = [m for m in [self.gen, self.rev, self.judge_model, *agent_models] if m is not None]
        if not skip_preflight:
            problems = preflight(self.all_models)
            if problems:
                raise SystemExit("preflight failed, nothing was run:\n  - " + "\n  - ".join(problems))
        self.resumed = bool(resume_id)
        if resume_id:
            self.run_id, self.stamp = resume_id, resume_id.split("_", 1)[0]
            self.out_dir = out_root / resume_id
            if (self.out_dir / "manifest.json").exists():
                raise SystemExit(f"{resume_id} is already scored, so it can't be resumed")
        else:
            self.stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            self.run_id = f"{self.stamp}_{run_name(cfg)}_{uuid.uuid4().hex[:4]}"
            self.out_dir = out_root / self.run_id
        self.ctx = RunContext(self.run_id, self.out_dir, rubric, personas, enforce=g.get("enforce", True),
                              audit_rate=g.get("audit_rate", 0.10), seed=cfg.get("audit_seed", 0))
        if not resume_id:
            (self.out_dir / PLAN_FILE).write_text(json.dumps(
                {"run_id": self.run_id, "config_file": str(config_file), "config": cfg, "only": only, "seeds": seeds,
                 "agents": self.agent_names}, indent=2, default=str))
        self.child = ChildSimulator(self.gen, self.rev, self.ctx, temperature=temps.get("child", 0.9),
                                    demo_adult_first_draft=demo.get("adult_first_draft", False))
        self.judge = Judge(self.judge_model, self.ctx, temperature=temps.get("judge", 0.0), demo_bad_quote=demo.get("bad_quote", False))
        self.agents = [AgentUnderTest(a["name"], m, system_prompt=a.get("system_prompt"),
                                      temperature=temps.get("agent", 0.7), mock_behavior=a["model"].get("behavior"))
                       for a, m in zip(agent_cfgs, agent_models)]
        self.plan = [(p, a, s) for p in personas.values() for a in self.agents for s in seeds]

    @classmethod
    def reopen(cls, run_dir: Path, *, skip_preflight: bool = False, quiet: bool = True) -> "Run":
        """The run in `run_dir`, from its run_plan.json, ready to talk to its unfinished chats."""
        plan_file = run_dir / PLAN_FILE
        if not plan_file.exists():
            raise SystemExit(f"{run_dir.name} has no {PLAN_FILE}, so it can't be resumed (it was started before F5)")
        plan = json.loads(plan_file.read_text())
        cfg = plan["config"]
        rubric, rubric_path, personas, hashes = load_scenarios(cfg, plan.get("only"), quiet=quiet)
        return cls(cfg, plan["config_file"], rubric, rubric_path, personas, hashes, plan["seeds"], out_root=run_dir.parent,
                   only=plan.get("only"), agent_names=plan.get("agents"), resume_id=run_dir.name, skip_preflight=skip_preflight)

    # ------------------------------------------------------------------ state
    @staticmethod
    def tid(p: Persona, a: AgentUnderTest, s: int) -> str:
        return f"{p.id}__{a.name}__s{s}"

    def finished(self) -> set[str]:
        """Transcript ids that already have a complete conversation in this folder."""
        return {d["transcript_id"] for d in read_jsonl(self.out_dir / "transcripts.jsonl") if d.get("complete", True)}

    def todo(self) -> list[tuple[Persona, AgentUnderTest, int]]:
        done = self.finished()
        return [(p, a, s) for p, a, s in self.plan if self.tid(p, a, s) not in done]

    # ------------------------------------------------------------------ talking and scoring
    def talk(self, p: Persona, a: AgentUnderTest, s: int) -> Transcript:
        """One conversation, saved to transcripts.jsonl whether or not it finished."""
        if isinstance(a.model, ManualModel):
            a.model.label, a.model.max_turns = f"{p.id} · {a.name}", p.max_turns
        t0, u0 = time.perf_counter(), model_usage(self.all_models)
        t = converse(p, a, self.child, self.ctx, s)
        secs = time.perf_counter() - t0
        self.ctx.write("transcripts.jsonl", t)
        self.ctx.write(TALK_LOG, {"transcript_id": t.transcript_id, "complete": t.complete, "talk_seconds": round(secs, 2),
                                  "talk_tokens": usage_delta(model_usage(self.all_models), u0)})
        print(f"  talked   {t.transcript_id:<40} {len(t.agent_turns())} turns · {t.ended_because} · {secs:.0f} s")
        return t

    def score(self) -> dict:
        """Score every complete conversation in the folder, once, and write summary.json, manifest.json and LATEST."""
        if (self.out_dir / "manifest.json").exists():
            raise SystemExit(f"{self.run_id} is already scored")
        rows = read_jsonl(self.out_dir / "transcripts.jsonl")
        attempts: dict[str, list[Transcript]] = {}
        for d in rows:
            t = Transcript(**d)
            attempts.setdefault(t.transcript_id, []).append(t)
        ctx, rubric = self.ctx, self.rubric
        for tid, ts in attempts.items():
            done = [t for t in ts if t.complete]
            if done:
                ctx.transcripts[tid] = done[-1]       # only complete conversations are scored
        incomplete = [tid for tid, ts in attempts.items() if tid not in ctx.transcripts]
        restarted = [tid for tid, ts in attempts.items() if tid in ctx.transcripts and len(ts) > 1]
        per_conv: dict[str, dict] = {}
        for d in read_jsonl(self.out_dir / TALK_LOG):
            if d["complete"] or d["transcript_id"] not in per_conv:
                per_conv[d["transcript_id"]] = {"talk_seconds": d["talk_seconds"], "talk_tokens": d["talk_tokens"]}
        talk_seconds = sum(v["talk_seconds"] for v in per_conv.values())
        score_start = time.perf_counter()
        for tid, t in ctx.transcripts.items():
            per_conv.setdefault(tid, {})
            if not rubric.scores_situation(SITUATION_CODE[t.situation]):
                print(f"  skipped  {tid:<40} rubric {rubric.version} has no rules for '{t.situation}', so it isn't scored")
                continue
            t0, u0 = time.perf_counter(), model_usage(self.all_models)
            try:
                rec = self.judge.score(t, seed=t.seed)
            except ModelError as e:
                # One failed judge call must not stop the run. The message can hold the server address: save only its type.
                print(f"  judge model error in {tid}: {e}", file=sys.stderr)
                rec = ctx.scored.get(tid)   # the judge may have submitted before its last call failed
                if rec is None:
                    flag_for_human_review(ctx, tid, "JUDGE_FAILED", f"judge model error: {type(e.__cause__ or e).__name__}")
            secs = time.perf_counter() - t0
            per_conv[tid].update({"score_seconds": round(secs, 2), "score_tokens": usage_delta(model_usage(self.all_models), u0)})
            status = rec.overall + (" (provisional)" if rec.provisional else "") if rec else "JUDGE FAILED"
            print(f"  scored   {tid:<40} {status} · {secs:.0f} s")
        score_seconds = time.perf_counter() - score_start

        summary = summarize(ctx.transcripts, ctx.scored, ctx.queued)
        (self.out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        gen, rev, judge_model = self.gen, self.rev, self.judge_model
        manifest = {
            "run_id": self.run_id, "created_at": self.stamp, "harness_version": __version__, "git": git_info(),
            "python": platform.python_version(), "config_file": str(self.config_file), "config": self.cfg,
            "rubric": {"version": rubric.version, "sha256": hashlib.sha256(self.rubric_path.read_bytes()).hexdigest()[:16]},
            "scenarios": self.hashes if not self.only else {k: self.hashes[k] for k in self.personas},
            "seeds": self.seeds,
            "models": {"child_generator": gen.model_id, "child_reviewer": rev.model_id if rev else None,
                       "judge": judge_model.model_id, "agents": {a.name: a.model_id for a in self.agents}},
            "guardrails": {"enforce": ctx.enforce, "audit_rate": ctx.audit_rate},
            "counts": {"transcripts": len(ctx.transcripts), "incomplete": incomplete, "restarted": restarted,
                       "scored": len(ctx.scored), "review_items": len(ctx.queued), "tool_calls": ctx.tool_calls},
            "timing": {"conversations": len(ctx.transcripts), "talk_seconds": round(talk_seconds, 2),
                       "score_seconds": round(score_seconds, 2), "per_conversation": per_conv},
            "tokens": {"total": model_usage(self.all_models),
                       "by_role": {"child_generator": model_usage([gen]), "child_reviewer": model_usage([rev] if rev else []),
                                   "judge": model_usage([judge_model]),
                                   "agents": {a.name: model_usage([a.model]) for a in self.agents}},
                       "note": "from each response's usage; 0 for mock models" + ("; this session only, the run was resumed" if self.resumed else "")},
            "provider_data_retention": self.cfg.get("provider_data_retention", "[record each provider's retention/training settings before publishing]"),
        }
        if self.agent_names:
            manifest["agents_only"] = self.agent_names
        (self.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
        (self.out_root / "LATEST").write_text(self.run_id + "\n")
        return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the ChildBench pipeline.")
    ap.add_argument("--config")
    ap.add_argument("--only", nargs="*", help="scenario ids to run")
    ap.add_argument("--seeds", nargs="*", type=int)
    ap.add_argument("--check-only", action="store_true", help="validate scenarios and rubric, then exit")
    ap.add_argument("--out", default="results")
    ap.add_argument("--estimate", action="store_true", help="print conversation count and expected time, then exit")
    ap.add_argument("--skip-preflight", action="store_true", help="don't check that model servers are up before running")
    ap.add_argument("--resume", metavar="RUN_ID", help="finish an unscored run: skip its complete chats, start unfinished ones again, then score")
    args = ap.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")
    out_root = REPO_ROOT / args.out

    if args.resume:
        if args.config or args.only or args.seeds:
            raise SystemExit("--resume takes its config, scenarios and seeds from the run folder; drop --config, --only and --seeds")
        run = Run.reopen(out_root / args.resume, skip_preflight=args.skip_preflight, quiet=False)
    else:
        if not args.config:
            raise SystemExit("--config is required (or --resume RUN_ID)")
        cfg_path = Path(args.config)
        cfg = yaml.safe_load((cfg_path if cfg_path.is_absolute() else REPO_ROOT / cfg_path).read_text())
        rubric, rubric_path, personas, hashes = load_scenarios(cfg, args.only)
        if args.check_only:
            print("check passed")
            return 0
        seeds = args.seeds or cfg.get("seeds", [1])
        if args.estimate:
            estimate(cfg, len(personas), len(seeds), out_root)
            return 0
        run = Run(cfg, args.config, rubric, rubric_path, personas, hashes, seeds, out_root=out_root, only=args.only,
                  skip_preflight=args.skip_preflight)

    todo = run.todo()
    n, rev = len(run.plan), run.rev
    print(f"\nrun {run.run_id}: {len(run.personas)} scenarios x {len(run.agents)} chatbots x {len(run.seeds)} seeds = {n} conversations"
          + (f", {n - len(todo)} already done" if len(todo) < n else ""))
    print(f"max model calls ~ {len(todo) * 6 * (2 + (1 if rev else 0)) + len(todo) * 14} (turn cap 6, judge cap 14 steps)")
    for p, a, s in todo:
        run.talk(p, a, s)
        if getattr(a.model, "quit_requested", False):
            break   # Ctrl+C in a relay run: skip the remaining conversations, still score
    summary = run.score()
    print()
    print_table(summary)
    shown = run.out_dir.relative_to(REPO_ROOT) if run.out_dir.is_relative_to(REPO_ROOT) else run.out_dir
    print(f"\nresults: {shown}  ·  review queue: {len(run.ctx.queued)} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
