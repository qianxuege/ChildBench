# Related child-safety benchmarks

Notes from reading other public benchmarks, checked on 2026-11-06. This is background for team discussion.

**Nothing here has been adopted.** No rule, weight or scenario in this repo was taken from these benchmarks on this branch. If we do borrow something, record it in the table at the end and cite the source. Rubric rules also need a source tag, as `CLAUDE.md` requires.

## At a glance

| | ChildSafe | MinorBench | Safe-Child-LLM | KORA | ChildBench |
|---|---|---|---|---|---|
| **Tests for** | Nine safety dimensions | Refusing unsafe queries | Resisting jailbreaks, ethical refusal | Child-specific risks | Referring to an adult at the right moment |
| **Ages** | 6–8, 9–11, 12–14, 15–17 | Framed around a 12-year-old | 7–12, 13–17 | Not found | Proposed: kindergarten, primary, grades 6–12 |
| **Format** | Multi-turn, simulated child | Single prompts, 299 of them | 200 adversarial prompts | Multi-turn, simulated child | Multi-turn, scripted pretend child |
| **Scored by** | Word filter, then GPT-4o | Claude 3.5 Haiku, refusal or not | Human annotators, 0–5 scale | AI judge | AI judge plus human audit |
| **Aggregate** | Age-weighted average | None, unweighted rates | Not found | Letter grades, formula not found | None, rates per situation |

## ChildSafe

Murali et al., *Evaluating LLM Safety Across Child Development Stages: A Simulated Agent Approach*, 2025. [arXiv:2510.05484](https://arxiv.org/abs/2510.05484)

- Simulated child agents at four stages, built by constraining parameters such as volatility and context horizon rather than using static personas.
- Checked against real child language from CHILDES. Our RFC 0002 plans a similar check.
- Two scoring stages: an age-specific list of banned words scores 0 straight away, then GPT-4o scores each dimension from 0 to 1 against a rubric.
- The dimensions are content appropriateness, boundary respect, educational impact, social influence, emotional safety, privacy protection, manipulation resistance, developmental sensitivity and long-term impact.
- Reports up to 11.5% worse performance with early-childhood agents than with standard baselines.

## MinorBench

Khoo, Chua & Shong, *MinorBench: A hand-built benchmark for content-based risks for children*, 2025. [arXiv:2503.10242](https://arxiv.org/abs/2503.10242)

- Started from a case study of a chatbot in a middle school.
- 299 queries across six categories: danger, sexual, profanities, hateful, self-harm and substance use.
- Tests four system prompts, adding guidance step by step, which showed that safety varies a lot with the prompt. This could matter if we test chatbots with child-focused system prompts.
- Reports simple refusal rates with bootstrapped 95% intervals and no composite score. Our RFC 0003 also uses bootstrapped intervals, for agreement.
- It measures refusal rather than safety. A plain refusal would not pass our rubric, which requires pointing the child to someone.

## Safe-Child-LLM

Jiao et al., *Safe-Child-LLM: A Developmental Benchmark for Evaluating LLM Safety in Child-LLM Interactions*, 2025. [arXiv:2506.13510](https://arxiv.org/abs/2506.13510)

- 200 adversarial prompts from red-teaming sets such as SG-Bench and HarmBench, split across ages 7–12 and 13–17.
- Humans label whether each jailbreak succeeded, and rate responses on a 0–5 ethical refusal scale.
- Code and benchmark are public, according to the paper.
- We couldn't confirm how scores are aggregated, because only the abstract page was readable.

## KORA

[korabench.ai](https://korabench.ai), an independent, non-profit, open-source benchmark.

- Covers 26 child-specific risks according to its site. An earlier write-up says 24, so the number has moved.
- Two tracks: Models (V1.2) and Apps (V1.0). Apps tests consumer products including their system prompts and filters.
- According to its write-ups, an AI plays the child across conversations of three or eight turns, an AI judge grades them, and results come out as grades.
- We didn't find its weighting formula, age bands or license. Check its methodology page and GitHub repo before relying on this.

Sources beyond the site: [KORA on testing 32 models](https://korabench.substack.com/p/we-tested-32-ai-models-for-child), [KORA on testing apps](https://korabench.substack.com/p/we-evaluated-the-ai-apps-kids-use), [EdTech Innovation Hub coverage](https://www.edtechinnovationhub.com/news/kora-benchmark-finds-no-significant-safety-difference-between-child-and-adult-ai-modes).

## What ChildBench does differently

- It scores **referral**, meaning handing off to an adult at the right moment, rather than content safety or refusal.
- Control scenarios catch chatbots that send every child to an adult. A benchmark that rewards refusing wouldn't notice this.
- Human labels audit the judge, with agreement published per rule.

These differences come from reading the sources above, not from a full literature review.

## For the weights discussion

Today ChildBench has no weights. A broken MUST or MUST NOT fails the conversation. See the [README](../README.md#weights-not-decided).

The closest example is **ChildSafe's age-weighted average**. Each dimension gets a score from 0 to 1 and a weight that depends on age, and the total is the weighted mean. Its weights shift with age: emotional safety is heaviest for ages 6–8 (0.25, falling to 0.05 at 15–17), and manipulation resistance is heaviest for ages 15–17 (0.25, up from 0.05). The full table is in the paper.

Options to compare:

| Option | Gain | Risk |
|---|---|---|
| Keep fixed logic, no weights | Simple, and matches RFC 0008 | One rule counts as much as any other within a level |
| Weight only a summary score, and keep the fixed rating | Gives a single number while keeping the failure rule | A headline number can hide what failed |
| Weight rules by age, like ChildSafe | Reflects that priorities change with age | Weights need a defensible source |

Questions to settle first:

1. **Should averaging be able to hide a serious failure?** Under a weighted average, a chatbot that promises to keep a secret (X7) could still score well on everything else. Fixed logic doesn't allow that today.
2. **Where would the weights come from?** ChildSafe's table is built for its own nine dimensions and doesn't map directly onto our rules. Ours would need Ran's input and a source tag.
3. **Do weights vary by age group?** That depends on the scenario plan.
4. **Does the site change?** It currently says there is no points total.

## Anything we borrow

| Date | What | From | Where it's used | Agreed by |
|---|---|---|---|---|
| | | | | |
