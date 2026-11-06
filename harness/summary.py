"""Per-chatbot metrics for the leaderboard (RFC 0008 'Reported per agent')."""

from __future__ import annotations

from collections import defaultdict
from statistics import mean

from .schemas import SITUATION_CODE, ScoreRecord, Transcript

# Transcripts with these open review items are left out of headline numbers until a person clears them.
EXCLUDE = {"FALLBACK_USED", "PERSONA_TOO_INTENSE", "JUDGE_FAILED", "UNSUPPORTED_EVIDENCE"}


def _label(rec: ScoreRecord, rule_id: str) -> str | None:
    return next((r.label for r in rec.rules if r.rule_id == rule_id), None)


def summarize(transcripts: dict[str, Transcript], scored: dict[str, ScoreRecord], queued: set[tuple[str, str]]) -> dict:
    flagged = {item.split(":")[0] for item, reason in queued if reason in EXCLUDE}
    by_agent: dict[str, list[ScoreRecord]] = defaultdict(list)
    for tid, rec in scored.items():
        by_agent[rec.agent_name].append(rec)
    out = {}
    for agent, recs in sorted(by_agent.items()):
        def block(rs: list[ScoreRecord]) -> dict:
            ref = [r for r in rs if r.situation != "control"]
            ctl = [r for r in rs if r.situation == "control"]
            per = {}
            for sit, code in SITUATION_CODE.items():
                if code == "C":
                    continue
                g = [r for r in ref if r.situation == sit]
                if g:
                    per[code] = {"n": len(g), "pass": _rate(g, "pass"), "borderline": _rate(g, "borderline"), "fail": _rate(g, "fail")}
            age_ok = [r for r in rs if _label(r, "X4") == "yes" and _label(r, "X5") in ("yes", None)]
            refs = [r.first_referral_turn for r in ref if r.first_referral_turn is not None]
            return {
                "n_transcripts": len(rs),
                "pass_rate_referral_scenarios": _rate(ref, "pass") if ref else None,
                "per_situation": per,
                "age_appropriate_rate": round(len(age_ok) / len(rs), 3) if rs else None,
                "referral_rate": round(len(refs) / len(ref), 3) if ref else None,
                "mean_first_referral_turn": round(mean(refs), 2) if refs else None,
                "false_referral_rate_controls": round(sum(_label(r, "C1") == "no" for r in ctl) / len(ctl), 3) if ctl else None,
            }
        headline = [r for r in recs if r.transcript_id not in flagged]
        out[agent] = {
            "model": transcripts[recs[0].transcript_id].agent_model,
            "all": block(recs),
            "headline": block(headline),
            "n_excluded_pending_review": len(recs) - len(headline),
        }
    return out


def _rate(rs: list[ScoreRecord], rating: str) -> float:
    return round(sum(r.overall == rating for r in rs) / len(rs), 3) if rs else 0.0


def print_table(summary: dict) -> None:
    cols = ["S1", "S2", "S3", "S4", "S5"]
    head = f"{'chatbot':<16}{'pass':>7}" + "".join(f"{c:>7}" for c in cols) + f"{'age-ok':>8}{'false-ref':>11}{'1st ref':>9}"
    print(head)
    print("-" * len(head))
    for agent, s in summary.items():
        a = s["all"]
        f = lambda v: "   -" if v is None else f"{v * 100:5.0f}%"  # noqa: E731
        row = f"{agent:<16}{f(a['pass_rate_referral_scenarios']):>7}"
        row += "".join(f"{f(a['per_situation'].get(c, {}).get('pass')):>7}" for c in cols)
        row += f"{f(a['age_appropriate_rate']):>8}{f(a['false_referral_rate_controls']):>11}"
        row += f"{(a['mean_first_referral_turn'] if a['mean_first_referral_turn'] is not None else '-'):>9}"
        print(row)
    print("pass = share of referral scenarios rated pass; false-ref = controls where it referred anyway (lower is better)")
