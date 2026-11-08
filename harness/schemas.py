"""Data records shared by every part of the harness.

Everything that crosses a boundary (file on disk, LLM output, tool result) is
validated against one of these models, so malformed scenarios or judge output
fail loudly instead of silently producing scores (RFC 0009).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Situation = Literal[
    "beyond_ability",
    "personal_data",
    "over_attachment",
    "distressing_disclosure",
    "unsafe_request",
    "control",
]
SITUATION_CODE = {
    "beyond_ability": "S1",
    "personal_data": "S2",
    "over_attachment": "S3",
    "distressing_disclosure": "S4",
    "unsafe_request": "S5",
    "control": "C",
}
ContentLevel = Literal["mild", "moderate", "severe"]
Label = Literal["yes", "partial", "no", "na"]
RuleLevel = Literal["MUST", "MUST_NOT", "SHOULD"]
Confidence = Literal["low", "medium", "high"]
Rating = Literal["pass", "borderline", "fail"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- scenarios


class Beat(BaseModel):
    """One step of the disclosure plan: what the child brings up on a turn."""

    turn: int = Field(ge=1, le=20)
    plan: str = Field(description="What the child does this turn, in plain words (not the exact line).")
    fallback_line: str = Field(description="Hand-written line used if the generator fails 3 times.")


class Review(BaseModel):
    author: str
    reviewers: list[str] = Field(default_factory=list)
    ran_approved: bool = False

    @property
    def approved(self) -> bool:
        # Guardrail (RFC 0006): author + at least one second reviewer before a run.
        return len(self.reviewers) >= 1


class Persona(BaseModel):
    id: str
    situation: Situation
    child_age: int = Field(ge=2, le=17)
    age_group: Optional[str] = Field(default=None, description="e.g. '3-5'; bands pending Ran.")
    child_name: str
    context: str = Field(default="casual chat", description="Everyday setting: homework help, bedtime chat...")
    speech_style: str = "short whole sentences, simple words, small grammar mistakes for the age, spoken"
    beats: list[Beat]
    stop_condition: str = "agent refers to a trusted adult OR max turns"
    max_turns: int = Field(default=6, ge=1, le=12)
    content_level: ContentLevel = "mild"
    allowed_words: list[str] = Field(default_factory=list, description="Topic words lint should not penalize.")
    pushback_once: bool = Field(default=True, description="Child resists the first referral once, as real kids do.")
    expected_good: str = ""
    example_bad: str = ""
    ref: str = ""
    review: Review
    held_out: bool = False

    @field_validator("beats")
    @classmethod
    def _beats_sorted(cls, v: list[Beat]) -> list[Beat]:
        turns = [b.turn for b in v]
        if turns != sorted(turns) or len(set(turns)) != len(turns):
            raise ValueError("beats must have unique, increasing turn numbers")
        return v

    @model_validator(mode="after")
    def _limits(self) -> "Persona":
        if self.content_level == "severe":
            raise ValueError("severe content is never allowed (RFC 0006)")
        if self.beats and self.beats[-1].turn > self.max_turns:
            raise ValueError("a beat is scheduled after max_turns")
        return self

    @property
    def code(self) -> str:
        return SITUATION_CODE[self.situation]


# ------------------------------------------------------------------- rubric


class RubricRule(BaseModel):
    id: str
    text: str
    level: RuleLevel
    applies_to: list[str] = Field(description="Situation codes (S1..S5, C) or 'all_referral'.")
    source: str
    judge_hint: str = ""


class Rubric(BaseModel):
    version: str
    rules: list[RubricRule]

    def for_situation(self, code: str) -> list[RubricRule]:
        out = []
        for r in self.rules:
            if code in r.applies_to or ("all_referral" in r.applies_to and code != "C"):
                out.append(r)
        return out


# --------------------------------------------------------------- transcripts


class Turn(BaseModel):
    turn: int
    speaker: Literal["child", "agent"]
    text: str
    used_fallback: bool = False
    attempts: int = 1


class Transcript(BaseModel):
    transcript_id: str
    run_id: str
    scenario_id: str
    situation: Situation
    child_age: int
    agent_name: str
    agent_model: str
    seed: int
    turns: list[Turn]
    ended_because: str
    created_at: str = Field(default_factory=now_iso)

    def agent_turns(self) -> list[Turn]:
        return [t for t in self.turns if t.speaker == "agent"]


# -------------------------------------------------------------------- scores


class RuleScore(BaseModel):
    rule_id: str
    label: Label
    evidence_quote: Optional[str] = None
    evidence_speaker: Optional[Literal["child", "agent"]] = None
    evidence_turn: Optional[int] = None
    confidence: Confidence = "medium"
    supported: bool = True
    source: Literal["judge", "deterministic"] = "judge"
    note: str = ""


class ScoreRecord(BaseModel):
    transcript_id: str
    run_id: str
    scenario_id: str
    situation: Situation
    agent_name: str
    rubric_version: str
    judge_model: str
    rules: list[RuleScore]
    overall: Rating
    first_referral_turn: Optional[int]
    provisional: bool = Field(default=False, description="True if any label is unsupported or low confidence.")
    automatic_checks: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)


class ReviewItem(BaseModel):
    item_id: str
    run_id: str
    reason_code: Literal[
        "FALLBACK_USED",
        "PERSONA_TOO_INTENSE",
        "REVIEWER_UNSURE",
        "CHECK_DISAGREE",
        "PERSONA_DRIFT",
        "AUDIT_SAMPLE",
        "LOW_CONFIDENCE",
        "UNSUPPORTED_EVIDENCE",
        "JUDGE_FAILED",
    ]
    details: str = ""
    created_at: str = Field(default_factory=now_iso)


class GuardrailEvent(BaseModel):
    run_id: str
    item_id: str
    check: str
    result: Literal["PASS", "FAIL", "APPROVE", "REJECT", "UNSURE", "SHADOW_FAIL"]
    reason: str = ""
    enforced: bool = True
    created_at: str = Field(default_factory=now_iso)
