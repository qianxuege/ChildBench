# RFC 0005: Scenarios and Synthetic Data Quality

- **Status:** Draft
- **Created:** 2026-10-25 (updated 2026-11-23)
- **Owner role:** 2. Scenarios and rubric
- **Tech requirements addressed:** 6
- **Related RFCs:** 0002, 0006, 0008, [0010](0010-implementation-plan.md)

## Summary

The benchmark has 15 scenarios adapted from the DIM preprint's cards. Our 7 pilot scenarios are public examples. Everything is synthetic.

## Detailed design

**Format and the list of 15:** [RFC 0010, section 8](0010-implementation-plan.md#8-scenarios).

**Adapting a card:**

1. Keep the child's lines as the `turns`, in order.
2. Remove the parent's message and add `age_cue`, except: one 4.1 file keeps the message in `caregiver_message`, and the no-context 4.1 and no-age 9.4 files have neither.
3. List each turn's key facts in `must_say`.
4. Copy the card's appropriate and inappropriate responses into `good` and `bad`, and its targets into `referral.adults`.
5. Replace anything that looks like real personal data with placeholders.
6. Set `source` to the card and note "adapted".

**Age:** each scenario has one exact age. The judge uses it directly, and results aren't split by age. A teenager isn't always required to involve a parent: `referral.expectation` and `referral.adults` say what fits that case.

**Controls** (9.7, 9.8 and our `ctrl-*` examples) are chats where a good chatbot should simply help. Suggesting a parent for comfort is fine; a crisis line, professional or authority is over-escalation.

**Quality checks**

1. `source` says where each scenario came from.
2. Author plus one reviewer; distress scenarios also need the client's approval, or an `approval_basis` such as the preprint's released cards (RFC 0010, R13).
3. `make check` validates the format, content level and limits.
4. `must_say` confirms the key facts were actually said in each conversation.
5. The realism report compares the child's lines with real children's (RFC 0002).
6. Half of the gold set isn't used for tuning the judge (RFC 0003).

**Data protection:** fictional names, no real child data, no realistic phone numbers or addresses. Cards, transcripts and quoted rubric text may be published with credit (CC BY 4.0); the gold set stays in `private/` (RFC 0010, R2).

## Alternatives considered

- **Real child–chatbot logs:** more realistic, but unavailable and ethically difficult.
- **One version of each scenario per age group:** about 25 scenario ideas, each rewritten for 3 age groups (about 75 files): none would have had human scores to compare with.

## Risks

- Removing the parent's message (all files but one) changes the test, so our scores aren't directly comparable with the preprint's. Even with the message, our child rewords its lines.
- Distress approvals rest on the client or the preprint's release (RFC 0010, R13).
