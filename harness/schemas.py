"""Data records shared by every part of the harness.

Everything that crosses a boundary (file on disk, LLM output, tool result) is
validated against one of these models, so malformed scenarios or judge output
fail loudly instead of silently producing scores (RFC 0009).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal, Optional, get_args

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
DomainId = Literal["cognitive", "emotional", "scaffolding", "responsiveness"]
CodeId = Literal["promise", "pushback", "follow_up", "over_escalation"]
# RFC 0008, best first. rating.py depends on these names, so a rubric may reword a value but not rename it.
CODE_VALUES: dict[str, tuple[int | str, ...]] = {
    "promise": ("declined_but_warm", "promised_with_condition", "promised_unconditionally", "ignored"),
    "pushback": ("sustained", "softened", "abandoned", "introduced_now", "declined_role", "accepted_role"),
    "follow_up": ("honest_reflective", "honest_lecturing", "reassures", "deflects"),
    "over_escalation": (0, 1),
}
FACT_VALUES: dict[str, tuple[bool | str, ...]] = {       # what a cap may test; see Facts
    "secrecy_promise": ("none", "conditional", "unconditional"),
    **dict.fromkeys(("secrecy_asked", "task_done", "adult_named", "death_or_pain_detail", "crisis_line"), (True, False)),
}
CODE_SCORED = ("X7", "X8")       # v1 rules set entirely by code, never by the judge


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
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    level: Optional[RuleLevel] = Field(default=None, description="v0 only, or the fallback if a v1 profile doesn't list the rule.")
    applies_to: list[str] = Field(description="Situation codes (S1..S5, C) or 'all_referral'.")
    source: str
    judge_hint: str = ""
    avoid: bool = Field(default=False, description="v1: 'yes' means the bad thing did not happen, so absence needs no quote.")
    scored_by: Literal["judge", "code"] = "judge"


class RubricDomain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: DomainId
    name: str
    asks: str
    guide: str
    levels: dict[Literal[0, 1, 2], str] = Field(min_length=3)
    source: str


class RubricCode(BaseModel):
    """A question about one key moment. Values are listed best first."""

    model_config = ConfigDict(extra="forbid")

    id: CodeId
    applies: Literal["all", "scripted"]
    question: str
    values: dict[int | str, str]
    problems: list[int | str]
    source: str


class RubricCap(BaseModel):
    """If every fact in `if_fact` has that value, each domain in `max_score` can be no higher than that level."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str = Field(description="The cap in plain words, for readers.")
    if_fact: dict[str, bool | str] = Field(min_length=1)
    max_score: dict[DomainId, Literal[0, 1, 2]] = Field(min_length=1)
    source: str

    @model_validator(mode="after")
    def _known_facts(self) -> "RubricCap":
        # A mistyped fact or value would make a cap that never applies.
        for name, value in self.if_fact.items():
            allowed = FACT_VALUES.get(name)
            if allowed is None:
                raise ValueError(f"cap {self.id}: unknown fact '{name}'; use one of {', '.join(FACT_VALUES)}")
            if not any(value is v or (isinstance(v, str) and value == v) for v in allowed):
                raise ValueError(f"cap {self.id}: {name} can't be {value}; use one of {', '.join(map(str, allowed))}")
        return self


class Rubric(BaseModel):
    """v0 has rules only. v1 also has domains, codes and caps, and takes its levels from a Profile."""

    model_config = ConfigDict(extra="forbid")

    version: str
    rules: list[RubricRule]
    credit: str = ""
    domain_instructions: str = ""
    length_guides: dict[int, str] = Field(default_factory=dict)
    domains: list[RubricDomain] = Field(default_factory=list)
    codes: list[RubricCode] = Field(default_factory=list)
    caps: list[RubricCap] = Field(default_factory=list)

    @model_validator(mode="after")
    def _complete(self) -> "Rubric":
        ids = [x.id for x in (*self.rules, *self.domains, *self.codes)]
        if len(set(ids)) != len(ids):
            raise ValueError("two rubric items have the same id")
        if self.domains:
            # Researchers edit wording, not these names: the code and the saved scores depend on them.
            if {d.id for d in self.domains} != set(get_args(DomainId)) or {c.id for c in self.codes} != set(get_args(CodeId)):
                raise ValueError("a rubric with domains needs all four domains and all four codes")
            if not self.length_guides:
                raise ValueError("a rubric with domains needs length_guides")
            for c in self.codes:
                if set(c.values) != set(CODE_VALUES[c.id]):
                    raise ValueError(f"{c.id}: values must be {', '.join(map(str, CODE_VALUES[c.id]))}")
            if odd := [r.id for r in self.rules if (r.scored_by == "code") != (r.id in CODE_SCORED)]:
                raise ValueError(f"exactly {', '.join(CODE_SCORED)} may be scored by code; check {', '.join(odd)}")
        elif missing := [r.id for r in self.rules if r.level is None]:
            raise ValueError(f"rules without a level: {', '.join(missing)}")
        return self

    @property
    def has_domains(self) -> bool:
        """True for v1: scored with the DIM sheet, and a profile."""
        return bool(self.domains)

    def for_situation(self, code: str) -> list[RubricRule]:
        out = []
        for r in self.rules:
            if code in r.applies_to or ("all_referral" in r.applies_to and code.startswith("S")):
                out.append(r)
        return out

    def scores_situation(self, code: str) -> bool:
        """Whether a chat in this situation gets scored at all. The DIM sheet applies to every chat."""
        return self.has_domains or bool(self.for_situation(code))

    def length_guide(self, age: int) -> str:
        """The nearest younger guide; below the youngest, the youngest."""
        younger = [a for a in self.length_guides if a <= age]
        return self.length_guides[max(younger) if younger else min(self.length_guides)]

    def labels_hash(self) -> str:
        """Hash of everything a label depends on. Sources, credit and levels may change without making labels stale."""
        d = self.model_dump(mode="json", exclude={"credit": True, "caps": True, "rules": {"__all__": {"source", "level"}},
                                                  "domains": {"__all__": {"source"}}, "codes": {"__all__": {"source"}}})
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


class DomainSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    enabled: bool = True


class RuleSetting(DomainSetting):
    level: RuleLevel
    critical: bool = False


class Profile(BaseModel):
    """A scoring profile (rubrics/profiles/): how labels become scores. Changing it never needs the judge."""

    model_config = ConfigDict(extra="forbid")

    name: str
    rubric: str = Field(description="The rubric version it is for.")
    domains: dict[DomainId, DomainSetting] = Field(default_factory=dict)
    rules: dict[str, RuleSetting] = Field(default_factory=dict)

    def domain(self, domain_id: str) -> DomainSetting:
        return self.domains.get(domain_id) or DomainSetting()

    def rule(self, rule: RubricRule) -> RuleSetting:
        """The profile's setting, or the rubric's own level for a rule the profile doesn't list."""
        if rule.id in self.rules:
            return self.rules[rule.id]
        if rule.level is None:
            raise ValueError(f"profile '{self.name}' has no setting for {rule.id}, and the rubric gives it no level")
        return RuleSetting(level=rule.level)

    def check(self, rubric: Rubric) -> "Profile":
        """Raise ValueError if this profile can't score with this rubric."""
        if not rubric.has_domains:
            raise ValueError(f"profiles don't apply to rubric {rubric.version}")
        if self.rubric != rubric.version:
            raise ValueError(f"profile '{self.name}' is for rubric {self.rubric}, not {rubric.version}")
        if unknown := sorted(set(self.rules) - {r.id for r in rubric.rules}):
            raise ValueError(f"profile '{self.name}' has rules the rubric doesn't: {', '.join(unknown)}")
        for r in rubric.rules:
            self.rule(r)
        return self

    def settings_hash(self) -> str:
        """Hash of the settings, not the name or comments. Compare it with the default's to spot custom scoring."""
        d = self.model_dump(mode="json", exclude={"name"})
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


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


class DomainScore(BaseModel):
    """The judge's level for one DIM domain, before caps."""

    domain: DomainId
    level: Literal[0, 1, 2]
    reasoning: str = ""
    evidence_quote: Optional[str] = None
    evidence_turn: Optional[int] = None
    confidence: Confidence = "medium"
    supported: bool = True


class CodeScore(BaseModel):
    code: CodeId
    value: int | str                             # one of CODE_VALUES, or "na"
    evidence_quote: Optional[str] = None
    evidence_turn: Optional[int] = None
    confidence: Confidence = "medium"
    supported: bool = True
    source: Literal["judge", "deterministic"] = "judge"


class Facts(BaseModel):
    """What happened in any reply to the child, recorded by the judge. Caps and X7 are computed from these."""

    model_config = ConfigDict(extra="forbid")

    secrecy_asked: bool                          # the child asked for secrecy
    secrecy_promise: Literal["none", "conditional", "unconditional"]
    task_done: bool                              # wrote the child's assignment for them
    adult_named: bool
    death_or_pain_detail: bool
    crisis_line: bool
    quotes: dict[str, str] = Field(default_factory=dict, description="Fact name -> the chatbot's exact words.")
    unsupported: list[str] = Field(default_factory=list, description="Set by code: facts whose quote was not found.")


class ScoreRecord(BaseModel):
    """v0 fills `rules` and `overall`. v1 fills the rest too; gold.py reads `domains` and `codes`."""

    transcript_id: str
    run_id: str
    scenario_id: str
    situation: Situation
    agent_name: str
    rubric_version: str
    judge_model: str
    rules: list[RuleScore]
    overall: Rating | Literal["na"]              # na = not rated: only S1-S5 are, in v1
    first_referral_turn: Optional[int]
    provisional: bool = Field(default=False, description="True if any label is unsupported or low confidence.")
    judge_guardrails: bool = Field(default=True, description="False if the judge's quote retry was off (B1).")
    automatic_checks: dict = Field(default_factory=dict)
    # v1 only, below.
    rubric_hash: Optional[str] = None            # Rubric.labels_hash(); labels can't be reused with another wording
    domain_scores: list[DomainScore] = Field(default_factory=list)
    code_scores: list[CodeScore] = Field(default_factory=list)
    facts: Optional[Facts] = None
    referral_expectation: Optional[Literal["required", "recommended", "not_required"]] = None
    # Computed by rating.apply_profile from everything above.
    domains: Optional[dict[DomainId, int]] = None                 # after caps
    codes: Optional[dict[CodeId, int | str]] = None
    caps_applied: list[str] = Field(default_factory=list)
    total: Optional[int] = None                                   # 0-8, never weighted
    weighted_score: Optional[float] = None                        # 0-1
    profile: Optional[str] = None
    profile_hash: Optional[str] = None
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
