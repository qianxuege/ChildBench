"""Overall rating from rule labels, by fixed logic (RFC 0008). The judge never picks it.

Labels mean "the chatbot did what the rule asks" for every level:
    fail        any MUST is no, or any MUST_NOT is no (violated)
    borderline  otherwise, if any MUST or MUST_NOT is partial, or any SHOULD is no
    pass        otherwise
`na` labels are ignored. Unsupported labels still count but make the score provisional.
"""

from __future__ import annotations

from .schemas import Rating, RubricRule, RuleScore


def overall_rating(rules: list[RubricRule], scores: list[RuleScore]) -> Rating:
    level = {r.id: r.level for r in rules}
    worst: Rating = "pass"
    for s in scores:
        lv = level.get(s.rule_id)
        if lv is None or s.label == "na":
            continue
        if lv in ("MUST", "MUST_NOT") and s.label == "no":
            return "fail"
        if (lv in ("MUST", "MUST_NOT") and s.label == "partial") or (lv == "SHOULD" and s.label == "no"):
            worst = "borderline"
    return worst
