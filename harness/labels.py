"""Blind label sheets for human scoring (RFC 0003).

    python -m harness.labels --run latest

Writes to results/<run_id>/labeling/:
    label_sheet.csv     one row per transcript, rule columns blank; give one copy to each labeler
    labeling_packet.md  the conversations, chatbot names hidden
    judge_labels.csv    the judge's labels in the same format, for harness.validate_judge
    blind_key.csv       blind id -> real transcript id. Do NOT share with labelers (gitignored).
Rule columns that don't apply to a transcript's situation are pre-filled with "-".
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import yaml

from . import REPO_ROOT
from .schemas import SITUATION_CODE, Rubric


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def build(run_dir: Path, rubric: Rubric) -> Path:
    run_id = run_dir.name
    transcripts = _read_jsonl(run_dir / "transcripts.jsonl")
    scores = {s["transcript_id"]: s for s in _read_jsonl(run_dir / "scores.jsonl")}
    out = run_dir / "labeling"
    out.mkdir(exist_ok=True)
    rule_ids = [r.id for r in rubric.rules]
    header = ["transcript_id", "situation", *rule_ids, "first_referral_turn", "overall", "note", "labeler_id"]

    def blind(tid: str) -> str:
        return "L" + hashlib.sha1(f"{run_id}:{tid}".encode()).hexdigest()[:6]

    ordered = sorted(transcripts, key=lambda t: blind(t["transcript_id"]))  # shuffles chatbots together
    with open(out / "label_sheet.csv", "w", newline="") as f, open(out / "judge_labels.csv", "w", newline="") as g, \
            open(out / "blind_key.csv", "w", newline="") as k:
        ws, wj, wk = csv.writer(f), csv.writer(g), csv.writer(k)
        ws.writerow(header)
        wj.writerow(header)
        wk.writerow(["blind_id", "transcript_id", "agent_name"])
        for t in ordered:
            code = SITUATION_CODE[t["situation"]]
            applicable = {r.id for r in rubric.for_situation(code)}
            bid = blind(t["transcript_id"])
            ws.writerow([bid, t["situation"], *["" if r in applicable else "-" for r in rule_ids], "", "", "", ""])
            wk.writerow([bid, t["transcript_id"], t["agent_name"]])
            s = scores.get(t["transcript_id"])
            if s:
                lab = {r["rule_id"]: r["label"] for r in s["rules"]}
                wj.writerow([bid, t["situation"], *[lab.get(r, "-") for r in rule_ids],
                             s["first_referral_turn"] if s["first_referral_turn"] is not None else "none",
                             s["overall"], "", "judge"])
    lines = [f"# Labeling packet · run {run_id}", "",
             "Score each conversation in label_sheet.csv with yes / partial / no / na per rule.",
             "`yes` always means the chatbot did what the rule asks. Chatbot names are hidden on purpose.", ""]
    for t in ordered:
        lines += [f"## {blind(t['transcript_id'])} · {t['situation']} · child age {t['child_age']}", ""]
        for turn in t["turns"]:
            who = "CHILD" if turn["speaker"] == "child" else "CHATBOT"
            lines.append(f"- **[{turn['turn']}] {who}:** {turn['text']}")
        lines.append("")
    (out / "labeling_packet.md").write_text("\n".join(lines))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="latest", help="run id, or 'latest'")
    ap.add_argument("--results", default="results")
    ap.add_argument("--rubric", default="rubrics/v0.yaml")
    a = ap.parse_args(argv)
    res = REPO_ROOT / a.results
    run_id = (res / "LATEST").read_text().strip() if a.run == "latest" else a.run
    rubric = Rubric(**yaml.safe_load((REPO_ROOT / a.rubric).read_text()))
    out = build(res / run_id, rubric)
    print(f"wrote {out.relative_to(REPO_ROOT)}/ (label_sheet.csv, labeling_packet.md, judge_labels.csv, blind_key.csv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
