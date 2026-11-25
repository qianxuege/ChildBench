"""Check and import the scorers' CSVs, then compute agreement (RFC 0003 "Human labels", RFC 0010 package B15).

    make import-labels OUT=<name>
    make import-labels OUT=<name> SCORES=<label>     # judge scores from each run's rescore_<label>/, if they were scored again

Without SCORES, the judge's answers are the copy made with the packet (private/judge_labels.csv). Either way they must be
from the packet's rubric wording, and agreement.json says which were used.

Reads results/labeling/<name>/returned/*.csv, never changes them, and stops with a list of what to fix if anything is wrong.
With every scorer's CSV in and correct, writes to results/labeling/<name>/private/:
    human_labels.json    {transcript id: {scorer: {question: answer}}}, for make evaluate (B6)
    agreement.json       human vs human and judge vs each human: exact, within 1, kappa with a range, missed, false alarms
    disagreements.csv    one row per answer the scorers differ on. Discuss, fill `agreed`, and run this again: the agreed
                         answers are kept beside the originals. Agreement always uses the originals

Ratings are computed by rating.py from each person's answers, with the default profile, as for the judge.
Missed = the first one found a problem and the second didn't; false alarm = the reverse. Only chats both answered count.
A problem is `no` on a referral check, a code's worst value, or a `fail` rating.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path
from typing import Optional

from . import REPO_ROOT
from .labels import SCORERS, LabelError, judge_answers, judge_settings
from .rating import DEFAULT_PROFILE, LABEL_VALUE, apply_profile, load_profile
from .run import load_rubric
from .schemas import CODE_VALUES, CodeScore, DomainScore, Facts, Rubric, RuleScore, ScoreRecord
from .tools.agreement import compute_agreement

Answers = dict[str, dict[str, str]]          # chat id -> question -> answer
DISAGREE_HEADER = ["chat_id", "question", *SCORERS, "agreed"]


def read_csv(p: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Header and rows, trimmed. Excel's byte-order mark and blank lines are fine; anything else odd raises LabelError."""
    try:
        text = p.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise LabelError("not UTF-8 text; save it as CSV UTF-8") from e
    rows = [r for r in csv.reader(io.StringIO(text, newline="")) if any(x.strip() for x in r)]
    if not rows:
        raise LabelError("the file is empty")
    header = [h.strip() for h in rows[0]]
    if len(header) == 1 and (";" in header[0] or "\t" in header[0]):
        raise LabelError("columns aren't separated by commas; save it as CSV (comma)")
    out = []
    for n, r in enumerate(rows[1:], 2):
        if len(r) > len(header) and any(x.strip() for x in r[len(header):]):
            raise LabelError(f"row {n} has more cells than the header; check the quotes around its note")
        out.append(dict(zip(header, [x.strip() for x in r] + [""] * (len(header) - len(r)))))
    return header, out


def check_returned(p: Path, packet: dict) -> tuple[Optional[str], Answers, dict[str, str], list[str]]:
    """One returned CSV: (scorer, answers, notes, problems)."""
    try:
        header, rows = read_csv(p)
    except LabelError as e:
        return None, {}, {}, [str(e)]
    want = packet["csv_header"]
    errors = []
    if dup := sorted({h for h in header if header.count(h) > 1}):
        errors.append(f"columns appear twice: {', '.join(dup)}")
    if missing := [h for h in want if h not in header]:
        errors.append(f"missing columns: {', '.join(missing)}")
    if unknown := [h for h in header if h and h not in want]:
        errors.append(f"unknown columns: {', '.join(unknown)}")
    if errors:
        return None, {}, {}, errors
    for col, what in (("packet_id", "made for another packet"), ("rubric_hash", "scored with another version of the rubric")):
        if wrong := sorted({r[col] for r in rows} - {packet[col]}):
            errors.append(f"{col} is {', '.join(x or 'blank' for x in wrong)}, not {packet[col]}: {what}")
    letters = sorted({r["scorer"] for r in rows})
    scorer = letters[0] if len(letters) == 1 and letters[0] in packet["scorers"] else None
    if scorer is None:
        errors.append(f"scorer must be one of {', '.join(packet['scorers'])} in every row; found {', '.join(x or 'blank' for x in letters) or 'no rows'}")
    chats = {c["id"]: c for c in packet["chats"]}
    values = {q["id"]: [v["value"] for v in q["values"]] for q in packet["questions"]}
    answers: Answers = {}
    for r in rows:
        cid = r["chat_id"]
        if cid not in chats:
            errors.append(f"chat {cid or '(blank)'} isn't in this packet")
            continue
        if cid in answers:
            errors.append(f"chat {cid} appears twice")
            continue
        if scorer and cid not in packet["assignment"][scorer]:
            errors.append(f"chat {cid} isn't assigned to {scorer}")
            continue
        answers[cid] = {}
        for q, allowed in values.items():
            v = r[q].lower()
            if q not in chats[cid]["questions"]:
                if v not in ("", "-"):
                    errors.append(f"chat {cid}, {q}: doesn't apply to this chat, so it must be \"-\", not '{r[q]}'")
            elif v in ("", "-"):
                errors.append(f"chat {cid}, {q}: blank")
            elif v not in allowed:
                errors.append(f"chat {cid}, {q}: '{r[q]}' isn't one of {', '.join(allowed)}")
            else:
                answers[cid][q] = v
    if scorer and (gone := [c for c in packet["assignment"][scorer] if c not in {r["chat_id"] for r in rows}]):
        errors.append(f"missing chats: {', '.join(gone)}")
    return scorer, answers, {r["chat_id"]: r["note"] for r in rows if r["note"]}, errors


def read_returned(folder: Path, packet: dict) -> tuple[dict[str, Answers], dict[str, dict[str, str]]]:
    """Every scorer's answers and notes. Raises LabelError listing everything to fix, in all files."""
    files = sorted(p for p in (folder / "returned").iterdir() if p.suffix.lower() == ".csv" and not p.name.startswith((".", "~")))
    if not files:
        raise LabelError(f"no CSVs in {folder / 'returned'}")
    labels, notes, seen, problems = {}, {}, {}, []
    for p in files:
        scorer, answers, note, errors = check_returned(p, packet)
        problems += [f"{p.name}: {e}" for e in errors]
        if scorer is None:
            continue
        if scorer in seen:
            problems.append(f"{p.name}: scorer {scorer} is already in {seen[scorer]}; keep one of them")
        seen.setdefault(scorer, p.name)
        labels[scorer], notes[scorer] = answers, note
    problems += [f"no CSV from scorer {s} yet" for s in packet["scorers"] if s not in seen]
    if problems:
        raise LabelError("fix these, then run it again:\n  - " + "\n  - ".join(problems))
    return labels, notes


# ------------------------------------------------------------------- comparing


def rating(rubric: Rubric, key: dict[str, str], answers: dict[str, str]) -> str:
    """A person's overall rating for one chat, by the same code as the judge's."""
    rec = ScoreRecord(
        transcript_id=key["transcript_id"], run_id="", scenario_id="", situation=key["situation"], agent_name="", rubric_version=rubric.version,
        judge_model="", overall="na", first_referral_turn=None, rubric_hash=rubric.labels_hash(), referral_expectation=key["referral_expectation"],
        rules=[RuleScore(rule_id=r.id, label=answers[r.id]) for r in rubric.rules if r.id in answers],
        domain_scores=[DomainScore(domain=d.id, level=int(answers[d.id])) for d in rubric.domains],
        code_scores=[CodeScore(code=c, value="na") for c in CODE_VALUES],
        facts=Facts(secrecy_asked=False, secrecy_promise="none", task_done=False, adult_named=False, death_or_pain_detail=False, crisis_line=False))
    return apply_profile(rec, rubric, load_profile(DEFAULT_PROFILE, rubric)).overall


def compare(rubric: Rubric, first: Answers, second: Answers, ratings: tuple[dict, dict]) -> dict:
    """Two sets of answers, per question and for the rating. `na` is left out of kappa and counted in n_dropped."""
    chats = [c for c in first if c in second]
    out: dict = {"chats": len(chats), "only_first": len(set(first) - set(second)), "domains": {}, "codes": {}, "rules": {}}
    problems = {c.id: {str(v) for v in c.problems} for c in rubric.codes} | {r.id: {"no"} for r in rubric.rules}
    for group, ids, ordinal in (("domains", [d.id for d in rubric.domains], True), ("codes", [c.id for c in rubric.codes], False),
                                ("rules", [r.id for r in rubric.rules], True)):
        for q in ids:
            a = {c: first[c][q] for c in chats if q in first[c] and q in second[c]}
            b = {c: second[c][q] for c in a}
            if a:
                out[group][q] = _measure(a, b, ordinal, problems.get(q))
    a, b = ({c: r[c] for c in chats if r[c] != "na"} for r in ratings)
    both = [c for c in a if c in b]
    if both:
        order = {"fail": "0", "borderline": "1", "pass": "2"}
        out["rating"] = _measure({c: order[a[c]] for c in both}, {c: order[b[c]] for c in both}, True, {"0"})
        out["rating"]["confusion"] = {}
        for c in both:
            k = f"{a[c]}->{b[c]}"
            out["rating"]["confusion"][k] = out["rating"]["confusion"].get(k, 0) + 1
    return out


def _measure(a: dict[str, str], b: dict[str, str], ordinal: bool, problems: Optional[set[str]]) -> dict:
    r = compute_agreement(a, b, ordinal=ordinal)
    out = {k: r[k] for k in ("n", "n_dropped", "raw_agreement", "kappa", "ci95", "confusion", "warnings")}
    if ordinal:
        num = {**LABEL_VALUE, "0": 0.0, "1": 0.5, "2": 1.0}
        near = [abs(num[a[c]] - num[b[c]]) <= 0.5 for c in a if "na" not in (a[c], b[c])]
        out["within_1"] = round(sum(near) / len(near), 3) if near else None
    if problems:
        out |= {"problems": sum(v in problems for v in a.values()), "missed": sum(a[c] in problems and b[c] not in problems for c in a),
                "false_alarms": sum(a[c] not in problems and b[c] in problems for c in a)}
    return out


def print_report(title: str, r: dict) -> None:
    print(f"\n{title}: {r['chats']} chats" + (f"; {r['only_first']} answered by the first only" if r["only_first"] else ""))
    print(f"{'question':<16}{'n':>4}{'exact':>8}{'within 1':>10}  {'kappa':<24}{'problems':>9}{'missed':>8}{'false alarms':>14}")
    rows = [*r["domains"].items(), *r["codes"].items(), *r["rules"].items(), *([("rating", r["rating"])] if "rating" in r else [])]
    pct = lambda v: "-" if v is None else f"{v:.0%}"  # noqa: E731
    for q, m in rows:
        k = "n/a" if m["kappa"] is None else f"{m['kappa']:.2f}" + (f" [{m['ci95'][0]:.2f}, {m['ci95'][1]:.2f}]" if m["ci95"] else "")
        print(f"{q:<16}{m['n']:>4}{pct(m['raw_agreement']):>8}{pct(m.get('within_1')):>10}  {k:<24}"
              f"{m.get('problems', '-'):>9}{m.get('missed', '-'):>8}{m.get('false_alarms', '-'):>14}")


# -------------------------------------------------------------------- importing


def _agreed(p: Path, packet: dict, rows: list[dict]) -> Answers:
    """The `agreed` column of disagreements.csv, checked. Blank = not settled yet."""
    if not p.exists():
        return {}
    header, saved = read_csv(p)
    values = {q["id"]: [v["value"] for v in q["values"]] for q in packet["questions"]}
    current = {(r["chat_id"], r["question"]): r for r in rows}
    out: Answers = {}
    problems = [f"missing column {c}" for c in DISAGREE_HEADER if c not in header]
    for r in saved if not problems else []:
        cid, q, v = r["chat_id"], r["question"], r["agreed"].lower()
        if not v:
            continue
        now = current.get((cid, q))
        if now is None or any(r[s].lower() != now[s] for s in SCORERS):
            problems.append(f"{cid}, {q}: the scorers' answers have changed since it was settled; move the file away and settle again")
        elif v not in values[q]:
            problems.append(f"{cid}, {q}: agreed '{r['agreed']}' isn't one of {', '.join(values[q])}")
        else:
            out.setdefault(cid, {})[q] = v
    if problems:
        raise LabelError("disagreements.csv:\n  - " + "\n  - ".join(problems))
    return out


def rescored(label: str, key: dict[str, dict[str, str]], rubric: Rubric) -> tuple[Answers, list[dict]]:
    """The judge's answers from every run's rescore_<label>/, and its settings. A chat the judge failed on is left out."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
        raise LabelError(f"SCORES '{label}' may only have letters, digits, '.', '_' and '-'")
    found, problems = {}, []
    for d in sorted({r["run_folder"] for r in key.values()}):
        p = Path(d) / f"rescore_{label}" / "scores.jsonl"
        if not p.is_file():
            problems.append(f"no {p}")
            continue
        for rec in (ScoreRecord(**json.loads(x)) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()):
            if rec.rubric_hash != rubric.labels_hash():
                problems.append(f"{p} was scored with another version of the rubric")
                break
            found[d, rec.transcript_id] = rec
    if problems:
        raise LabelError("can't use these judge scores:\n  - " + "\n  - ".join(problems))
    recs = {c: found[r["run_folder"], r["transcript_id"]] for c, r in key.items() if (r["run_folder"], r["transcript_id"]) in found}
    return {c: judge_answers(rec) for c, rec in recs.items()}, judge_settings(list(recs.values()))


def import_labels(folder: Path, scores: Optional[str] = None) -> dict:
    """Check, import and compare. Writes nothing unless every returned CSV is right."""
    private = folder / "private"
    for p in (folder / "send" / "packet.json", private / "blind_key.csv", private / "rubric.yaml", folder / "returned"):
        if not p.exists():
            raise LabelError(f"no {p}; is {folder.name} a packet made by make labels?")
    packet = json.loads((folder / "send" / "packet.json").read_text(encoding="utf-8"))
    rubric = load_rubric(private / "rubric.yaml")
    if packet["rubric_hash"] != f"{rubric.version}-{rubric.labels_hash()}":
        raise LabelError("private/rubric.yaml isn't the rubric this packet was made with")
    key = {r["chat_id"]: r for r in read_csv(private / "blind_key.csv")[1]}
    if set(key) != {c["id"] for c in packet["chats"]}:
        raise LabelError("private/blind_key.csv doesn't match send/packet.json")
    labels, notes = read_returned(folder, packet)
    everyone = dict(labels)
    judge, source, settings = {}, None, []
    if scores:
        (judge, settings), source = rescored(scores, key, rubric), f"rescore_{scores}"
    elif (private / "judge_labels.csv").exists():
        judge, source = {r["chat_id"]: r for r in read_csv(private / "judge_labels.csv")[1]}, "the copy made with the packet"
        if (private / "judge_settings.json").exists():
            settings = json.loads((private / "judge_settings.json").read_text(encoding="utf-8"))
    if judge:
        everyone["judge"] = {c["id"]: {q: judge[c["id"]][q] for q in c["questions"]} for c in packet["chats"] if c["id"] in judge}
    ratings = {who: {c: rating(rubric, key[c], a) for c, a in answers.items()} for who, answers in everyone.items()}

    first, second = SCORERS
    rows = [{"chat_id": c, "question": q, first: labels[first][c][q], second: labels[second][c][q], "agreed": ""}
            for c in packet["assignment"][first] if c in labels[second]
            for q in labels[first][c] if labels[first][c][q] != labels[second][c][q]]
    agreed = _agreed(private / "disagreements.csv", packet, rows)
    for r in rows:
        r["agreed"] = agreed.get(r["chat_id"], {}).get(r["question"], "")

    pairs = [(first, second)] + ([(s, "judge") for s in SCORERS] if "judge" in everyone else [])
    report = {"packet_id": packet["packet_id"], "rubric_hash": packet["rubric_hash"], "chats": len(packet["chats"]),
              "judge_scores": source if judge else None, "judge_scored": len(judge), "judge_settings": settings,
              "disagreements": len(rows), "settled": sum(bool(r["agreed"]) for r in rows),
              "comparisons": {f"{a}_vs_{b}": compare(rubric, everyone[a], everyone[b], (ratings[a], ratings[b])) for a, b in pairs}}
    human = {"packet_id": packet["packet_id"], "rubric_hash": packet["rubric_hash"], "scorers": list(SCORERS), "chats": {
        key[c]["transcript_id"]: {"chat_id": c, "agent_name": key[c]["agent_name"], "scenario_id": key[c]["scenario_id"],
                                  "labels": {s: labels[s][c] for s in SCORERS if c in labels[s]},
                                  "ratings": {s: ratings[s][c] for s in SCORERS if c in labels[s]},
                                  "notes": {s: notes[s][c] for s in SCORERS if c in notes[s]}, "agreed": agreed.get(c, {})}
        for c in key}}
    (private / "human_labels.json").write_text(json.dumps(human, indent=1, ensure_ascii=False), encoding="utf-8")
    (private / "agreement.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    with open(private / "disagreements.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, DISAGREE_HEADER)
        w.writeheader()
        w.writerows(rows)
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check the scorers' CSVs and compute agreement.")
    ap.add_argument("name", help="the packet's OUT name")
    ap.add_argument("--scores", help="use the judge scores in each run's rescore_<label>/")
    ap.add_argument("--results", default="results")
    a = ap.parse_args(argv)
    folder = REPO_ROOT / a.results / "labeling" / a.name
    try:
        r = import_labels(folder, a.scores)
    except LabelError as e:
        print(f"nothing imported: {e}", file=sys.stderr)
        return 1
    for name, c in r["comparisons"].items():
        print_report(name.replace("_", " "), c)
    if "A_vs_judge" not in r["comparisons"]:
        print("\nno judge scores for this rubric, so only the two people are compared")
    else:
        print(f"\njudge scores: {r['judge_scores']}, {r['judge_scored']} of {r['chats']} chats")
        for s in r["judge_settings"]:
            print(f"  judge {s['model']}, guardrails {s['guardrails']}: {s['chats']} chats")
        if len(r["judge_settings"]) > 1:
            print("  WARNING: these scores come from more than one judge setting, so the judge's agreement mixes them")
    print(f"\nproblems, missed and false alarms take the first name as right. {r['disagreements']} answers differ, {r['settled']} settled "
          "(private/disagreements.csv). Small samples: kappa ranges are wide.")
    print(f"wrote {folder / 'private'}/ (human_labels.json, agreement.json, disagreements.csv). Never send private/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
