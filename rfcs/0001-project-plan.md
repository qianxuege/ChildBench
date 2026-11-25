# RFC 0001: Project Plan and Key Decisions

- **Status:** Draft
- **Created:** 2026-10-25 (updated 2026-11-23)
- **Owner role:** 1. Lead and client
- **Tech requirements addressed:** 1 (overall approach)
- **Related RFCs:** 0002 to 0010

## Summary

ChildBench tests whether chatbots respond to children well, and whether they involve a trusted adult at the right moments and not otherwise. We build two agents, a **child simulator** and a **judge**, that exercise and score a chatbot over several turns.

This RFC records why. [architecture.md](../architecture.md) gives the overview, and [RFC 0010](0010-implementation-plan.md) has the current plan, packages and dates.

## Motivation

Feedback on our first scope: it read like a prompt set with a scoring script, not an agentic workflow, and behaviours such as growing attachment only appear over several turns. The course also requires custom tools, a skill folder, guardrails with a with/without comparison, and documented evaluation and data quality.

**Why not test real child products** (Curio, Buddy.ai)? Cost and time. A researcher can connect one later through the adapter. Our reusable contribution is the simulator, the judge and the method.

## Where each part is specified

| Part | RFC |
|---|---|
| Child simulator and orchestrator | [0002](0002-child-simulator-agent.md) |
| Judge, labeling and evaluation | [0003](0003-judge-and-evaluation.md) |
| Agent tools | [0004](0004-agentic-tools.md) |
| Scenarios and data quality | [0005](0005-scenarios-and-synthetic-data.md) |
| Guardrails | [0006](0006-safety-guardrails.md) |
| Skills | [0007](0007-skill-folder.md) |
| Situations and rubric | [0008](0008-referral-rules-and-rubric.md) |
| Stack, repository and site | [0009](0009-technology-stack-and-repo.md) |
| Current plan (takes priority where they differ) | [0010](0010-implementation-plan.md) |

## Design principles

- Every run records models, settings, seeds, rubric version and git commit.
- AI behaviour stays inside fixed rails: tools enforce limits, content levels and formats.
- The judge and child shouldn't share a model family with the tested chatbot where avoidable.
- Anything a researcher must change to test their own chatbot sits behind one adapter.
- Never filter or change the tested chatbot's replies.

## Owner roles

| # | Role | Responsibilities |
|---|---|---|
| 1 | Lead and client | Scope, schedule, client and TA communication |
| 2 | Scenarios and rubric | Scenarios, rubric, checking against the papers |
| 3 | Child simulator and orchestrator | Simulator, orchestrator, adapters |
| 4 | Judge and review | Judge, review queue, agreement |
| 5 | Site and poster | Site, poster |

## Alternatives considered

- **Static prompt set:** can't show multi-turn drift, and isn't agentic.
- **Human scoring only:** most valid, doesn't scale. Kept for checking the judge and for app chats.

## Milestones

| Date | Milestone |
|---|---|
| Nov 9 | Pilot run: 7 scenarios × 2 local chatbots × 2 seeds |
| Nov 11 | Pilot meeting with the client |
| Nov 26 | Judge and rubric frozen |
| Nov 29 | Human scoring and guardrail comparison done |
| Nov 30 | Final results; code and data freeze |
| Dec 1 | Poster done |
| Dec 1 or 2 | Meeting with the client |
| Dec 3 | Final poster presentation |
