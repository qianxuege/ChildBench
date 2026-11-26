"""Realism and script-fidelity report for the pretend child (A8, RFC 0002).

Two questions about a saved run, answered without any model:

1. **Realism.** Do the simulated lines look like real children's speech at that age? Words per
   sentence, vocabulary variety (MATTR), share of sentences that are questions, and the share of
   words learned after the child's age (Kuperman et al., 2012). The same numbers for real children
   come from CHILDES where it covers the age. Fallback lines (the scripted line used as written)
   are reported separately, as they weren't written by the model.
2. **Script fidelity.** Does the child do what the scenario asks? Turns delivered, key facts on the
   first try and after retries, fallback rate and pushback when due, per chatbot and overall,
   over complete conversations. Incomplete ones are counted separately.

Usage:
    python -m harness.realism --run <run_id>|latest            writes realism.json and realism.md in the run folder
    python -m harness.realism build-childes --cha-dir data/raw/childes [--norms data/raw/kuperman_aoa.csv]

The CHILDES and Kuperman files aren't in the repo (licences: see data/README.md). Without them the
report still computes the simulated side and the fidelity counts, and says the reference is missing.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import REPO_ROOT
from .schemas import Persona, Transcript
from .run import load_personas
from .score import ScoreError, _read_jsonl, resolve_run, run_scenarios, why_not_scored
from .tools.lint import LIMITS as LINT_LIMITS, _WORD
from .tools.referral import detect_referral

DATA = REPO_ROOT / "data"
REFERENCE = DATA / "childes_reference.json"
NORMS = DATA / "raw" / "kuperman_aoa.csv"
MATTR_WINDOW = 25            # small, because a scenario's child says only a few hundred words per age
MIN_SENTENCES = 2            # fewer than this on either side: no comparison
MIN_CHILDREN = 5             # fewer CHILDES children than this at an age: shown, not compared

SOURCES = {
    "childes": "MacWhinney, B. (2000). The CHILDES Project: Tools for analyzing talk (3rd ed.). Lawrence Erlbaum. "
               "https://talkbank.org/childes/",
    "kuperman": "Kuperman, V., Stadthagen-Gonzalez, H., & Brysbaert, M. (2012). Age-of-acquisition ratings for "
                "30,000 English words. Behavior Research Methods, 44(4), 978-990. https://doi.org/10.3758/s13428-012-0210-4",
}

# TalkBank's rules: any use of a corpus is cited with its own reference (from the corpus page).
CORPUS_CITATIONS = {
    "OCSC": "Wagner, L., Alghowinhem, S., Alwan, A., Bowdrie, K., Breazeal, C., Clopper, C. G., Fosler-Lussier, E., "
            "Jamsek, I. A., Lander, D., Ramnath, R., & Ross, J. (2025). The Ohio Child Speech Corpus. Speech Communication, "
            "170, 103206. doi:10.21415/60TV-D669",
}

LIMITS_TEXT = [
    "CHILDES is spoken, while children type to chatbots.",
    "CHILDES children mostly talk to parents about everyday things, so it checks how the child speaks, not what it discloses.",
    "The age-of-acquisition norms are adults' memories of when they learned a word, not observed child vocabulary.",
    "The speech check (lint_child_utterance) already shapes line length and adult words, so the simulated lines are partly "
    "selected to pass these measures.",
    "No one rates how childlike the lines sound; people check reaction turns only in the 10% audit sample.",
    "Samples per age are small on the simulated side. Effect sizes matter more than p-values, and a non-significant test "
    "isn't proof of a match.",
    "Words in one conversation aren't independent, so the p-values for word shares are optimistic.",
    "Pushback is rebuilt from the transcript with today's detect_referral; a run from before a change to it may differ "
    "from what the run did.",
]

_SENT = re.compile(r"[^.!?…\n]+[.!?…\n]*")


# ------------------------------------------------------------------ measures


def tokens(text: str) -> list[str]:
    return [w.strip("'").lower() for w in _WORD.findall(text) if w.strip("'")]


def sentences(line: str) -> list[tuple[list[str], bool]]:
    """A simulated line split the way the speech check splits it: (words, is a question)."""
    out = []
    for m in _SENT.finditer(line):
        words = tokens(m.group(0))
        if words:
            out.append((words, "?" in m.group(0)))
    return out


def mattr(words: list[str], window: int = MATTR_WINDOW) -> Optional[float]:
    """Moving-average type-token ratio (Covington & McFall, 2010); None if there are fewer words than the window."""
    if len(words) < window:
        return None
    counts = Counter(words[:window])
    total = len(counts)
    for i in range(window, len(words)):
        out, new = words[i - window], words[i]
        counts[out] -= 1
        if counts[out] == 0:
            del counts[out]
        counts[new] += 1
        total += len(counts)
    return round(total / (len(words) - window + 1) / window, 4)


def measures(units: list[tuple[list[str], bool]], age: Optional[int], norms: Optional[dict[str, float]],
             n_lines: Optional[int] = None) -> dict:
    """The realism numbers for a list of sentences. `age` is the child's age in years, for the AoA share."""
    lens = [len(w) for w, _ in units]
    words = [w for ws, _ in units for w in ws]
    n = len(lens)
    mean = sum(lens) / n if n else None
    sd = math.sqrt(sum((x - mean) ** 2 for x in lens) / (n - 1)) if n > 1 else None
    out = {
        "lines": n_lines if n_lines is not None else n,
        "sentences": n,
        "words": len(words),
        "words_per_sentence": {"mean": _r(mean), "sd": _r(sd), "p95": _p95(lens)},
        "questions": {"n": sum(q for _, q in units), "share": _r(sum(q for _, q in units) / n) if n else None},
        "vocabulary": {"types": len(set(words)), "ttr": _r(len(set(words)) / len(words)) if words else None,
                       "mattr": mattr(words), "window": MATTR_WINDOW},
        "aoa": None,
    }
    if norms is not None and age is not None:
        rated = [norms[w] for w in words if w in norms]
        late = sum(a > age for a in rated)
        out["aoa"] = {"rated_words": len(rated), "coverage": _r(len(rated) / len(words)) if words else None,
                      "after_age": late, "share_after_age": _r(late / len(rated)) if rated else None}
    return out


def compare(sim: dict, ref: dict) -> Optional[dict]:
    """Effect sizes and tests, simulated minus CHILDES. None if either side is too small."""
    if sim["sentences"] < MIN_SENTENCES or ref["sentences"] < MIN_SENTENCES:
        return None
    from scipy import stats

    s, r = sim["words_per_sentence"], ref["words_per_sentence"]
    out: dict = {}
    if s["sd"] is not None and r["sd"] is not None:
        pooled = math.sqrt(((sim["sentences"] - 1) * s["sd"] ** 2 + (ref["sentences"] - 1) * r["sd"] ** 2)
                           / (sim["sentences"] + ref["sentences"] - 2))
        t = stats.ttest_ind_from_stats(s["mean"], s["sd"], sim["sentences"], r["mean"], r["sd"], ref["sentences"],
                                       equal_var=False)
        out["words_per_sentence"] = {"difference": _r(s["mean"] - r["mean"]),
                                     "cohens_d": _r((s["mean"] - r["mean"]) / pooled) if pooled else None,
                                     "welch_p": _r(float(t.pvalue), 4) if not math.isnan(t.pvalue) else None}
    out["questions"] = _props(sim["questions"]["n"], sim["sentences"], ref["questions"]["n"], ref["sentences"])
    sm, rm = sim["vocabulary"]["mattr"], ref["vocabulary"]["mattr"]
    out["mattr_difference"] = _r(sm - rm) if sm is not None and rm is not None else None
    if sim["aoa"] and ref["aoa"]:
        out["aoa_share_after_age"] = _props(sim["aoa"]["after_age"], sim["aoa"]["rated_words"],
                                            ref["aoa"]["after_age"], ref["aoa"]["rated_words"])
    return out


def _props(x1: int, n1: int, x2: int, n2: int) -> Optional[dict]:
    """Cohen's h and a two-proportion z-test."""
    if not n1 or not n2:
        return None
    from scipy import stats

    p1, p2 = x1 / n1, x2 / n2
    pool = (x1 + x2) / (n1 + n2)
    se = math.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    p = float(2 * stats.norm.sf(abs(p1 - p2) / se)) if se else None
    return {"difference": _r(p1 - p2), "cohens_h": _r(2 * math.asin(math.sqrt(p1)) - 2 * math.asin(math.sqrt(p2))),
            "z_test_p": _r(p, 4)}


def _r(x: Optional[float], nd: int = 3) -> Optional[float]:
    return None if x is None else round(x, nd)


def _p95(xs: list[int]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    return float(s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)])


# ------------------------------------------------------------------ reference data


def load_norms(path: Optional[Path]) -> Optional[dict[str, float]]:
    """Kuperman AoA norms from a CSV (or an .xlsx if openpyxl is installed): word -> age in years. None if missing."""
    if path is None or not path.is_file():
        return None
    if path.suffix.lower() in {".xlsx", ".xls"}:
        import openpyxl  # optional; save the sheet as CSV to avoid it

        ws = openpyxl.load_workbook(path, read_only=True).active
        rows = [[("" if c is None else str(c)) for c in row] for row in ws.iter_rows(values_only=True)]
    else:
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    head = [h.strip().lower() for h in rows[0]]
    if "word" not in head:
        raise ValueError(f"{path}: no 'Word' column")
    aoa_col = next((head.index(c) for c in ("rating.mean", "aoa_kup_lem", "aoa_kup", "aoa") if c in head), None)
    if aoa_col is None:
        raise ValueError(f"{path}: no AoA column (Rating.Mean, AoA_Kup_lem, AoA_Kup or AoA)")
    wi, norms = head.index("word"), {}
    for row in rows[1:]:
        try:
            word, aoa = row[wi].strip().lower(), float(row[aoa_col])
        except (ValueError, IndexError):
            continue
        if word and word not in norms:
            norms[word] = aoa
    return norms


_CLINICAL_OK = {"", "td", "typical", "typ", "normal"}
# Drilled tasks (@G gems), not the child's own speech: naming letters and numbers, plural tests, reading aloud.
DRILL_TASKS = frozenset({"alphabet", "numbers", "wug", "reading"})
_CHA_DROP = [
    re.compile(r"\x15[^\x15]*\x15"),         # time bullets
    re.compile(r"\u21ab[^\u21ab]*\u21ab"),     # ↫sh↫ repetition markers
    re.compile(r"\[[^\]]*\]"),               # [: replacement], [*], [/], [=! laughs] ...
    re.compile(r"\+[^\s]*"),                 # +... +/. +"/. and other terminators and linkers
    re.compile(r"(?<!\S)(&\S*|0\S*|xxx|yyy|www)(?!\S)"),   # fillers, omitted words, unintelligible
]


def _task(gem: str) -> str:
    return re.sub(r"[^a-z]", "", gem.lower())       # "HowTo ." -> "howto"


def parse_cha(text: str, skip_tasks: frozenset[str] = frozenset()) -> tuple[Optional[int], str, list, Counter]:
    """The target child's age in years, group, utterances and, per skipped task (@G), how many were left out."""
    age, group, utts, cur, task, dropped = None, "", [], None, "", Counter()
    lines = text.replace("\r\n", "\n").split("\n")
    merged: list[str] = []
    for line in lines:          # continuation lines start with a tab
        if line.startswith("\t") and merged:
            merged[-1] += " " + line.strip()
        else:
            merged.append(line)
    for line in merged:
        if line.startswith("@ID:"):
            f = [x.strip() for x in line[4:].split("|")]
            if len(f) > 7 and f[2] == "CHI":
                m = re.match(r"(\d+);", f[3])
                age, group = (int(m.group(1)) if m else None), f[5]
        elif line.startswith("@G:"):
            task = _task(line[3:])
        elif line.startswith("*CHI:"):
            if task in skip_tasks:
                dropped[task] += 1
                continue
            cur = line[5:].strip()
            q = bool(re.search(r"\?\s*(\x15[^\x15]*\x15)?\s*$", cur))
            for pat in _CHA_DROP:
                cur = pat.sub(" ", cur)
            cur = re.sub(r"@\S*", "", cur)            # word@b -> word
            cur = cur.replace("(", "").replace(")", "")   # goin(g) -> going
            cur = re.sub(r"[_+]", " ", cur)            # compounds
            words = tokens(cur)
            if words:
                utts.append((words, q))
    return age, group, utts, dropped


def build_childes(cha_dir: Path, norms: Optional[dict[str, float]], out: Path,
                  skip_tasks: frozenset[str] = DRILL_TASKS) -> dict:
    """Summary numbers per year of age from every .cha file under cha_dir. Typically developing children only,
    leaving out drilled tasks. A child is a corpus plus file name (e.g. Bates/frankst across its sessions)."""
    by_age: dict[int, list] = defaultdict(list)
    children: dict[int, set] = defaultdict(set)
    corpora: Counter = Counter()
    skipped: Counter = Counter()
    tasks_out: Counter = Counter()
    for f in sorted(cha_dir.rglob("*.cha")):
        age, group, utts, dropped = parse_cha(f.read_text(encoding="utf-8", errors="replace"), skip_tasks)
        if age is None:
            skipped["no target child age"] += 1
            continue
        if group.lower() not in _CLINICAL_OK:
            skipped[f"group {group}"] += 1
            continue
        by_age[age].extend(utts)
        tasks_out.update(dropped)
        rel = f.relative_to(cha_dir)
        children[age].add((rel.parts[0] if len(rel.parts) > 1 else ".") + "/" + f.stem)
        corpora[rel.parts[0] if len(rel.parts) > 1 else "."] += 1
    if not by_age:
        raise ValueError(f"no usable CHILDES files under {cha_dir}")
    ref = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCES["childes"], "aoa_source": SOURCES["kuperman"] if norms else None,
        "citations": {c: CORPUS_CITATIONS.get(c, "add this corpus's reference from its CHILDES page") for c in sorted(corpora)},
        "corpora": dict(sorted(corpora.items())), "skipped_files": dict(skipped),
        "skipped_tasks": {"tasks": sorted(skip_tasks), "utterances": dict(sorted(tasks_out.items()))},
        "note": "Target-child utterances only (*CHI), typically developing children; ages are whole years.",
        "by_age": {str(a): {**measures(u, a, norms), "children": len(children[a])} for a, u in sorted(by_age.items())},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ref, indent=2) + "\n")
    return ref


# ------------------------------------------------------------------ the run


def _strip_cue(p: Persona, turn: int, text: str) -> str:
    """Code, not the model, added the age cue to the first line, so it doesn't count as the child's speech."""
    cue = p.age_cue
    if turn == 1 and cue and text.lower().startswith(cue.lower()):
        return text[len(cue):].strip()
    return text


def pushback_status(p: Persona, t: Transcript) -> Optional[tuple[str, int]]:
    """('said' | 'skipped', turn of the referral) if the rule called for a pushback, else None (RFC 0002)."""
    if not (p.is_referral and p.pushback_line):
        return None
    last = len(p.turns)
    child = {x.turn for x in t.turns if x.speaker == "child"}
    for x in sorted((x for x in t.turns if x.speaker == "agent" and x.turn >= last), key=lambda x: x.turn):
        if detect_referral(x.text):
            return ("said" if x.turn + 1 in child else "skipped"), x.turn
    return None


def phase_of(p: Persona, t: Transcript, turn: int, push: Optional[tuple[str, int]]) -> str:
    if turn <= len(p.turns):
        return "script"
    if push and push[0] == "said" and turn == push[1] + 1:
        return "pushback"
    return "react"


def _key_fact_events(log: list[dict]) -> dict[tuple[str, int], dict[int, str]]:
    """(transcript id, turn) -> {attempt: result} for the key-facts check."""
    out: dict = defaultdict(dict)
    for e in log:
        if e.get("check") != "key-facts":
            continue
        m = re.match(r"^(.+):t(\d+):a(\d+)$", e.get("item_id", ""))
        if m:
            out[(m.group(1), int(m.group(2)))][int(m.group(3))] = e.get("result")
    return out


def _empty_fidelity() -> dict:
    return {"conversations": 0, "turns_delivered": [0, 0], "key_facts_first_try": [0, 0], "key_facts_final": [0, 0],
            "fallback": [0, 0], "fallback_by_phase": {"script": [0, 0], "pushback": [0, 0], "react": [0, 0], "unknown": [0, 0]},
            "pushback_when_due": [0, 0], "scenario_changed": 0}


def _fraction(pair: list[int]) -> dict:
    n, d = pair
    return {"n": n, "of": d, "share": round(n / d, 3) if d else None}


def fidelity(transcripts: list[Transcript], personas: dict[str, Persona], log: list[dict],
             changed: frozenset[str] | set[str] = frozenset()) -> dict:
    """Counts over complete conversations. A scenario whose file changed since the run can't be split into
    scripted, pushback and reaction turns again, so its chats count only towards the fallback rate."""
    events = _key_fact_events(log)
    recorded = bool(events)
    groups: dict[str, dict] = defaultdict(_empty_fidelity)
    incomplete: dict[str, list] = defaultdict(list)
    misses: list[dict] = []
    for t in transcripts:
        p = personas.get(t.scenario_id)
        if p is None:
            continue
        if why := why_not_scored(t):
            incomplete[t.agent_name].append({"transcript_id": t.transcript_id, "why": why, "ended_because": t.ended_because})
            continue
        child = {x.turn: x for x in t.turns if x.speaker == "child"}
        for g in (groups[t.agent_name], groups["__overall__"]):
            g["conversations"] += 1
        delta = _empty_fidelity()
        if t.scenario_id in changed:
            fb = sum(x.used_fallback for x in child.values())
            for g in (groups[t.agent_name], groups["__overall__"]):
                g["scenario_changed"] += 1
                g["fallback"] = [g["fallback"][0] + fb, g["fallback"][1] + len(child)]
                g["fallback_by_phase"]["unknown"] = [g["fallback_by_phase"]["unknown"][0] + fb,
                                                     g["fallback_by_phase"]["unknown"][1] + len(child)]
            continue
        push = pushback_status(p, t)
        n_script = len(p.turns)
        said = [k for k in child if 1 <= k <= n_script]
        delta["turns_delivered"] = [len(said), n_script]
        for k in range(1, n_script + 1):
            if k not in child:
                misses.append({"transcript_id": t.transcript_id, "turn": k, "what": "scripted turn not delivered"})
        for k, x in child.items():
            ph = phase_of(p, t, k, push)
            delta["fallback"][1] += 1
            delta["fallback_by_phase"][ph][1] += 1
            if x.used_fallback:
                delta["fallback"][0] += 1
                delta["fallback_by_phase"][ph][0] += 1
                misses.append({"transcript_id": t.transcript_id, "turn": k, "what": f"fallback ({ph})"})
        if recorded:
            for k in said:
                if not p.script_turn(k).must_say:
                    continue
                ev = events.get((t.transcript_id, k), {})
                first, final = ev.get(1) == "PASS", any(r == "PASS" for r in ev.values())
                delta["key_facts_first_try"][0] += first
                delta["key_facts_first_try"][1] += 1
                delta["key_facts_final"][0] += final
                delta["key_facts_final"][1] += 1
                if not final:
                    misses.append({"transcript_id": t.transcript_id, "turn": k, "what": "key facts missed on every try"})
                elif not first:
                    misses.append({"transcript_id": t.transcript_id, "turn": k, "what": "key facts missed on the first try"})
        if push:
            delta["pushback_when_due"] = [int(push[0] == "said"), 1]
            if push[0] == "skipped":
                misses.append({"transcript_id": t.transcript_id, "turn": push[1] + 1,
                               "what": "pushback due but not said (turn limit)"})
        for g in (groups[t.agent_name], groups["__overall__"]):
            for key in ("turns_delivered", "key_facts_first_try", "key_facts_final", "fallback", "pushback_when_due"):
                g[key] = [a + b for a, b in zip(g[key], delta[key])]
            for ph in g["fallback_by_phase"]:
                g["fallback_by_phase"][ph] = [a + b for a, b in zip(g["fallback_by_phase"][ph], delta["fallback_by_phase"][ph])]

    def show(g: dict) -> dict:
        kf = _fraction if recorded else (lambda pair: "not recorded")
        return {"conversations": g["conversations"],
                "scenario_changed": g["scenario_changed"],
                "turns_delivered": _fraction(g["turns_delivered"]),
                "key_facts_first_try": kf(g["key_facts_first_try"]),
                "key_facts_final": kf(g["key_facts_final"]),
                "fallback": _fraction(g["fallback"]),
                "fallback_by_phase": {k: _fraction(v) for k, v in g["fallback_by_phase"].items()},
                "pushback_when_due": _fraction(g["pushback_when_due"])}

    overall = groups.pop("__overall__", _empty_fidelity())
    return {"key_facts_recorded": recorded,
            "overall": show(overall),
            "by_chatbot": {k: show(v) for k, v in sorted(groups.items())},
            "incomplete": {"n": sum(len(v) for v in incomplete.values()), "by_chatbot": dict(incomplete)},
            "misses": misses}


def realism(transcripts: list[Transcript], personas: dict[str, Persona], norms: Optional[dict[str, float]],
            reference: Optional[dict]) -> dict:
    model: dict[int, list] = defaultdict(list)
    fallback: dict[int, list] = defaultdict(list)
    lines: Counter = Counter()
    for t in transcripts:
        p = personas.get(t.scenario_id)
        if p is None:
            continue
        for x in t.turns:
            if x.speaker != "child":
                continue
            units = sentences(_strip_cue(p, x.turn, x.text))
            (fallback if x.used_fallback else model)[t.child_age].extend(units)
            lines[(t.child_age, x.used_fallback)] += 1
    ref_ages = (reference or {}).get("by_age", {})
    by_age = {}
    for age in sorted(set(model) | set(fallback)):
        sim = measures(model[age], age, norms, lines[(age, False)])
        ref = ref_ages.get(str(age))
        by_age[str(age)] = {
            "simulated": sim,
            "fallback": measures(fallback[age], age, norms, lines[(age, True)]),
            "childes": ref,
            "comparison": compare(sim, ref) if ref and ref.get("children", 0) >= MIN_CHILDREN else None,
            "speech_check_limit": {"lint_limit": LINT_LIMITS.get(age), "simulated_p95": sim["words_per_sentence"]["p95"],
                                   "childes_p95": ref["words_per_sentence"]["p95"] if ref else None},
        }
    return {"by_age": by_age,
            "childes_available": reference is not None, "norms_available": norms is not None,
            "childes_ages": sorted(int(a) for a in ref_ages)}


def report(run_dir: Path, *, reference_path: Path = REFERENCE, norms_path: Optional[Path] = NORMS) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    transcripts = [Transcript(**r) for r in _read_jsonl(run_dir / "transcripts.jsonl")]
    personas = run_scenarios(manifest)
    log = _read_jsonl(run_dir / "guardrail_log.jsonl")
    norms = load_norms(norms_path)
    reference = json.loads(reference_path.read_text()) if reference_path and reference_path.is_file() else None
    enforce = (manifest.get("config", {}).get("guardrails") or {}).get("enforce", True)
    unknown = sorted({t.scenario_id for t in transcripts} - set(personas))
    _, hashes = load_personas(manifest["config"]["scenarios"]) if personas else ({}, {})
    changed = sorted(k for k, h in (manifest.get("scenarios") or {}).items() if k in hashes and hashes[k] != h)
    out = {
        "run_id": manifest.get("run_id", run_dir.name),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "guardrails": "on" if enforce else "off",
        "note": None if enforce else "Guardrails-off run: report it separately from guardrails-on runs.",
        "sample": {"conversations": len(transcripts), "child_lines": sum(x.speaker == "child" for t in transcripts for x in t.turns),
                   "chatbots": sorted({t.agent_name for t in transcripts}), "ages": sorted({t.child_age for t in transcripts}),
                   "scenarios_not_found": unknown, "scenarios_changed_since_run": changed},
        "realism": realism(transcripts, personas, norms, reference),
        "fidelity": fidelity(transcripts, personas, log, set(changed)),
        "sources": {**SOURCES, **{f"childes:{k}": v for k, v in ((reference or {}).get("citations") or {}).items()}},
        "limits": LIMITS_TEXT,
    }
    (run_dir / "realism.json").write_text(json.dumps(out, indent=2) + "\n")
    (run_dir / "realism.md").write_text(to_markdown(out))
    return out


# ------------------------------------------------------------------ markdown


def _f(x) -> str:
    if x is None:
        return "–"
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return "–" if x.get("share") is None else f"{x['n']}/{x['of']} ({x['share']:.0%})"
    return f"{x:g}" if isinstance(x, float) else str(x)


def _m(m: Optional[dict]) -> list[str]:
    if not m or not m["sentences"]:
        return ["–"] * 7
    a = m.get("aoa") or {}
    return [_f(m["sentences"]), _f(m["words"]), f"{_f(m['words_per_sentence']['mean'])} ± {_f(m['words_per_sentence']['sd'])}",
            _f(m["questions"]["share"]), _f(m["vocabulary"]["mattr"]), _f(a.get("share_after_age")), _f(a.get("coverage"))]


def to_markdown(r: dict) -> str:
    re_, fi, s = r["realism"], r["fidelity"], r["sample"]
    L = [f"# Realism and script fidelity · {r['run_id']}", "",
         f"Guardrails **{r['guardrails']}**." + (f" {r['note']}" if r["note"] else ""),
         f"{s['conversations']} conversations, {s['child_lines']} child lines, chatbots: {', '.join(s['chatbots']) or '–'}; "
         f"ages: {', '.join(map(str, s['ages'])) or '–'}."]
    if s["scenarios_not_found"]:
        L.append(f"Left out, scenario file not found: {', '.join(s['scenarios_not_found'])}.")
    if s["scenarios_changed_since_run"]:
        L.append(f"Scenario files changed since this run: {', '.join(s['scenarios_changed_since_run'])}. Their chats "
                 "count only towards the fallback rate, as their scripted, pushback and reaction turns can't be told apart again.")
    L += ["", "## Script fidelity", "",
          "Complete conversations only. Key facts are the scripted turns with `must_say` facts."
          + ("" if fi["key_facts_recorded"] else " This run is from before the key-fact check (A16): **not recorded**."), "",
          "| Chatbot | Chats | Turns delivered | Key facts, first try | Key facts, final | Fallback | Pushback when due |",
          "|---|---|---|---|---|---|---|"]
    for name, g in [*fi["by_chatbot"].items(), ("**overall**", fi["overall"])]:
        L.append(f"| {name} | {g['conversations']} | {_f(g['turns_delivered'])} | {_f(g['key_facts_first_try'])} | "
                 f"{_f(g['key_facts_final'])} | {_f(g['fallback'])} | {_f(g['pushback_when_due'])} |")
    ph = fi["overall"]["fallback_by_phase"]
    L += ["", f"Fallback by kind of turn: scripted {_f(ph['script'])}, pushback {_f(ph['pushback'])}, reaction {_f(ph['react'])}"
          + (f", scenario changed since the run {_f(ph['unknown'])}" if ph["unknown"]["of"] else "")
          + ". Reaction turns have no key facts; people check them in the 10% audit sample.", "",
          f"Incomplete conversations, not counted above: {fi['incomplete']['n']}"
          + "".join(f"; {k}: {len(v)}" for k, v in fi["incomplete"]["by_chatbot"].items()) + "."]
    if fi["misses"]:
        L += ["", "<details><summary>Every miss</summary>", ""] + \
             [f"- `{m['transcript_id']}` turn {m['turn']}: {m['what']}" for m in fi["misses"]] + ["", "</details>"]

    L += ["", "## Realism", "",
          f"CHILDES reference: {'yes, ages ' + ', '.join(map(str, re_['childes_ages'])) if re_['childes_available'] else 'not available (see data/README.md)'}. "
          f"Age-of-acquisition norms: {'yes' if re_['norms_available'] else 'not available'}. "
          f"The age cue that code adds to the first line is left out. Vocabulary variety is MATTR over {MATTR_WINDOW}-word windows.", "",
          "| Age | Lines from | Sentences | Words | Words per sentence | Questions | MATTR | Words learned after age | AoA coverage |",
          "|---|---|---|---|---|---|---|---|---|"]
    for age, a in re_["by_age"].items():
        for label, key in (("simulated", "simulated"), ("fallback", "fallback"), ("CHILDES", "childes")):
            L.append(f"| {age} | {label} | " + " | ".join(_m(a[key])) + " |")
    L += ["", f"**Simulated minus CHILDES** (effect sizes first; n is in the table above). Ages with fewer than "
          f"{MIN_CHILDREN} CHILDES children are shown above but not compared.", "",
          "| Age | Words per sentence: diff, d, p | Questions: diff, h, p | MATTR diff | Late words: diff, h, p |", "|---|---|---|---|---|"]
    any_cmp = False
    for age, a in re_["by_age"].items():
        c = a["comparison"]
        if not c:
            continue
        any_cmp = True
        w, q, o = c.get("words_per_sentence") or {}, c.get("questions") or {}, c.get("aoa_share_after_age") or {}
        L.append(f"| {age} | {_f(w.get('difference'))}, {_f(w.get('cohens_d'))}, {_f(w.get('welch_p'))} | "
                 f"{_f(q.get('difference'))}, {_f(q.get('cohens_h'))}, {_f(q.get('z_test_p'))} | {_f(c.get('mattr_difference'))} | "
                 f"{_f(o.get('difference'))}, {_f(o.get('cohens_h'))}, {_f(o.get('z_test_p'))} |")
    if not any_cmp:
        L.append("| – | no age with both sides | | | |")
    L += ["", "**Speech-check limits** (most words in one sentence; hand-set, `calibrated: false`):", "",
          "| Age | Limit | Simulated p95 | CHILDES p95 |", "|---|---|---|---|"]
    for age, a in re_["by_age"].items():
        sl = a["speech_check_limit"]
        L.append(f"| {age} | {_f(sl['lint_limit'])} | {_f(sl['simulated_p95'])} | {_f(sl['childes_p95'])} |")
    L += ["", "## Limits", ""] + [f"- {x}" for x in r["limits"]]
    L += ["", "## Sources", ""] + [f"- {v}" for v in r["sources"].values()] + [""]
    return "\n".join(L)


# ------------------------------------------------------------------ CLI


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["build-childes"]:
        ap = argparse.ArgumentParser(prog="python -m harness.realism build-childes")
        ap.add_argument("--cha-dir", required=True, type=Path, help="folder with CHILDES .cha files (any depth)")
        ap.add_argument("--norms", type=Path, default=NORMS, help="Kuperman AoA CSV, for the late-word share")
        ap.add_argument("--out", type=Path, default=REFERENCE)
        ap.add_argument("--skip-tasks", default=",".join(sorted(DRILL_TASKS)),
                        help="@G tasks to leave out, comma-separated; '' keeps all")
        a = ap.parse_args(argv[1:])
        try:
            ref = build_childes(a.cha_dir, load_norms(a.norms), a.out,
                                frozenset(_task(t) for t in a.skip_tasks.split(",") if _task(t)))
        except (ValueError, OSError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"wrote {a.out}: ages {', '.join(ref['by_age'])}")
        return 0
    ap = argparse.ArgumentParser(prog="python -m harness.realism")
    ap.add_argument("--run", default="latest", help="run id, or 'latest'")
    ap.add_argument("--results", type=Path, default=REPO_ROOT / "results")
    ap.add_argument("--reference", type=Path, default=REFERENCE, help="built by build-childes")
    ap.add_argument("--norms", type=Path, default=NORMS)
    a = ap.parse_args(argv)
    try:
        run_dir = resolve_run(a.run, a.results)
        r = report(run_dir, reference_path=a.reference, norms_path=a.norms)
    except (ScoreError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    o = r["fidelity"]["overall"]
    print(f"wrote {run_dir / 'realism.json'} and realism.md · {o['conversations']} complete chats · "
          f"fallback {_f(o['fallback'])} · pushback {_f(o['pushback_when_due'])} · key facts final {_f(o['key_facts_final'])}")
    if not r["realism"]["childes_available"]:
        print("note: no CHILDES reference; see data/README.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
