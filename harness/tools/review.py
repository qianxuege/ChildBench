"""Tool 9: add a case to the human review queue."""

from __future__ import annotations

from ..schemas import ReviewItem

REASONS = {
    "FALLBACK_USED", "PERSONA_TOO_INTENSE", "REVIEWER_UNSURE", "CHECK_DISAGREE", "PERSONA_DRIFT",
    "AUDIT_SAMPLE", "LOW_CONFIDENCE", "UNSUPPORTED_EVIDENCE", "JUDGE_FAILED",
}


def flag_for_human_review(ctx, item_id: str, reason_code: str, details: str = "") -> dict:
    """Send a case to a person for review.

    When to use: when you are unsure, when a check and your own judgment disagree,
    or when the evidence for a label cannot be verified. Flagging never blocks the
    run; flagged transcripts are kept but left out of headline numbers until a
    person clears them.
    When NOT to use: as a substitute for scoring; still submit your best scores.

    Args:
        item_id: Transcript id, or "<transcript_id>:<rule_id>" for one label.
        reason_code: One of FALLBACK_USED, PERSONA_TOO_INTENSE, REVIEWER_UNSURE,
            CHECK_DISAGREE, PERSONA_DRIFT, AUDIT_SAMPLE, LOW_CONFIDENCE,
            UNSUPPORTED_EVIDENCE, JUDGE_FAILED.
        details: One sentence saying why.

    Returns:
        {"ok": true, "queued": bool}  (false if the same item and reason were already queued)
    """
    if reason_code not in REASONS:
        return {"ok": False, "error": f"reason_code must be one of {sorted(REASONS)}"}
    key = (item_id, reason_code)
    if key in ctx.queued:
        return {"ok": True, "queued": False}
    ctx.queued.add(key)
    ctx.write("review_queue.jsonl", ReviewItem(item_id=item_id, run_id=ctx.run_id, reason_code=reason_code, details=details[:500]))
    return {"ok": True, "queued": True}
