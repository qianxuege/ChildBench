# RFC 0001: Project Plan and Key Decisions

- **Status:** Draft
- **Created:** 2026-10-25 (updated 2026-10-27)
- **Owner role:** 1. Lead and client
- **Tech requirements addressed:** 1 (overall approach); the per-requirement mapping is in [architecture.md](../architecture.md#6-how-the-courses-technical-requirements-are-met)
- **Related RFCs:** 0002 to 0009

## Summary

ChildBench evaluates child-facing AI agents on whether they involve a trusted adult when they should. We move from "prompt set plus scoring script" to a pipeline of two agents we build, a **child simulator** and a **judge**, that exercise and score an **agent under test** (ChatGPT, Claude, Gemini) across multi-turn conversations.

This RFC records the project-level decisions: why this approach, the design principles, who owns what, what the pilot is, and the timeline. **The system itself is described in [architecture.md](../architecture.md)** (pipeline diagram, each component, a worked example, and how each course requirement is met); details are in RFCs 0002 to 0009.

## Motivation

Assignment feedback on the first scope: the proposal read like a dataset and a scoring script, not an agentic workflow, and many target behaviours (e.g. slowly growing attachment) only appear over several turns. The course also requires five custom agentic tools, a skill folder, guardrails with a with/without comparison, and documented evaluation and data-quality methods.

### Why a simulator and judge instead of testing real products

We originally wanted to test real child-facing products such as [Curio](https://curio.academy/) and [Buddy.ai](https://buddy.ai/en/). Cost and time constraints make that impractical this term. Instead we build the child simulator and evaluation pipeline so that a future researcher can repeat our process on real child-facing agents by writing one adapter (see RFC 0007, `add-agent-adapter`). The benchmark's reusable contribution is therefore the **method and harness**, not only the prompt set. Real products are a stated non-goal for this term and a first-class future use.

## Detailed design

### Where each part is specified

| Part | RFC |
|---|---|
| Child simulator and orchestrator | [0002](0002-child-simulator-agent.md) |
| Judge, labeling, and evaluation methodology | [0003](0003-judge-and-evaluation.md) |
| Custom agentic tools | [0004](0004-agentic-tools.md) |
| Scenarios and synthetic-data quality | [0005](0005-scenarios-and-synthetic-data.md) |
| Safety guardrails and with/without comparison | [0006](0006-safety-guardrails.md) |
| Skill folder | [0007](0007-skill-folder.md) |
| Situations, rules, and rubric | [0008](0008-referral-rules-and-rubric.md) |
| Technology stack, repository, data flow, and site | [0009](0009-technology-stack-and-repo.md) |

### Design principles

- The harness is the unit of evaluation: every run records model versions, prompts, seeds, rubric version, and git commit so results are reproducible.
- Keep LLM behaviour inside deterministic rails: tools enforce turn limits, content ceilings, and output schemas.
- Judge and simulator should not share a model family with the agent under test where avoidable, to limit self-preference bias.
- Anything a researcher must change to test their own agent lives behind one adapter interface.
- Never filter or alter the agent under test's replies; its behaviour is what we score.

### Owner roles

The "Owner role" in each RFC header is the team role responsible for writing and maintaining it. The roles come from the project plan in the feedback document. The options are:

| # | Role | Typical responsibilities |
|---|---|---|
| 1 | Lead and client | Scope, schedule, Ran and Yingtao communication, weekly check-ins, rehearsals, handoff |
| 2 | Scenarios and rubric | Persona template, scenarios, rubric criteria and rules, checking against Ran's table |
| 3 | Child simulator and orchestrator | API keys, simulator, orchestrator, adapters for agents under test |
| 4 | Judge and review | Judge prompts, review queue, judge-vs-human agreement |
| 5 | Site, report, deck | Public site, scenario browser, leaderboard, report and final deck |
| Everyone | Shared work | Writing personas for one referral situation each; labeling transcripts |

An RFC can name two roles (RFC 0004 is shared by 3 and 4). Replace role numbers with names once assigned.

## Alternatives considered

- **Static prompt set only** (DevBench / MinorBench style): cheap, but cannot capture multi-turn drift and is not agentic.
- **Fully human-scored:** most valid, not scalable; kept as the audit layer instead.
- **Test real products (Curio, Buddy.ai) directly:** highest ecological validity; rejected for this term on cost and time, kept as future work through the adapter interface.

## Evaluation / how we know it works

See 0002 (simulator realism) and 0003 (judge agreement). End-to-end acceptance test: an outside researcher can run one command with their agent adapter and get a scored report.

## Pilot definition

The pilot is the first end-to-end run, finished by **Nov 12**:

- Pipeline coded: orchestrator, simulator, 2 agent adapters, judge, review queue, tools.
- About 10 scenarios (2 per situation), 2 agents under test, 2 seeds each.
- About 20 transcripts labeled by two teammates; preliminary judge-vs-human kappa (with wide uncertainty, stated as such).
- Meeting with Ran on Nov 12 to discuss preliminary results and decide rubric wording, content level, and expansion.

## Risks and open questions

- Simulator quality bounds benchmark validity (see 0002).
- API cost: need a budget estimate per full run. Ran has Gemini credit and possibly Claude access; no GPT credit.
- Does Ran want the benchmark to support agents with tool use, or chat only? **Ask Ran.**
- Ran's HAI '26 paper is presented Nov 16-19 in Osaka, so she may be unavailable that week. Plan Ran touchpoints for Nov 12 and Nov 22.

## Milestones

| Date | Milestone |
|---|---|
| Oct 26 - Nov 1 | Define: persona template, rubric v0 (RFC 0008), repo skeleton, API keys, hello-world loop. Progress update with Ran. |
| Nov 2 - Nov 8 | Build: orchestrator, simulator, judge, tools, adapters, pilot scenarios. |
| Nov 9 - Nov 11 | Pilot run, testing, labeling ~20 transcripts, one-page preliminary results. |
| **Nov 12** | **Pilot complete. Meet Ran with preliminary results.** |
| Nov 13 - Nov 22 | Scale to ~50 scenarios, full run, guardrail ablation, judge validation. Send sample to Ran Nov 22. |
| Nov 23 - Nov 29 | Thanksgiving week (Nov 26): no new runs; site, leaderboard, writing. |
| Nov 30 - Dec 2 | Code and data freeze, final rerun, rehearsals. |
| Dec 3 | Final presentation. |
