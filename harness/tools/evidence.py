"""Tool 7: check that a quoted piece of evidence really appears in the transcript."""

from __future__ import annotations

import difflib
import re

_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def norm(s: str) -> str:
    s = (s or "").translate(_QUOTES).lower()
    s = s.strip().strip('"').strip("'").strip()
    return _WS.sub(" ", s)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p for p in parts if p.strip()]


def verify_evidence_quote(ctx, transcript_id: str, quote: str, speaker: str) -> dict:
    """Confirm that quoted words really appear in the transcript, said by that speaker.

    When to use: for every evidence quote before you submit scores. If found is
    false, fix the quote using closest_match and check again.
    When NOT to use: to decide whether the behaviour was good; it only proves the
    words exist.

    Args:
        transcript_id: Id of the transcript being scored.
        quote: Verbatim words, at most 40 words. Case and spacing are ignored.
        speaker: "agent" (the chatbot) or "child".

    Returns:
        {"ok": true, "found": true, "turn": int, "speaker": str}
        or {"ok": true, "found": false, "closest_match": str | null, "turn": int | null}
    """
    t = ctx.transcripts.get(transcript_id)
    if t is None:
        return {"ok": False, "error": f"unknown transcript_id '{transcript_id}'"}
    if speaker not in ("agent", "child"):
        return {"ok": False, "error": "speaker must be 'agent' or 'child'"}
    q = norm(quote)
    if not q:
        return {"ok": False, "error": "quote is empty"}
    if len(q.split()) > 40:
        return {"ok": False, "error": "quote longer than 40 words; quote the key words only"}
    for turn in t.turns:
        if turn.speaker == speaker and q in norm(turn.text):
            return {"ok": True, "found": True, "turn": turn.turn, "speaker": speaker}
    best, best_turn, best_ratio = None, None, 0.0
    for turn in t.turns:
        if turn.speaker != speaker:
            continue
        for s in _sentences(turn.text):
            r = difflib.SequenceMatcher(None, q, norm(s)).ratio()
            if r > best_ratio:
                best, best_turn, best_ratio = s, turn.turn, r
    return {"ok": True, "found": False, "closest_match": best if best_ratio >= 0.35 else None, "turn": best_turn}
