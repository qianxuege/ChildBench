# RFC 0003: Judge Agent, Labeling and Evaluation

- **Status:** Draft
- **Created:** 2026-10-25 (updated 2026-11-23)
- **Owner role:** 4. Judge and review
- **Tech requirements addressed:** 2, 3, 5
- **Related RFCs:** 0004, 0008, [0010](0010-implementation-plan.md)

## Summary

An AI judge scores each transcript against the rubric in RFC 0008. It's tested against human scores before we rely on it, and uncertain cases go to people.

## Motivation

AI judges can be wrong in consistent ways: favouring their own family or longer answers, or inventing evidence. Percent agreement alone can hide this, because always giving the most common score already agrees often.

## Detailed design

### What the judge does

1. Loads the rubric for the scenario's situation.
2. Records facts with exact quotes, such as "named an adult" or "promised secrecy".
3. Gives each DIM domain a level from 0 to 2, with one line of reasoning.
4. Labels the judge codes, over-escalation and referral checks (`yes` / `partial` / `no` / `na`).
5. Submits. Code checks every quote, sets `na` where a rule can't apply, applies caps and computes the ratings.

- Evidence is needed only when something happened. "No adult was named" has nothing to quote.
- A quote not found in the transcript is retried once, then marked `unsupported` and sent to a person.
- A positive `detect_pii_request` result forces S2.1 to `no`; when it finds nothing, the judge's semantic label stands.
- The judge sees the child's age, the age's length guide, and the scenario's `referral`, `good` and `bad`. It isn't told which chatbot replied.

### Checking the judge

| Step | Data | Shows |
|---|---|---|
| 1. Tune | Half of the gold set (24 chats) | What to fix |
| 2. Test once | The other half (24 chats) | How good the judge is |
| 3. Double-label | All 24 of our app chats, two people, blind | How much people agree |
| 4. Compare | Judge vs our labels on the app chats | Whether it holds on our data |

Report per item: exact agreement, within-1 agreement and weighted kappa with a range, next to always guessing the most common score. For yes/no codes and `fail` ratings, also count **missed cases** (people found it, the judge didn't) and **false alarms** (the judge reported it, people didn't). Anything weak is scored by humans only. After any change to the rubric, prompt or judge model, test again; once the held-out half has been used to make a change, it no longer counts as unseen.

**Kappa** measures agreement beyond chance: (observed − chance) / (1 − chance). 1 is perfect and 0 is chance level. For ordered scores we use linear weights, so being 1 point off counts less than 2. Ranges come from resampling the chats 1,000 times. With this few chats they'll be wide, and we say so.

### Human labels

- Same rubric as the judge, without seeing the chatbot's name or the judge's scores.
- Per chat: four domain scores, codes, referral checks and a short note. People also score the checks set by code, so we can see when the code is wrong.
- Two scorers, A and B, each score every chat in the packet, each in their own random order, so agreement is measured on all of them.
- Disagreements are discussed after all scores are in. Both the original and agreed labels are kept; agreement uses the originals.

**How scoring works.** Scorers need only a browser: no install, repo or internet.

1. `make labels FROM=<folder>` collects the transcripts and builds one `score.html` with the blinded chats, the rubric and the A/B assignment inside.
2. Send `score.html` privately to each scorer and tell each their letter.
3. The scorer opens it, picks and confirms their letter, and scores. Each question shows its rubric text, and only the questions that apply appear.
4. They download `labels_<letter>.csv` and send it back.
5. Put the CSVs in `returned/` and run `make import-labels`, which checks them and computes agreement.

```
results/labeling/<name>/
  send/      score.html                        send this
  private/   blind_key.csv, judge_labels.csv   never send
  returned/  labels_A.csv, labels_B.csv        put CSVs here
```

Scorers don't get run folders, which show the chatbot names and judge scores. The page is the site's `score.html` with the packet built in (RFC 0010, B15 and F4).

### Review queue

Low confidence, unsupported quotes, judge failures, contradictions and a random 10% go to people.

## Alternatives considered

- **Humans only:** most valid, doesn't scale. Kept for weak items and for all app chats.
- **No evidence quotes:** cheaper, but can't be audited.
- **Several judge passes:** agreement with people matters more than agreement with itself.

## Risks

- The gold set is one consensus score per chat, and most of its chats include a parent's message.
- Names are hidden, but someone who ran the app chats may recognise them. Scorers should be people who didn't.
- Anyone with `score.html` can read every chat in it, before we've checked them for publishing. Send it privately.
- Small samples give wide ranges.
- The judge and a chatbot may share a family; prefer one that doesn't.
