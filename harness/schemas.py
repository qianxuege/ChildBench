"""Data records shared by every part of the harness.

Everything that crosses a boundary (file on disk, LLM output, tool result) is
validated against one of these models, so malformed scenarios or judge output
fail loudly instead of silently producing scores (RFC 0009).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

Situation = Literal[
    "beyond_ability",
    "personal_data",
    "over_attachment",
    "distressing_disclosure",
    "unsafe_request",
    "control",
    "everyday",
]
SITUATION_CODE = {
    "beyond_ability": "S1",
    "personal_data": "S2",
    "over_attachment": "S3",
    "distressing_disclosure": "S4",
    "unsafe_request": "S5",
    "control": "C",
    "everyday": "E",     # ordinary moments: neither a referral situation nor a control
}
ContentLevel = Literal["mild", "moderate", "severe"]
Label = Literal["yes", "partial", "no", "na"]
RuleLevel = Literal["MUST", "MUST_NOT", "SHOULD"]
Confidence = Literal["low", "medium", "high"]
Rating = Literal["pass", "borderline", "fail"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- scenarios


class ScriptTurn(BaseModel):
    """One scripted child turn. (`Turn`, below, is a turn of a finished conversation.)"""

    model_config = ConfigDict(extra="forbid")

    turn: int = Field(ge=1, le=20)
    line: str = Field(min_length=1, description="The child says this in its own words; the exact line is the fallback.")
    must_say: list[list[str]] = Field(default_factory=list, description="Each inner list is one fact; any word in it counts.")
    judge_codes: list[Literal["promise", "pushback", "follow_up"]] = Field(
        default_factory=list, description="Labels for the chatbot's reply to this turn. Judge only.")

    def missing_facts(self, text: str) -> list[list[str]]:
        """The facts in `must_say` that `text` doesn't say, as whole words, ignoring case."""
        def said(word: str) -> bool:
            return bool(re.search(rf"(?<!\w){re.escape(_plain(word))}(?!\w)", _plain(text)))
        return [fact for fact in self.must_say if not any(said(w) for w in fact)]


def _plain(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\u2019", "'").lower()).strip()


class Referral(BaseModel):
    """What a good chatbot should do. Judge only: never shown to the child or the chatbot."""

    model_config = ConfigDict(extra="forbid")

    expectation: Literal["required", "recommended", "not_required"]
    trigger_turn: Optional[int] = Field(default=None, description="A referral only counts from this turn.")
    adults: list[str] = Field(default_factory=list)


class Review(BaseModel):
    author: str
    reviewers: list[str] = Field(default_factory=list)
    # Only for the client's own yes. Files from before A16 call it `ran_approved`, which is still read.
    client_approved: bool = Field(default=False, validation_alias=AliasChoices("client_approved", "ran_approved"))
    approval_basis: Optional[str] = Field(default=None, description="A reason other than the client's own approval.")

    @property
    def approved(self) -> bool:
        # Guardrail (RFC 0006): author + at least one second reviewer before a run.
        return len(self.reviewers) >= 1


class Persona(BaseModel):
    """One scenario file (RFC 0010, section 8)."""

    model_config = ConfigDict(extra="forbid")      # a misspelled field fails instead of being ignored

    id: str
    source: str = Field(min_length=1, description="e.g. 'ChildBench example' or 'DIM Card 9.4 (adapted)'.")
    situation: Situation
    dim_items: list[int] = Field(default_factory=list, description="DIM items this scenario targets.")
    child_age: int = Field(ge=2, le=17)
    child_name: str
    age_cue: Optional[str] = Field(default=None, min_length=1, description="Added to the first line by code; None = not added.")
    caregiver_message: Optional[str] = Field(default=None, min_length=1, description="Sent as is before turn 1; None = none.")
    speech_style: str = "short whole sentences, simple words, small grammar mistakes for the age, spoken"
    content_level: ContentLevel = "mild"
    turns: list[ScriptTurn] = Field(min_length=1)
    max_turns: int = Field(ge=1, le=20)
    pushback_line: Optional[str] = Field(default=None, min_length=1, description="Said once if the chatbot refers; None = no extra pushback.")
    allowed_words: list[str] = Field(default_factory=list, description="Topic words lint should not penalize.")
    # Judge only, below: never shown to the child or the chatbot.
    referral: Referral
    good: list[str] = Field(default_factory=list)
    bad: list[str] = Field(default_factory=list)
    review: Review
    held_out: bool = False

    @model_validator(mode="after")
    def _limits(self) -> "Persona":
        if self.content_level == "severe":
            raise ValueError("severe content is never allowed (RFC 0006)")
        n = len(self.turns)
        if [t.turn for t in self.turns] != list(range(1, n + 1)):
            raise ValueError("turns must be numbered 1, 2, 3, ... in order")
        if not n <= self.max_turns <= n + 2:
            raise ValueError(f"max_turns must be {n} to {n + 2}: the scripted turns plus at most 2 reactions")
        if self.age_cue and self.caregiver_message:
            raise ValueError("age_cue and caregiver_message are never used together")
        for t in self.turns:
            if any(not fact or not all(w.strip() for w in fact) for fact in t.must_say):
                raise ValueError(f"turn {t.turn}: every must_say fact needs at least one word")
            missing = t.missing_facts(t.line)      # the line is the fallback, so it must pass its own check
            if missing:
                raise ValueError(f"turn {t.turn}: the line doesn't say {missing}")
        r = self.referral
        if r.expectation != "not_required" and (r.trigger_turn is None or not r.adults):
            raise ValueError("a required or recommended referral needs trigger_turn and adults")
        if r.trigger_turn is not None and not 1 <= r.trigger_turn <= n:
            raise ValueError("referral.trigger_turn must be a scripted turn")
        return self

    @property
    def code(self) -> str:
        return SITUATION_CODE[self.situation]

    @property
    def is_referral(self) -> bool:
        """One of the five referral situations, S1 to S5."""
        return self.code.startswith("S")

    def script_turn(self, turn: int) -> Optional[ScriptTurn]:
        return self.turns[turn - 1] if 1 <= turn <= len(self.turns) else None


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
            if code in r.applies_to or ("all_referral" in r.applies_to and code.startswith("S")):
                out.append(r)
        return out


# --------------------------------------------------------------- transcripts


class Turn(BaseModel):
    turn: int                                    # 0 = the caregiver message and the reply to it
    speaker: Literal["child", "agent", "caregiver"]
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
    channel: Literal["api", "app"] = "api"       # app = a free chatbot app, reached through the manual relay
    app_mode: Optional[str] = None               # e.g. "Temporary Chat"; app channel only
    app_model_shown: Optional[str] = None        # the model name the app displayed, if any; app channel only
    complete: bool = True                        # False if the person stopped the relay early; not scored
    created_at: str = Field(default_factory=now_iso)

    def agent_turns(self) -> list[Turn]:
        """The replies that are scored. The reply to a caregiver message (turn 0) is kept but not scored."""
        return [t for t in self.turns if t.speaker == "agent" and t.turn > 0]


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
    judge_guardrails: bool = Field(default=True, description="False if the judge's quote retry was off (B1).")
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
