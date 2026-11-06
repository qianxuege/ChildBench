"""Agreement per rule between two label sheets (human vs human, or judge vs human).

    python -m harness.validate_judge results/<run>/labeling/judge_labels.csv labels_alex.csv
    python -m harness.validate_judge labels_alex.csv labels_sam.csv --json agreement.json

Rows are matched on transcript_id (the blind id). "-" and "na" are dropped.
Uses weighted kappa (no < partial < yes) by default, as in RFC 0003.
"""

from __future__ import annotations

import argparse
import csv
import json

from .tools.agreement import compute_agreement

SKIP = {"transcript_id", "situation", "first_referral_turn", "overall", "note", "labeler_id"}


def read(path: str) -> dict[str, dict[str, str]]:
    with open(path, newline="") as f:
        return {r["transcript_id"]: r for r in csv.DictReader(f)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--nominal", action="store_true", help="unweighted kappa")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    A, B = read(args.a), read(args.b)
    rules = [c for c in next(iter(A.values())).keys() if c not in SKIP]
    out = {}
    print(f"{'rule':<8}{'n':>5}{'raw':>8}{'kappa':>8}  95% range")
    for rid in rules:
        la = {k: v[rid].strip().lower() for k, v in A.items() if v.get(rid, "").strip() not in ("", "-")}
        lb = {k: v[rid].strip().lower() for k, v in B.items() if v.get(rid, "").strip() not in ("", "-")}
        r = compute_agreement(la, lb, ordinal=not args.nominal)
        if not r["n"]:
            continue
        out[rid] = r
        ci = f"[{r['ci95'][0]}, {r['ci95'][1]}]" if r["ci95"] else "-"
        print(f"{rid:<8}{r['n']:>5}{r['raw_agreement']:>8}{str(r['kappa']):>8}  {ci}  {' '.join(r['warnings'])}")
    ov = compute_agreement({k: v["overall"] for k, v in A.items() if v.get("overall")},
                           {k: v["overall"] for k, v in B.items() if v.get("overall")})
    out["overall"] = ov
    print(f"{'overall':<8}{ov['n']:>5}{str(ov['raw_agreement']):>8}{str(ov['kappa']):>8}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(out, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
