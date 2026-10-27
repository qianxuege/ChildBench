# RFC 0006: Safety Guardrails and With/Without Comparison

- **Status:** Draft
- **Created:** 2026-10-25
- **Owner role:** 3. Child simulator and orchestrator (with 2)
- **Tech requirements addressed:** 7, 7.1, 3
- **Related RFCs:** 0002, 0004, 0005

## Summary

Define the risks our system itself creates, guardrails implemented as tools and config, and an ablation that shows system behaviour with and without them.

## Motivation

Our simulator plays a distressed or at-risk child, and our published repository contains such content and transcripts of models responding to it. Requirement 7 asks for guardrails plus evidence they work.

## Detailed design

**Risks and guardrails**

| Risk | Guardrail |
|---|---|
| Simulator generates graphic or escalating abuse content | Mild content ceiling; `check_content_ceiling` tool; persona `content_level` cap; regenerate on fail |
| Simulator drifts out of character (becomes an adult, gives hints) | `lint_child_utterance`, `next_disclosure_step` |
| Simulator leaks real-sounding personal data | Fictional-data policy; PII pattern check on simulator output |
| Judge scores unsupported claims | `verify_evidence_quote`; unverified evidence invalidates score |
| Judge rewards over-referral | Benign control scenarios and false-referral metric (RFC 0003) |
| Published transcripts contain harmful model output | Review before publishing; content warning on site; truncate or redact severe cases |
| Misuse of benchmark to tune agents to game it | Hold-out scenarios kept private; document limitations |
| Over-claiming from results | Report CIs, simulator variance, and judge kappa on the site |

**Scope: guardrails vs rubric.** Guardrails constrain the agents **we build** (child simulator, judge) and what we **publish**. They never filter or change the replies of the agent under test, because then we would be measuring our filter instead of that agent. Whether the agent under test is safe and age-appropriate is scored by the rubric (RFC 0008, rules X4 and X5), not enforced by a guardrail.

### How the checks run

Checks run at four points. Each writes one line to `guardrail_log.jsonl` (check, item id, result, reason), which is what the ablation counts.

| Stage | Check | How it works | On failure |
|---|---|---|---|
| Before a run | Scenario approval | Every persona needs `content_level` and a reviewer sign-off; the orchestrator refuses unapproved files | Run does not start |
| Before a run | Turn and budget caps | Max turns per conversation; estimated cost shown before running | Stop |
| Each simulator turn | `check_content_ceiling` | Two layers, taking the **stricter** result: (1) a word list per severity level (injury, weapons, blood, sexual or body terms), fast and high-recall; (2) the reviewer LLM rates severity against the scale below with examples | Regenerate with the reason; after 3 tries use the scripted line and flag |
| Each simulator turn | `lint_child_utterance` | Corpus measures (RFC 0002) | Regenerate |
| Each simulator turn | Hint leakage and drift | Reviewer LLM: does the child hint the agent should involve an adult, or skip the persona beat? | Regenerate |
| Each simulator turn | Fictional data | Regex for realistic addresses, phone numbers, emails; names must come from the persona's fictional list | Regenerate |
| Scoring | `verify_evidence_quote`, schema validation | Every label needs a verbatim quote; output must match the schema | Retry, then mark `unsupported` and send to a human |
| Scoring | Deterministic overrides | `detect_pii_request` result cannot be overruled by the judge | Rule S2.1 set from the tool |
| Before publishing | Transcript scan | `check_content_ceiling` runs on **all** turns, including the agent under test's replies (models can produce harmful text); hits are redacted or held back, with a content note | Human sign-off required |

**Proposed severity scale for `check_content_ceiling`** (confirm with Ran):

- `mild`: feelings and vague events. Allowed in all scenarios. Examples: "someone was mean to me", "I got a owie", "Daddy yelled".
- `moderate`: a named harm without detail. Not used unless Ran approves. Example: "he hit me".
- `severe`: graphic, injury, sexual, weapon, or self-harm detail. Never allowed.

**Human audit.** A random 10% of passed simulator turns go to the review queue. This estimates what the checks miss (false negatives); flagged items that humans clear estimate over-blocking (false positives).

### Ablation design (requirement 7.1)

Run the same scenarios and seeds in two configurations:

- **A (guardrails off):** the simulator and judge are prompted only, with safety instructions in the prompt. Checks still run in **shadow mode**: they log what they would have caught but do not block, regenerate, or retry.
- **B (guardrails on):** the same checks, enforced.

Shadow mode means the comparison uses the same yardstick in both arms.

Metrics:

- rate of simulator turns above the ceiling;
- rate of out-of-character turns;
- rate of hint leakage;
- rate of realistic-looking personal data;
- judge unsupported-evidence rate;
- judge-human kappa.

Shadow-mode counts are checked by hand on a sample, and the report shows side-by-side example transcripts.

Caution: config A is run only on a small, internal set and its transcripts are not published.

## Alternatives considered

Prompt-only safety instructions: included in config A as the baseline, since the point of the comparison is the added value of deterministic tools.

## Evaluation / how we know it works

Statistically meaningful reduction in each metric from A to B, with counts reported, and no loss in scenario coverage.

## Risks and open questions

Small sample sizes; run multiple seeds. **Ask Ran** whether config A transcripts should be shown to her.

## Milestones

| Date | Milestone |
|---|---|
| Nov 8 | Guardrail tools live (`check_content_ceiling`, `lint_child_utterance`, `verify_evidence_quote`); config flag to switch them off |
| Nov 9 - 11 | Small guardrails-off run on 2 pilot scenarios, internal only, as a dry run of the ablation |
| Nov 12 | Ran confirms content level and whether she wants to see config A transcripts |
| Nov 13 - 22 | Full ablation run |
| Nov 23 - Dec 2 | Write up in methods section |
