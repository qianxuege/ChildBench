# RFC 0006: Safety Guardrails and With/Without Comparison

- **Status:** Draft
- **Created:** 2026-10-25 (updated 2026-11-23)
- **Owner role:** 3. Child simulator and orchestrator (with 2)
- **Tech requirements addressed:** 7, 7.1, 3
- **Related RFCs:** 0002, 0004, 0005, [0010](0010-implementation-plan.md)

## Summary

The risks our own system creates, the guardrails against them, and a comparison with and without those guardrails.

## Scope

Guardrails apply to the agents **we build** (child and judge) and to what we **publish**. They never change the tested chatbot's replies, because then we'd be measuring our filter. Whether the chatbot is age-appropriate is scored by the rubric (RFC 0008).

## Detailed design

### Risks and guardrails

| Risk | Guardrail |
|---|---|
| Child produces intense content | Content level per scenario; `check_content_ceiling` |
| Child sounds adult or breaks character | `lint_child_utterance`, `next_disclosure_step` |
| Child skips the key moment | `must_say` check |
| Child hints at the right answer | It never sees the expected behaviour |
| Realistic personal data | Fictional-data pattern check |
| Judge makes unsupported claims | `verify_evidence_quote`, schema check, code-set overrides |
| Scores reward "ask a grown-up" every time | Controls and the over-escalation code |
| Harmful or uncleared material published | Publishing check; `private/` folder for the gold set |
| Over-claiming | Ranges, sample sizes and judge agreement shown on the site |

### When checks run

Each check writes a line to `guardrail_log.jsonl`.

| Stage | Check | On failure |
|---|---|---|
| Before a run | Scenario format, content level, reviewer (`allow_unreviewed` for internal runs) | Run doesn't start |
| Each child line | Content level (always enforced) | Retry with reasons, then the scripted line |
| Each child line | Speech, fake data, key facts | Retry with reasons, then the scripted line |
| Scoring | Quotes and format | Retry once, then `unsupported` and a person |
| Scoring | `detect_pii_request` sets S2.1 | Judge can't overrule it |
| Before publishing | Content check on every turn, including the chatbot's; review and clearance status | Left out |

Scripted lines are checked too, when the scenario is validated.

**Severity scale** (the client to confirm):

- `mild`: feelings and vague events ("someone was mean to me"). Allowed.
- `moderate`: a named harm without detail ("he hit me"). Only with the client's approval.
- `severe`: graphic, injury, sexual, weapon or self-harm detail. Never.

**Free apps:** mild and cleared scenarios only, guardrails always on (RFC 0010, section 7).

### With/without comparison (requirement 7.1)

The same 15 scenarios with one Ollama chatbot and one seed:

- **Off (shadow mode):** safety is only asked for in the prompt. Checks still run and log what they would have caught. The content level still blocks.
- **On:** the checks are enforced.

**What we show.** Each measure is reported for on and off, with counts:

| Guardrail | Measure |
|---|---|
| Speech check | Adult-sounding child lines that reached the chatbot |
| Key-facts check | Key moments missed |
| Fake-data check | Lines with realistic personal data that reached the chatbot |
| Content level (never off) | Lines it stopped in each run |
| Quote check (judge) | Unsupported quotes in the 15 "on" chats; agreement with the human scores on the gold set's tuning half |
| All switchable guardrails together | Scenarios where the chatbot's DIM total or referral rating differs between runs, and how many of those trace to a logged guardrail failure |

Plus side-by-side examples: the same scenario turn with and without each guardrail.

Some failures may be rare in 15 conversations. So we also report how often each check catches bad lines, and flags good ones, on the small labelled sets (RFC 0010, B6). Those lines never go to a chatbot.

**Where:** data and statistics in `results/guardrails/` (team Drive); numbers only in `docs/guardrail-comparison.md`.

**When:** runs by Nov 28, table and examples done Nov 29, before the final results.

Guardrails-off runs stay internal. Only numbers and a few checked examples go on the poster.

## Risks

The comparison is small (15 conversations per arm), so report counts, not significance.
