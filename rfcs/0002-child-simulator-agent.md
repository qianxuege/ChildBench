# RFC 0002: Child Simulator Agent and Orchestrator

- **Status:** Draft
- **Created:** 2026-10-25 (updated 2026-11-23)
- **Owner role:** 3. Child simulator and orchestrator
- **Tech requirements addressed:** 1, 2, 3, 5, 6
- **Related RFCs:** 0004, 0005, 0006, [0010](0010-implementation-plan.md)

## Summary

An AI agent plays a child in short multi-turn conversations. It follows a script, calls tools to check its own lines, and can't send a line that fails them. A plain-code orchestrator runs scenarios × chatbots × seeds.

## Motivation

Many of the moments we test only appear over several turns, and simulated children tend to be too cooperative and too fluent. So the design checks that the child sounds its age, says the key facts, and stays within the approved content level.

## Detailed design

### How one line is produced

1. `next_disclosure_step` says which scripted turn applies: script, pushback or react.
2. The child says that turn's `line` in its own words, given its age, style and the conversation so far.
3. The line is checked, cheapest first: content level, speech for the age, realistic personal data, and the key facts in `must_say`.
4. If a check fails, the reasons go back to the child, up to 3 tries.
5. After 3 failures the scripted `line` is used as written and flagged `FALLBACK_USED`.

Today, code calls the checks for the child. Package A3 makes the child a tool-calling agent that ends each turn with `submit_child_line`, which enforces the same checks.

**What the child never sees:** expected behaviour, referral targets, judge codes or good and bad examples. It must not hint that it needs an adult unless the script says so.

**A line above the content level never reaches the chatbot,** with guardrails on or off.

**The reviewer model is off.** It judged child grammar inconsistently ([F-003](../docs/findings.md)).

### Pushback

After the last scripted turn, if the chatbot points the child to an adult, the child resists once with the scenario's `pushback_line`. Scenarios whose script already includes the pushback set it to null.

### Speech check: `lint_child_utterance`

Checks words per sentence for the age, adult giveaway words, stage directions and emoji. Limits are hand-set, and results say `"calibrated": false`. The realism report (A8) shows how far off they are.

### Orchestrator (plain code, no AI)

- Runs every scripted turn in order, then up to 2 reaction turns, up to `max_turns`.
- Marks a conversation incomplete if a model errors or the relay is stopped; incomplete ones aren't scored.
- Writes JSONL transcripts and a manifest with model ids, settings, seeds, rubric version and git commit.

### When people are alerted

Alerts go to the review queue and don't block the run. Affected transcripts stay out of headline numbers until cleared.

| Trigger | Reason code |
|---|---|
| Fallback line used | `FALLBACK_USED` |
| Content level exceeded on 2 or more turns | `PERSONA_TOO_INTENSE` |
| Random 10% of accepted lines | `AUDIT_SAMPLE` |

### Realism report (A8)

| Source | Measures | Ages |
|---|---|---|
| [CHILDES](https://talkbank.org/childes/): real children's speech, typically developing children only | Words per sentence, vocabulary variety, questions | Where the data covers the age |
| [Kuperman et al. (2012)](https://doi.org/10.3758/s13428-012-0210-4) age-of-acquisition norms | Share of words learned after the child's age | All |

Report values for both sides, sample sizes and effect sizes. A non-significant test isn't proof of a match.

**Limits to state:** CHILDES is spoken, while children type to chatbots; its children talk to parents about everyday things, so it checks *how* the child speaks, not *what* it discloses; the norms are adults' memories; the speech check already shapes length and adult words; no one rates how childlike the lines sound. Check each corpus's terms and cite it.

### Script fidelity (A8)

Does the child do what the scenario asks? Counts over complete conversations:

| Measure | Meaning |
|---|---|
| Turns delivered | Scripted turns said, out of those due |
| Key facts, first try | Turns with `must_say` facts that passed the check without a retry |
| Key facts, final | The same, after retries but before any fallback |
| Fallback rate | Lines replaced by the scripted line |
| Pushback when due | Pushbacks said, out of the conversations where the rule called for one |
| Incomplete | Conversations that stopped early, reported separately |

Reaction turns have no key facts, so people check them in the 10% sample. Guardrails-off runs are reported separately.

## Alternatives considered

- **Generator with prompt-only safety:** no independent check. It's the guardrails-off arm (RFC 0006).
- **Fixed scripted dialogue:** can't react. Kept as the fallback.
- **Voice:** out of scope this term.

## Risks

- AIs write "cute child" caricatures. Mitigation: speech check, realism report, human review.
- Small local models may break the tool protocol. Mitigation: a config switch to today's path.
