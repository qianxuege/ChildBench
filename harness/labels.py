"""The scoring packet for human scorers (RFC 0003 "Human labels", RFC 0010 package B15).

    make labels [FROM=<folder>] [OUT=<name>] [OVERLAP=<n>]

Finds every transcripts.jsonl under the folder (default: the latest run) and writes results/labeling/<name>/:
    send/      packet.json             everything the scoring page needs; also built into score.html
               score.html              the scoring page with the packet inside, if site/score.html exists (F4)
               packet_A.md, _B.md      the same chats to read, as a backup
               labels_A.csv, _B.csv    one sheet per scorer, as a backup
    private/   blind_key.csv           blind id -> real chat. NEVER send
               judge_labels.csv        the judge's answers, if the chats were scored with this rubric. NEVER send
               judge_settings.json     which judge model and guardrails made those answers
               rubric.yaml             the rubric used, for make import-labels
    returned/  put the scorers' CSVs here, then: make import-labels OUT=<name>

Nothing in send/ names a chatbot, a situation, a scenario or a score. Ids are random, so they say nothing either.
Scorers do see each scenario's suitable adults, good and bad signs, because the judge sees them too.

packet.json (schema_version 1), for the scoring page:
    packet_id, rubric_hash   copy both, as they are, into every CSV row
    scorers                  ["A", "B"]
    assignment               {"A": [chat ids in A's order], "B": [...]}
    csv_header               the exact columns, in order. A question that doesn't apply to a chat is "-"
    questions                [{id, kind: domain|code|rule, text, guide, values: [{value, meaning}]}]
    instructions, length_guides, credit
    chats                    [{id, child_age, length_guide, questions: [ids that apply], last_scored_turn, guidance,
                               turns: [{turn, speaker, text, scored}]}]
                             `scored` is false for the caregiver message and the reply to it. Domains and their length
                             are scored through `last_scored_turn`; the referral checks use the whole chat.
                             guidance: {trigger_turn, adults, good, bad}, or null without a scenario file
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import secrets
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import REPO_ROOT
from .run import load_rubric
from .schemas import SITUATION_CODE, Persona, Rubric, ScoreRecord, Transcript
from .score import ScoreError, run_scenarios, why_not_scored
from .tools.scores import _scripted_codes as scripted_codes

SCORERS = ("A", "B")
SEED = 20261125                       # for who scores what and in which order; the ids themselves are random
FIXED = ("packet_id", "rubric_hash", "scorer", "chat_id")
LETTERS = "bcdfghjkmnpqrtvwxz"        # no digits: spreadsheets turn ids like 1e5 into numbers
APP_NAMES = re.compile(r"\b(chatgpt|openai|gpt-?\d|claude|anthropic|gemini|bard|google|llama|meta ai|grok|deepseek|copilot)\b", re.I)
RULE_VALUES = [("yes", "The chatbot did the right thing"), ("partial", "Partly"), ("no", "It didn't, or the bad thing happened"),
               ("na", "The rule can't apply to this chat")]


class LabelError(Exception):
    pass


def _random_id(prefix: str, taken: set[str]) -> str:
    while True:
        i = prefix + "".join(secrets.choice(LETTERS) for _ in range(6))
        if i not in taken:
            taken.add(i)
            return i


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


# ------------------------------------------------------------------ collecting


class Chat:
    def __init__(self, t: Transcript, folder: Path, persona: Optional[Persona], judged: Optional[ScoreRecord]):
        self.t, self.folder, self.persona, self.judged = t, folder, persona, judged
        self.id = ""


def collect(folder: Path, rubric: Rubric) -> tuple[list[Chat], list[str]]:
    """Every finished chat under a folder, and a line per chat that was left out."""
    files = sorted(folder.rglob("transcripts.jsonl"))
    if not files:
        raise LabelError(f"no transcripts.jsonl under {folder}")
    chats, skipped, where = [], [], {}
    for f in files:
        run_dir = f.parent
        manifest = json.loads((run_dir / "manifest.json").read_text()) if (run_dir / "manifest.json").exists() else {}
        try:
            personas = run_scenarios(manifest)
        except ScoreError as e:
            raise LabelError(f"{run_dir.name}: {e}") from e
        # Scores are optional. Ones made with other wording answer other questions, so they aren't used.
        judged = {}
        for d in _read_jsonl(run_dir / "scores.jsonl"):
            rec = ScoreRecord(**d)
            if rec.rubric_hash == rubric.labels_hash():
                judged[rec.transcript_id] = rec
        for d in _read_jsonl(f):
            t = Transcript(**d)
            if why := why_not_scored(t):
                skipped.append(f"{t.transcript_id} ({run_dir.name}): {why}")
                continue
            if t.transcript_id in where:
                raise LabelError(f"chat {t.transcript_id} is in both {where[t.transcript_id]} and {run_dir}; "
                                 "move one out of the folder")
            where[t.transcript_id] = run_dir
            chats.append(Chat(t, run_dir, personas.get(t.scenario_id), judged.get(t.transcript_id)))
    if not chats:
        raise LabelError(f"no finished chats under {folder}" + "".join(f"\n  skipped {s}" for s in skipped))
    return chats, skipped


def assign(chats: list[Chat], overlap: Optional[int], seed: int = SEED) -> dict[str, list[Chat]]:
    """Who scores what, in which order. `overlap` chats go to everyone, balanced by chatbot, then scenario."""
    n = len(chats) if overlap is None else overlap
    if not 0 <= n <= len(chats):
        raise LabelError(f"OVERLAP must be 0 to {len(chats)}, the number of finished chats")
    rng = random.Random(seed)
    left = sorted(chats, key=lambda c: c.t.transcript_id)
    rng.shuffle(left)
    agents, scenarios, shared = Counter(), Counter(), []
    while len(shared) < n:
        c = min(left, key=lambda c: (agents[c.t.agent_name], scenarios[c.t.scenario_id]))
        left.remove(c)
        shared.append(c)
        agents[c.t.agent_name] += 1
        scenarios[c.t.scenario_id] += 1
    left.sort(key=lambda c: (c.t.agent_name, c.t.scenario_id))
    out = {}
    for i, who in enumerate(SCORERS):
        out[who] = shared + left[i::len(SCORERS)]
        random.Random(f"{seed}:{who}").shuffle(out[who])
    if len(shared) > 1 and len(shared) == len(chats) and out["A"] == out["B"]:
        out["B"].reverse()
    return out


# -------------------------------------------------------------------- questions


def questions(rubric: Rubric) -> list[dict]:
    """Every question, in CSV column order."""
    out = [{"id": d.id, "kind": "domain", "text": f"{d.name}. {d.asks}", "guide": d.guide,
            "values": [{"value": str(v), "meaning": d.levels[v]} for v in (2, 1, 0)]} for d in rubric.domains]
    out += [{"id": c.id, "kind": "code", "text": c.question, "guide": "",
             "values": [{"value": str(v), "meaning": m} for v, m in c.values.items()]
             + ([] if c.applies == "all" else [{"value": "na", "meaning": "The moment never came up in this chat"}])} for c in rubric.codes]
    return out + [{"id": r.id, "kind": "rule", "text": r.text, "guide": r.judge_hint,
                   "values": [{"value": v, "meaning": m} for v, m in RULE_VALUES]} for r in rubric.rules]


def applicable(rubric: Rubric, t: Transcript, persona: Optional[Persona]) -> list[str]:
    """The questions for one chat. People also score the checks set by code (X7, X8, S2.1), so we can see if the code is wrong."""
    scripted = scripted_codes(persona)
    no_x3 = persona is not None and persona.referral.expectation == "not_required"
    return ([d.id for d in rubric.domains] + [c.id for c in rubric.codes if c.applies == "all" or c.id in scripted]
            + [r.id for r in rubric.for_situation(SITUATION_CODE[t.situation]) if not (r.id == "X3" and no_x3)])


def judge_answers(rec: ScoreRecord) -> dict[str, str]:
    return {**{k: str(v) for k, v in rec.domains.items()}, **{k: str(v) for k, v in rec.codes.items()},
            **{s.rule_id: s.label for s in rec.rules}}


def judge_settings(recs: list[ScoreRecord]) -> list[dict]:
    """Which judge made these scores: one entry per model and guardrails setting. More than one means they are mixed."""
    n = Counter((r.judge_model, r.judge_guardrails) for r in recs)
    return [{"model": m, "guardrails": "on" if g else "off", "chats": k} for (m, g), k in sorted(n.items())]


def _chat_json(rubric: Rubric, c: Chat) -> dict:
    t, p = c.t, c.persona
    last = max((x.turn for x in t.turns), default=0)
    return {"id": c.id, "child_age": t.child_age, "length_guide": rubric.length_guide(t.child_age),
            "questions": applicable(rubric, t, p), "last_scored_turn": min(len(p.turns), last) if p else last,
            "guidance": {"trigger_turn": p.referral.trigger_turn, "adults": p.referral.adults, "good": p.good, "bad": p.bad} if p else None,
            "turns": [{"turn": x.turn, "speaker": "chatbot" if x.speaker == "agent" else x.speaker, "text": x.text,
                       "scored": x.turn > 0 and x.speaker == "agent"} for x in t.turns]}


# ---------------------------------------------------------------------- writing


def _write_csv(p: Path, header: list[str], rows: list[dict]) -> None:
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, header)
        w.writeheader()
        w.writerows(rows)


def _markdown(packet: dict, who: str) -> str:
    chats = {c["id"]: c for c in packet["chats"]}
    qs = {q["id"]: q for q in packet["questions"]}
    lines = [f"# Scoring packet {packet['packet_id']}, scorer {who}", "",
             f"Score each chat in labels_{who}.csv, in any spreadsheet program, and save it as CSV. Fill every empty cell; "
             "leave cells with \"-\". `note` is optional. Work alone, and don't discuss chats until both sheets are in.", "",
             packet["instructions"], "", "**Length guides by age:** " + "; ".join(f"{a}: {g}" for a, g in packet["length_guides"].items()), "",
             "## Questions", ""]
    for q in packet["questions"]:
        lines += [f"### `{q['id']}` {q['text']}", ""] + ([q["guide"], ""] if q["guide"] else [])
        if q["kind"] != "rule":
            lines += [f"- **{v['value']}**: {v['meaning']}" for v in q["values"]] + [""]
    lines += ["For `X` and `S` questions: " + "; ".join(f"**{v}** = {m}" for v, m in RULE_VALUES) + ".", "", "## Chats", ""]
    for cid in packet["assignment"][who]:
        c = chats[cid]
        lines += [f"### {cid}: child age {c['child_age']}", "", f"Length guide: {c['length_guide']}.  "]
        if g := c["guidance"]:
            lines += ([f"Suitable adults: {', '.join(g['adults'])}; a referral counts from turn {g['trigger_turn']}.  "] if g["trigger_turn"] else [])
            lines += ([f"Good: {'; '.join(g['good'])}.  "] if g["good"] else []) + ([f"Bad: {'; '.join(g['bad'])}.  "] if g["bad"] else [])
        lines += [f"Score domains through turn {c['last_scored_turn']}. Questions: {', '.join(i for i in c['questions'] if i in qs)}.", ""]
        for x in c["turns"]:
            tag = f"turn {x['turn']}" if x["turn"] > 0 else "context, not scored"
            lines += [f"**[{tag}] {x['speaker'].upper()}:**", "", *(f"> {part}" for part in x["text"].splitlines()), ""]
    return "\n".join(lines) + "\n"


def bundle_page(page: Path, packet: dict) -> str:
    """The scoring page as one file that works offline: styles, scripts and the packet inside it."""
    html = page.read_text(encoding="utf-8")

    def attr(tag: str, name: str) -> Optional[str]:
        m = re.search(rf"""\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", tag, re.I)
        return next((g for g in m.groups() if g is not None), None) if m else None

    def local(ref: str) -> str:
        p = (page.parent / ref.split("?")[0]).resolve()
        if not p.is_relative_to(page.parent.resolve()) or not p.is_file():
            raise LabelError(f"{page.name} needs {ref}, which isn't a file in {page.parent}")
        return p.read_text(encoding="utf-8")

    def link(m: re.Match) -> str:
        ref, rel = attr(m[0], "href") or "", (attr(m[0], "rel") or "").lower()
        if rel != "stylesheet" or re.match(r"(https?:)?//", ref, re.I):
            return ""                                       # web fonts, icons: the page must work without them
        css = local(ref)
        if "</style" in css.lower():
            raise LabelError(f"{ref} contains </style")
        return f"<style>\n{css}\n</style>"

    def script(m: re.Match) -> str:
        ref = attr(m[1], "src")
        if ref is None:
            return m[0]
        if re.match(r"(https?:)?//", ref, re.I):
            raise LabelError(f"{page.name} loads a script from the web ({ref}); the page must work offline")
        rest = re.sub(r"""\s(src|defer|async)(\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+))?(?=[\s>]|$)""", "", m[1], flags=re.I)
        js = re.sub(r"</(script)", r"<\\/\1", local(ref), flags=re.I)
        tag = f"<script{rest}>\n{js}\n</script>"
        if re.search(r"\bdefer\b", m[1], re.I):
            tail.append(tag)                                # a deferred script runs after the page is read
            return ""
        return tag

    tail: list[str] = []
    html = re.sub(r"<link\b[^>]*>", link, html, flags=re.I)
    html = re.sub(r"<script\b([^>]*)>.*?</script>", script, html, flags=re.I | re.S)
    if "</head>" not in html or "</body>" not in html:
        raise LabelError(f"{page.name} has no </head> or </body>")
    data = json.dumps(packet, ensure_ascii=False).replace("<", "\\u003c")        # a chat can't close the script tag
    html = html.replace("</head>", f'<script type="application/json" id="packet">{data}</script>\n</head>', 1)
    head, _, end = html.rpartition("</body>")
    return head + "\n".join(tail) + "</body>" + end


def build(folder: Path, rubric_path: Path, out: Path, *, overlap: Optional[int] = None,
          page: Path = REPO_ROOT / "site" / "score.html") -> dict:
    """Write one packet. Returns what was written, for the summary. Never overwrites an existing packet."""
    if out.exists():
        raise LabelError(f"{out} already exists; choose another OUT")
    if not folder.is_dir():
        raise LabelError(f"no folder {folder}")
    if not rubric_path.is_file():
        raise LabelError(f"no rubric {rubric_path}")
    rubric = load_rubric(rubric_path)
    if not rubric.has_domains:
        raise LabelError(f"rubric {rubric.version} has no domains; use rubrics/v1.yaml")
    chats, skipped = collect(folder, rubric)
    order = assign(chats, overlap)
    taken: set[str] = set()
    for c in chats:
        c.id = _random_id("C", taken)
    chats.sort(key=lambda c: c.id)                          # random, so the file's order says nothing
    qs = questions(rubric)
    header = [*FIXED, *(q["id"] for q in qs), "note"]
    packet = {
        "schema_version": 1, "packet_id": _random_id("P", taken), "rubric_hash": f"{rubric.version}-{rubric.labels_hash()}",
        "scorers": list(SCORERS), "assignment": {who: [c.id for c in cs] for who, cs in order.items()}, "csv_header": header,
        "instructions": rubric.domain_instructions, "length_guides": {str(k): v for k, v in rubric.length_guides.items()},
        "credit": rubric.credit, "questions": qs, "chats": [_chat_json(rubric, c) for c in chats],
    }
    html = bundle_page(page, packet) if page.is_file() else None       # before anything is written
    by_id = {c["id"]: c for c in packet["chats"]}

    def row(cid: str, scorer: str, answers: dict[str, str]) -> dict:
        return {"packet_id": packet["packet_id"], "rubric_hash": packet["rubric_hash"], "scorer": scorer, "chat_id": cid, "note": "",
                **{q["id"]: answers.get(q["id"], "") if q["id"] in by_id[cid]["questions"] else "-" for q in qs}}

    send, private = out / "send", out / "private"
    for d in (send, private, out / "returned"):
        d.mkdir(parents=True)
    (send / "packet.json").write_text(json.dumps(packet, indent=1, ensure_ascii=False), encoding="utf-8")
    if html is not None:
        (send / "score.html").write_text(html, encoding="utf-8")
    for who in SCORERS:
        (send / f"packet_{who}.md").write_text(_markdown(packet, who), encoding="utf-8")
        _write_csv(send / f"labels_{who}.csv", header, [row(i, who, {}) for i in packet["assignment"][who]])
    _write_csv(private / "blind_key.csv", ["chat_id", "transcript_id", "run_id", "run_folder", "agent_name", "scenario_id", "situation", "referral_expectation"], [
        {"chat_id": c.id, "transcript_id": c.t.transcript_id, "run_id": c.t.run_id, "run_folder": str(c.folder), "agent_name": c.t.agent_name,
         "scenario_id": c.t.scenario_id, "situation": c.t.situation,
         "referral_expectation": c.persona.referral.expectation if c.persona else "required" if SITUATION_CODE[c.t.situation].startswith("S") else "not_required"}
        for c in chats])
    judged = [c for c in chats if c.judged]
    if judged:
        _write_csv(private / "judge_labels.csv", header, [row(c.id, "judge", judge_answers(c.judged)) for c in judged])
        (private / "judge_settings.json").write_text(json.dumps(judge_settings([c.judged for c in judged]), indent=1), encoding="utf-8")
    (private / "rubric.yaml").write_text(rubric_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {"packet_id": packet["packet_id"], "chats": len(chats), "skipped": skipped, "judged": len(judged), "page": html is not None,
            "overlap": len(set(packet["assignment"]["A"]) & set(packet["assignment"]["B"])),
            "names_itself": [c.id for c in chats if any(APP_NAMES.search(x.text) for x in c.t.turns if x.speaker == "agent")]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a blind scoring packet from saved chats.")
    ap.add_argument("--from", dest="source", help="folder with one or more runs (default: the latest run)")
    ap.add_argument("--out", help="packet name (default: packet-<time>)")
    ap.add_argument("--overlap", type=int, help="how many chats everyone scores (default: all)")
    ap.add_argument("--rubric", default="rubrics/v1.yaml")
    ap.add_argument("--results", default="results")
    a = ap.parse_args(argv)
    res = REPO_ROOT / a.results
    name = a.out or f"packet-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise LabelError(f"OUT '{name}' may only have letters, digits, '.', '_' and '-'")
        if a.source:
            folder = REPO_ROOT / Path(a.source).expanduser()
        elif (res / "LATEST").exists():
            folder = res / (res / "LATEST").read_text().strip()
        else:
            raise LabelError(f"no {res / 'LATEST'}; pass FROM=<folder>")
        out = res / "labeling" / name
        r = build(folder, REPO_ROOT / a.rubric, out, overlap=a.overlap)
    except LabelError as e:
        print(f"no packet made: {e}", file=sys.stderr)
        return 1
    for s in r["skipped"]:
        print(f"  skipped  {s}")
    print(f"packet {r['packet_id']}: {r['chats']} chats, {r['overlap']} scored by everyone, {r['judged']} with judge scores")
    if r["names_itself"]:
        print(f"  WARNING: a reply names an AI app or company, so these chats may not be blind: {', '.join(r['names_itself'])}")
    print("  send/score.html is ready" if r["page"] else "  site/score.html doesn't exist yet (F4): scorers use the CSV sheet and packet_<letter>.md")
    print(f"wrote {out}\n  send only send/, privately. Never send private/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
