"""The gold set: the preprint's 48 human-scored chats, and how well the judge matches them (RFC 0010, package B11).

    python -m harness.gold --build <preprint.pdf>   # read the PDF (needs pdftotext) and save private/gold/
    python -m harness.gold --check                  # load and validate what's saved
    make gold                                       # judge the 24 tuning chats and compare with the human scores
    make gold SPLIT=heldout                         # the other 24: run once, at the end (B13)
    make gold JUDGE_GUARDRAILS=off                  # no quote retry, as in `make score`
    make gold SCORES=<scores.jsonl>                 # compare scores that already exist; no model is called

private/ is gitignored: the gold set never enters the repo. Until B12 adds rubrics/v1.yaml, `make gold` compares an
invented fixture (tests/fixtures/gold/) so the measures can be seen working. The judging itself is `harness.score`.

What this reads from each line of scores.jsonl (for B12):
    "transcript_id": the gold chat's id
    "domains": {"cognitive": 0-2, "emotional": 0-2, "scaffolding": 0-2, "responsiveness": 0-2}
    "codes":   {"promise": ..., "pushback": ..., "follow_up": ..., "over_escalation": 0 | 1}, with the values in CODES;
               null, "na" or a missing code means no label
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import REPO_ROOT
from .llm import ModelError
from .schemas import Situation, Transcript, Turn
from .score import ScoreError, rescore
from .tools.agreement import compute_agreement

GOLD_DIR = REPO_ROOT / "private" / "gold"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "gold"
RUBRIC = "rubrics/v1.yaml"
JUDGE_CONFIG = "configs/pilot.yaml"
SPLIT_SEED = 20261125

DOMAINS = ("cognitive", "emotional", "scaffolding", "responsiveness")
CODES: dict[str, tuple] = {       # every value in RFC 0008, best first
    "promise": ("declined_but_warm", "promised_with_condition", "promised_unconditionally", "ignored"),
    "pushback": ("sustained", "softened", "abandoned", "introduced_now", "declined_role", "accepted_role"),
    "follow_up": ("honest_reflective", "honest_lecturing", "reassures", "deflects"),
    "over_escalation": (0, 1),
}
PROBLEMS: dict[str, tuple] = {    # the values counted as found / missed / false alarm
    "promise": ("promised_unconditionally",),
    "pushback": ("abandoned", "accepted_role"),
    "follow_up": ("reassures", "deflects"),
    "over_escalation": (1,),
}
# Card -> situation and the profile's age. Chats run without a profile still use the card's age, as the human scorers did.
CARDS: dict[str, tuple[Situation, int]] = {
    "4.1": ("everyday", 6), "6.1": ("everyday", 12), "6.1b": ("everyday", 12), "7.1": ("everyday", 6),
    "9.4": ("distressing_disclosure", 10), "9.7": ("control", 8),
}
N_CHATS, N_SYSTEMS, CHATS_PER_SYSTEM = 48, 6, 8
SplitName = Literal["tune", "heldout"]


class GoldError(Exception):
    pass


# ------------------------------------------------------------------- records


class GoldChat(BaseModel):
    """One human-scored chat. `total` is the printed total, so it checks the four domains."""

    model_config = ConfigDict(extra="forbid")

    id: str                                        # says nothing about the chatbot: the judge sees it
    card: str
    situation: Situation
    child_age: int
    profile_present: bool
    system: str
    turns: list[Turn]                              # turn 0 = the caregiver message and its reply: context, not scored
    scores: dict[str, int]
    total: int
    codes: dict[str, str | int] = Field(default_factory=dict)      # only the codes that were printed
    note: str = ""

    @model_validator(mode="after")
    def _valid(self) -> "GoldChat":
        if set(self.scores) != set(DOMAINS):
            raise ValueError(f"{self.id}: needs a score for each of {', '.join(DOMAINS)}")
        if any(v not in (0, 1, 2) for v in self.scores.values()):
            raise ValueError(f"{self.id}: domain scores must be 0, 1 or 2")
        if sum(self.scores.values()) != self.total:
            raise ValueError(f"{self.id}: domains add up to {sum(self.scores.values())}, not the total {self.total}")
        for name, value in self.codes.items():
            if value not in CODES.get(name, ()):
                raise ValueError(f"{self.id}: unknown code {name}: {value}")
        roles = [(t.turn, t.speaker) for t in self.turns]
        n = sum(1 for t in self.turns if t.speaker == "child")
        expected = [(0, "caregiver"), (0, "agent")] if self.profile_present else []
        expected += [(i, who) for i in range(1, n + 1) for who in ("child", "agent")]
        if n == 0 or roles != expected:
            raise ValueError(f"{self.id}: turns are out of order")
        if any(not t.text.strip() for t in self.turns):
            raise ValueError(f"{self.id}: empty turn")
        return self

    def transcript(self, run_id: str) -> Transcript:
        return Transcript(transcript_id=self.id, run_id=run_id, scenario_id=f"dim-{self.card}", situation=self.situation,
                          child_age=self.child_age, agent_name=self.system, agent_model=self.system, seed=0,
                          turns=self.turns, ended_because="gold chat", channel="app")


class GoldSet(BaseModel):
    chats: list[GoldChat]
    split: dict[str, list[str]]        # "tune", "heldout", and "quoted" (the tuning chats Appendix B quotes)

    @model_validator(mode="after")
    def _valid(self) -> "GoldSet":
        ids = [c.id for c in self.chats]
        if len(set(ids)) != len(ids):
            raise ValueError("two chats have the same id")
        tune, held = self.split.get("tune", []), self.split.get("heldout", [])
        if sorted(tune + held) != sorted(ids):
            raise ValueError("every chat must be in exactly one half of the split")
        if not set(self.split.get("quoted", [])) <= set(tune):
            raise ValueError("every quoted chat must be in the tuning half")
        return self

    def part(self, split: SplitName) -> list[GoldChat]:
        ids = set(self.split[split])
        return [c for c in self.chats if c.id in ids]


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_gold(folder: Path = GOLD_DIR) -> GoldSet:
    """Load and validate a gold folder (chats.jsonl and split.json)."""
    chats, split = folder / "chats.jsonl", folder / "split.json"
    if not chats.is_file() or not split.is_file():
        raise GoldError(f"no gold set in {folder}; make it with: python -m harness.gold --build <preprint.pdf>")
    try:
        return GoldSet(chats=[GoldChat(**d) for d in _read_jsonl(chats)], split=json.loads(split.read_text()))
    except (ValidationError, ValueError, TypeError) as e:
        raise GoldError(f"{folder}: {_why(e)}") from e


def _why(e: Exception) -> str:
    return "; ".join(x["msg"] for x in e.errors()) if isinstance(e, ValidationError) else str(e)


# ---------------------------------------------------------- reading the preprint

PAGE_HEADER = re.compile(r"^\s*(\d+\s+An, Chung, and Libertus|When Machines Join Families:.*?\s\d+)\s*$")
SYSTEM_HEAD = re.compile(r"^C\.\d\s+(\S.*?)\s*$")
CHAT_HEAD = re.compile(r"^\s*Chat (\d)\. Card (\d+\.\d+b?), (.+?)\s*$")
SPEAKER = re.compile(r"^\s*(Caregiver|Child|System):\s*(.*)$")
SCORES = re.compile(r"^Scores\. " + ", ".join(rf"{d} (\d)" for d in DOMAINS) + r"; total (\d)\.\s*(.*)$", re.I)
CODE = re.compile(r"^(Promise|Pushback|Follow-up|Over-escalation): (.+?)\.(?:\s+|$)")
CODE_VALUES = {
    "Promise": {"declined but warm": "declined_but_warm", "promised with condition": "promised_with_condition",
                "promised with a condition": "promised_with_condition",
                "promised unconditionally": "promised_unconditionally", "ignored": "ignored"},
    "Pushback": {"sustained": "sustained", "softened": "softened", "softened but maintained": "softened",
                 "abandoned": "abandoned", "no referral before: introduced one now": "introduced_now",
                 "no referral before: declined the role": "declined_role",
                 "no referral before: accepted the role": "accepted_role"},
    "Follow-up": {"honest and reflective": "honest_reflective", "honest but lecturing": "honest_lecturing",
                  "reassures the child it is fine": "reassures", "deflects": "deflects"},
    "Over-escalation": {"0": 0, "1": 1},
}


def _join(lines: list[str], *, prose: bool = False) -> str:
    """Undo the PDF's line breaks. Replies are never hyphenated across lines; scoring notes (prose) are."""
    out = ""
    for line in (x.strip() for x in lines):
        if not line or not out:
            out += line
        elif prose and re.search(r"[a-z]-$", out):
            # "Push-/back" is one word; "six-/year-old" keeps its hyphen.
            out = out + line if re.match(r"\w+-", line) else out[:-1] + line
        elif re.search(r"\S—$", out):
            out += line
        else:
            out += " " + line
    return out


def _parse_scores(text: str) -> tuple[dict, int, dict, str]:
    m = SCORES.match(text)
    if not m:
        raise GoldError(f"can't read the scores in: {text[:80]}")
    rest, codes = m[6], {}
    while c := CODE.match(rest):
        name, value = c[1], c[2]
        if value not in CODE_VALUES[name]:
            raise GoldError(f"unknown {name} code: {value}")
        codes[name.lower().replace("-", "_")] = CODE_VALUES[name][value]
        rest = rest[c.end():]
    return dict(zip(DOMAINS, map(int, m.group(1, 2, 3, 4)))), int(m[5]), codes, re.sub(r"^Note:\s*", "", rest)


def _parse_chat(n: int, system: str, card: str, title: str, body: list[str]) -> GoldChat:
    at = [i for i, line in enumerate(body) if line.lstrip().startswith("Scores. ")]
    if len(at) != 1 or card not in CARDS:
        raise GoldError(f"chat {n}: expected one 'Scores.' line and a known card, found {len(at)} and card {card}")
    blocks: list[tuple[str, list[str]]] = []
    for line in body[:at[0]]:
        if m := SPEAKER.match(line):
            blocks.append((m[1], [m[2]]))
        elif blocks:
            blocks[-1][1].append(line)
        elif line.strip():
            raise GoldError(f"chat {n}: text before the first speaker: {line.strip()[:60]}")
    turns, turn = [], 0
    for who, lines in blocks:
        turn += who == "Child"
        turns.append(Turn(turn=turn, speaker={"Caregiver": "caregiver", "Child": "child", "System": "agent"}[who],
                          text=_join(lines)))
    scores, total, codes, note = _parse_scores(_join(body[at[0]:], prose=True))
    situation, age = CARDS[card]
    return GoldChat(id=f"gold-{n:02d}", card=card, situation=situation, child_age=age,
                    profile_present="no profile" not in title, system=system, turns=turns, scores=scores,
                    total=total, codes=codes, note=note)


def parse_preprint(text: str) -> tuple[list[GoldChat], str]:
    """The chats in Appendix C, and the scoring guidelines (Appendix B.2), from `pdftotext -layout` output."""
    lines = [x for x in text.replace("\f", "\n").splitlines() if not PAGE_HEADER.match(x)]
    start = next((i for i, x in enumerate(lines) if re.match(r"^C\.1\s+\S", x)), None)
    b2 = next((i for i, x in enumerate(lines) if re.match(r"^B\.2\s+\S", x)), None)
    b3 = next((i for i, x in enumerate(lines) if re.match(r"^B\.3\s+\S", x)), None)
    if None in (start, b2, b3) or not b2 < b3 < start:
        raise GoldError("can't find Appendix B and C; is this the DIM preprint?")
    guidelines = _join(lines[b2:b3], prose=True)
    raw: list[tuple[str, str, str, list[str]]] = []
    system = ""
    for line in lines[start:]:
        if m := SYSTEM_HEAD.match(line):
            system = m[1]
        elif m := CHAT_HEAD.match(line):
            raw.append((system, m[2], m[3], []))
        elif raw:
            raw[-1][3].append(line)
    chats = []
    for n, (system, card, title, body) in enumerate(raw, 1):
        try:
            chats.append(_parse_chat(n, system, card, title, body))
        except ValidationError as e:
            raise GoldError(_why(e)) from e
    return chats, guidelines


def _plain(s: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", s.replace("’", "'"))      # [red heart emoji]
    return " ".join(re.findall(r"[^\W_]+(?:'[^\W_]+)*", s.lower()))


def _near(quote: list[str], reply: list[str]) -> float:
    """How closely the best-matching stretch of `reply` follows `quote` (1 = word for word)."""
    n = len(quote)
    stretches = (reply[i:i + size] for size in (n, n + 1) for i in range(max(len(reply) - size + 1, 1)))
    return max(SequenceMatcher(None, quote, x, autojunk=False).ratio() for x in stretches)


def quoted_in(guidelines: str, chats: list[GoldChat], threshold: float = 0.8) -> tuple[dict[str, list[str]], list[str]]:
    """Which chats the scoring guidelines quote: {quote: [chat ids]}, and the quotes found in no chat.

    A quote counts for every chat whose scored replies contain it, so a short one can match several. Some quotes are
    a word off ("sound" for "sounds"), so if no reply has the exact words, the closest ones count.
    """
    replies = {c.id: _plain(" ".join(t.text for t in c.turns if t.speaker == "agent" and t.turn > 0)) for c in chats}
    found, missing = {}, []
    for quote in dict.fromkeys(re.findall(r"“([^”]+)”", guidelines)):
        ids = [i for i, text in replies.items() if f" {_plain(quote)} " in f" {text} "]
        if not ids:
            near = {i: _near(_plain(quote).split(), text.split()) for i, text in replies.items()}
            ids = [i for i, r in near.items() if r >= threshold and r == max(near.values())]
        if ids:
            found[quote] = ids
        else:
            missing.append(quote)
    return found, missing


def make_split(chats: list[GoldChat], quoted: set[str], seed: int = SPLIT_SEED) -> dict[str, list[str]]:
    """Half to tune on, half held out. Quoted chats go in the tuning half; the rest balance cards, chatbots and scores."""
    half = len(chats) // 2
    rest = sorted(c.id for c in chats if c.id not in quoted)
    if len(quoted) > half:
        raise GoldError(f"{len(quoted)} quoted chats don't fit in a tuning half of {half}")

    def groups(c: GoldChat) -> list[tuple]:
        return ([("card", c.card, c.profile_present)] * 2 + [("system", c.system)] * 2
                + [(d, v) for d, v in c.scores.items()] + [(k, v) for k, v in c.codes.items()])

    totals = Counter(g for c in chats for g in groups(c))
    rng, best = random.Random(seed), None
    for _ in range(5000):
        tune = quoted | set(rng.sample(rest, half - len(quoted)))
        got = Counter(g for c in chats if c.id in tune for g in groups(c))
        cost = sum((got[g] - n / 2) ** 2 for g, n in totals.items())
        if best is None or cost < best[0]:
            best = (cost, tune)
    tune = best[1]
    return {"tune": sorted(tune), "heldout": sorted(c.id for c in chats if c.id not in tune), "quoted": sorted(quoted)}


def build(source: Path, folder: Path = GOLD_DIR) -> GoldSet:
    """Read the preprint (PDF, or `pdftotext -layout` text) and save the gold set. A saved split is never changed."""
    if not source.is_file():
        raise GoldError(f"no file {source}")
    if source.suffix == ".txt":
        text = source.read_text(encoding="utf-8")
    else:
        try:
            text = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(source), "-"], capture_output=True,
                                  check=True).stdout.decode("utf-8")
        except FileNotFoundError as e:
            raise GoldError("pdftotext isn't installed (macOS: brew install poppler)") from e
        except subprocess.CalledProcessError as e:
            raise GoldError(f"pdftotext failed: {e.stderr.decode(errors='replace').strip()}") from e
    chats, guidelines = parse_preprint(text)
    per_system = Counter(c.system for c in chats)
    if len(chats) != N_CHATS or len(per_system) != N_SYSTEMS or set(per_system.values()) != {CHATS_PER_SYSTEM}:
        raise GoldError(f"expected {N_SYSTEMS} chatbots with {CHATS_PER_SYSTEM} chats each, found {dict(per_system)}")
    said: dict[tuple, set] = {}        # the child's and caregiver's lines must be the same for every chatbot
    for c in chats:
        said.setdefault((c.card, c.profile_present), set()).add(tuple(t.text for t in c.turns if t.speaker != "agent"))
    if odd := [k for k, v in said.items() if len(v) > 1]:
        raise GoldError(f"the child's lines differ between chatbots on {odd}")
    found, missing = quoted_in(guidelines, chats)

    split_file = folder / "split.json"
    if split_file.exists():
        split = json.loads(split_file.read_text())
        print(f"kept the saved split in {split_file}")
    else:
        split = make_split(chats, {i for ids in found.values() for i in ids})
    try:
        gold = GoldSet(chats=chats, split=split)
    except ValidationError as e:
        raise GoldError(f"the saved split doesn't fit these chats: {_why(e)}") from e
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "chats.jsonl").write_text("".join(c.model_dump_json() + "\n" for c in chats), encoding="utf-8")
    if not split_file.exists():
        split_file.write_text(json.dumps(split, indent=2) + "\n")
    print(f"wrote {folder}: {len(chats)} chats; {len(found)} guideline quotes found in {len(split['quoted'])} chats")
    for q in missing:
        print(f"  guideline quote not in any chat: “{q}”")
    return gold


# ------------------------------------------------------------------- comparing

ORDINAL = {0: "no", 1: "partial", 2: "yes"}     # compute_agreement orders these three labels


def read_judge_scores(p: Path) -> dict[str, dict]:
    """{chat id: {"domains": {...}, "codes": {...}}} from a scores.jsonl. Raises on anything it can't compare."""
    out = {}
    for d in _read_jsonl(p):
        tid, domains, codes = d.get("transcript_id"), d.get("domains"), d.get("codes") or {}
        if not isinstance(domains, dict) or set(domains) != set(DOMAINS) or any(
                type(v) is not int or v not in (0, 1, 2) for v in domains.values()):
            raise GoldError(f"{p.name}: {tid} needs \"domains\" with 0, 1 or 2 for each of {', '.join(DOMAINS)} "
                            "(see the top of harness/gold.py)")
        codes = {k: int(v) if isinstance(v, bool) else v for k, v in codes.items() if k in CODES and v not in (None, "na")}
        for k, v in codes.items():
            if v not in CODES[k]:
                raise GoldError(f"{p.name}: {tid} has an unknown {k} code '{v}'; expected one of {CODES[k]}")
        if tid in out:
            raise GoldError(f"{p.name}: {tid} is scored twice")
        out[tid] = {"domains": domains, "codes": codes}
    return out


def _measure(human: dict, judge: dict, *, ordinal: bool) -> dict:
    """One item. A chat the judge didn't score counts as a disagreement; kappa uses only the chats both scored."""
    n = len(human)
    # Ties go to the value that scores better, so the judge has to beat the best constant guess.
    guess = max(sorted(set(human.values()), key=str),
                key=lambda v: (Counter(human.values())[v], ordinal and sum(abs(h - v) <= 1 for h in human.values())))
    both = {k: v for k, v in judge.items() if k in human}
    to_label = (lambda v: ORDINAL[v]) if ordinal else str
    agree = compute_agreement({k: to_label(human[k]) for k in both}, {k: to_label(v) for k, v in both.items()}, ordinal=ordinal)
    exact = sum(human[k] == v for k, v in both.items())
    base = sum(h == guess for h in human.values())
    out = {"n": n, "judged": len(both), "exact": round(exact / n, 3), "baseline_guess": guess,
           "baseline_exact": round(base / n, 3), "beats_baseline": exact > base, "kappa": agree["kappa"], "kappa_ci95": agree["ci95"]}
    if ordinal:
        out["within_1"] = round(sum(abs(human[k] - v) <= 1 for k, v in both.items()) / n, 3)
        out["baseline_within_1"] = round(sum(abs(h - guess) <= 1 for h in human.values()) / n, 3)
    return out


def compare(chats: list[GoldChat], judge: dict[str, dict]) -> dict:
    """Judge vs human on these chats: section 9's measures per domain and per code. Other chats' scores are ignored."""
    if not chats:
        raise GoldError("no chats to compare")
    judge = {c.id: judge[c.id] for c in chats if c.id in judge}
    domains = {d: _measure({c.id: c.scores[d] for c in chats}, {k: j["domains"][d] for k, j in judge.items()}, ordinal=True)
               for d in DOMAINS}
    codes = {}
    for name in CODES:
        human = {c.id: c.codes[name] for c in chats if name in c.codes}
        if not human:
            continue
        said = {k: j["codes"][name] for k, j in judge.items() if name in j["codes"]}
        bad = PROBLEMS[name]
        codes[name] = _measure(human, said, ordinal=False) | {
            "problems": sum(v in bad for v in human.values()),
            "missed": sum(v in bad and said.get(k) not in bad for k, v in human.items()),
            "false_alarms": sum(v not in bad and said.get(k) in bad for k, v in human.items())}
    totals = {c.id: c.total for c in chats}
    judged = {k: sum(j["domains"].values()) for k, j in judge.items()}
    diff = [judged[k] - totals[k] for k in judged]
    return {"n": len(chats), "judged": len(judge), "not_judged": sorted(set(totals) - set(judge)), "domains": domains,
            "codes": codes, "total_mean_difference": round(sum(diff) / len(diff), 2) if diff else None}


def disagreements(chats: list[GoldChat], judge: dict[str, dict]) -> list[str]:
    """One line per chat the judge scored differently (ids and numbers only). For the tuning half."""
    out = []
    for c in chats:
        if c.id not in judge:
            continue
        j = judge[c.id]
        parts = [f"{d} {c.scores[d]}→{j['domains'][d]}" for d in DOMAINS if c.scores[d] != j["domains"][d]]
        parts += [f"{k} {v}→{j['codes'].get(k, 'no label')}" for k, v in c.codes.items() if j["codes"].get(k) != v]
        if parts:
            out.append(f"{c.id} (card {c.card}): " + ", ".join(parts))
    return out


def print_report(r: dict) -> None:
    def kappa(m: dict) -> str:
        if m["kappa"] is None:
            return "n/a"
        return f"{m['kappa']:.2f}" + (f" [{m['kappa_ci95'][0]:.2f}, {m['kappa_ci95'][1]:.2f}]" if m["kappa_ci95"] else "")

    print(f"{r['judged']} of {r['n']} chats judged" + (f"; not judged, counted as wrong: {', '.join(r['not_judged'])}" if r["not_judged"] else ""))
    print(f"\n{'domain':<16}{'n':>4}{'exact':>8}{'guess':>8}{'within 1':>10}{'guess':>8}  {'weighted kappa':<22}beats the guess")
    for d, m in r["domains"].items():
        print(f"{d:<16}{m['n']:>4}{m['exact']:>8.0%}{m['baseline_exact']:>8.0%}{m['within_1']:>10.0%}"
              f"{m['baseline_within_1']:>8.0%}  {kappa(m):<22}{'yes' if m['beats_baseline'] else 'NO'} (always {m['baseline_guess']})")
    print(f"\n{'code':<16}{'n':>4}{'exact':>8}{'guess':>8}  {'kappa':<22}{'problems':>9}{'missed':>8}{'false alarms':>14}")
    for k, m in r["codes"].items():
        print(f"{k:<16}{m['n']:>4}{m['exact']:>8.0%}{m['baseline_exact']:>8.0%}  {kappa(m):<22}"
              f"{m['problems']:>9}{m['missed']:>8}{m['false_alarms']:>14}")
    if r["total_mean_difference"] is not None:
        print(f"\ntotal (0-8): the judge's is {r['total_mean_difference']:+.2f} from the human total on average")
    print("'guess' always gives the most common human score in this split. Small samples: kappa ranges are wide.")


# --------------------------------------------------------------------- running


def judge_gold(gold: GoldSet, split: SplitName, *, folder: Path = GOLD_DIR, rubric: str = RUBRIC,
               judge_config: str = JUDGE_CONFIG, guardrails: bool = True, label: Optional[str] = None) -> Path:
    """Score one half with the judge, through `harness.score`. Returns the folder it wrote, inside private/gold/."""
    run_dir = folder / "runs" / split
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"gold_{split}"
    cfg = yaml.safe_load((REPO_ROOT / judge_config).read_text())
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": run_id, "config": cfg}))
    (run_dir / "transcripts.jsonl").write_text(
        "".join(c.transcript(run_id).model_dump_json() + "\n" for c in gold.part(split)), encoding="utf-8")
    return rescore(run_dir, rubric_file=rubric, judge_config=judge_config, guardrails=guardrails, label=label)


def _heldout_uses(folder: Path) -> list[dict]:
    log = folder / "heldout_log.jsonl"
    return _read_jsonl(log) if log.exists() else []


def run(split: SplitName, *, folder: Path = GOLD_DIR, scores: Optional[str] = None, rubric: Optional[str] = None,
        judge_config: Optional[str] = None, guardrails: bool = True, label: Optional[str] = None) -> dict:
    """`make gold`: judge one half (or read existing scores), compare, print and save the report."""
    if not scores and not rubric and not (REPO_ROOT / RUBRIC).exists():
        print(f"{RUBRIC} doesn't exist yet (B12), so this compares an INVENTED fixture. The numbers mean nothing.\n")
        gold, judge = load_gold(FIXTURE_DIR), read_judge_scores(FIXTURE_DIR / "judge_scores.jsonl")
        report = compare(gold.part(split), judge)
        print_report(report)
        return report

    gold = load_gold(folder)
    for name in [scores] if scores else [rubric or RUBRIC, judge_config or JUDGE_CONFIG]:
        if not (REPO_ROOT / name).is_file():          # an absolute path stays absolute
            raise GoldError(f"no file {name}")
    if split == "heldout":
        if used := _heldout_uses(folder):
            print(f"WARNING: the held-out half was already used {len(used)} time(s), first on {used[0]['at']}. If it led "
                  "to a change, it is no longer unseen (RFC 0003).")
        # Logged before judging, so a run that stops partway still counts.
        with open(folder / "heldout_log.jsonl", "a") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "scores": scores,
                                "judge_config": judge_config or JUDGE_CONFIG, "rubric": rubric or RUBRIC,
                                "judge_guardrails": guardrails}) + "\n")
    if scores:
        path = REPO_ROOT / scores
        out = folder / "runs" / split
        out.mkdir(parents=True, exist_ok=True)
        report_file = out / f"report_{label or datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    else:
        out = judge_gold(gold, split, folder=folder, rubric=rubric or RUBRIC, judge_config=judge_config or JUDGE_CONFIG,
                         guardrails=guardrails, label=label)
        path, report_file = out / "scores.jsonl", out / "gold_report.json"
        if not path.exists():
            raise GoldError(f"the judge scored no chat; see {out}")
        print()
    judge = read_judge_scores(path)
    chats = gold.part(split)
    report = compare(chats, judge) | {"split": split, "scores": str(path)}
    print_report(report)
    if split == "tune":                      # never for the held-out half: it would show what to tune on it
        lines = disagreements(chats, judge)
        print(f"\nhuman→judge, {len(lines)} chats that differ:")
        for line in lines:
            print(f"  {line}")
    report_file.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {report_file}")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load the gold set and compare the judge with the human scores.")
    ap.add_argument("--build", metavar="PDF", help="read the preprint and save private/gold/")
    ap.add_argument("--check", action="store_true", help="load and validate the saved gold set, then stop")
    ap.add_argument("--split", choices=["tune", "heldout"], default="tune")
    ap.add_argument("--scores", help="compare this scores.jsonl instead of running the judge")
    ap.add_argument("--rubric", help=f"default: {RUBRIC}")
    ap.add_argument("--judge-config", help=f"config to take the judge from (default: {JUDGE_CONFIG})")
    ap.add_argument("--judge-guardrails", choices=["on", "off"], default="on", help="off skips the quote retry")
    ap.add_argument("--label", help="folder name after rescore_")
    a = ap.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")
    try:
        if a.build or a.check:
            gold = build(Path(a.build).expanduser()) if a.build else load_gold()
            if len(gold.chats) != N_CHATS:
                raise GoldError(f"expected {N_CHATS} chats, found {len(gold.chats)}")
            print(f"gold set ok: {len(gold.chats)} chats, every total adds up; {len(gold.split['tune'])} tuning "
                  f"({len(gold.split['quoted'])} quoted in the guidelines), {len(gold.split['heldout'])} held out")
            return 0
        run(a.split, scores=a.scores, rubric=a.rubric, judge_config=a.judge_config,
            guardrails=a.judge_guardrails == "on", label=a.label)
    except (GoldError, ScoreError, ModelError) as e:
        print(f"gold: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
