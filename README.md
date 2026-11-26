# ChildBench

**Does a chatbot bring in a trusted adult when a child needs one, and simply help when they don't?**

ChildBench is an open benchmark that plays short conversations between a simulated child and a chatbot, scores the chatbot's replies, and reports how far those scores can be trusted.

Team 20, Carnegie Mellon University, Introduction to AI, Fall 2026.

> **Status, Nov 25, 2026: in development.** The pipeline runs end to end and a pilot is done. The final scenarios, rubric and judge checks are being built, and the full run is due Nov 27–30. **There are no results worth citing yet.** Parts marked *planned* aren't built. [RFC 0010](rfcs/0010-implementation-plan.md#who-does-what-in-order) has the order, owners and dates.

**Contents:** [Concept](#core-concept) · [Why it matters](#why-it-matters) · [Scope](#project-scope) · [How it works](#how-it-works) · [Technical requirements](#technical-requirements) · [Pilot findings](#what-we-know-so-far) · [Run it](#run-it) · [Responsible AI](#responsible-ai) · [What's next](#whats-next) · [Authors](#authors) · [References](#references)

## Core concept

<img src="docs/diagrams/childbench-concept.svg" alt="ChildBench pipeline: scenarios go to a pretend child, which talks with the chatbot under test; the transcript goes to the judge and to human scorers, and then to results" width="820" style="max-width: 100%; height: auto;">

A model plays the **pretend child**: it says each scripted line in its own words, in a fixed loop that code runs and checks. Two AI agents choose their own tool calls: the **judge** scores the finished conversation and must quote evidence, and the **scenario-authoring agent** drafts new scenarios for a person to review. Plain code runs the steps, enforces the checks and computes the scores, and people check the judge.

## Why it matters

<img src="docs/diagrams/childbench-relevance.svg" alt="A chatbot can fail a child in two ways: too little referral and too much escalation. ChildBench measures both, for developers, researchers and child-safety reviewers" width="820" style="max-width: 100%; height: auto;">

Children use general chatbots for homework, games and secrets. Most child-safety tests ask whether a chatbot blocks harmful text. That misses two failures: **keeping a child's problem to itself**, and **sending the child to a hotline over a bad day**.

| Who | What ChildBench lets them do |
|---|---|
| **AI product teams** | Find these failures before release, and see whether a change helped |
| **Researchers** | Repeat a run, change the scoring, and add scenarios |
| **Schools, parents and policy reviewers** | Read evidence about behaviour instead of a marketing claim |

Regulators are asking too: the U.S. FTC has ordered seven companies that offer AI companions to explain how they test and monitor effects on children (An et al., 2026a).

## Project scope

**In scope:** English text chats of a few turns between one simulated child (ages 6–12 in the final scenarios) and one chatbot, scored for developmental fit and for referral to a trusted adult.

| | Included |
|---|---|
| **Scenarios** | 15 adapted from the preprint's released cards (An et al., 2026a): five referral situations, two controls and everyday moments *(planned)* |
| **Chatbots** | Free ChatGPT, Claude and Gemini apps on 8 scenarios, 24 chats; one local model on all 15 |
| **Scoring** | A four-domain sheet (0–2 each) on every chat; 12 referral checks on the five referral situations *(planned)* |
| **Evidence** | Judge vs 48 human-scored chats, two human scorers on all 24 app chats, guardrails on vs off |

**Out of scope:** voice, images, real children or their data, severe content, real children's products, fine-tuning any model, a single overall score or ranking, and proof of any product's safety.

The five referral situations come from An et al. (2026b): a request beyond the chatbot's ability, personal data, isolation or over-attachment, a distressing disclosure, and an unsafe or consequential request.

## How it works

1. **Scenario.** A file lists the child's age, lines, key facts, and what a good and a bad reply look like. The child and the chatbot never see the good and bad parts.
2. **Pretend child.** It says each line in its own words. Each line is checked for content level, age-appropriate speech and realistic personal data, and *(planned)* for the key facts. After 3 failed tries the scripted line is used and flagged.
3. **Conversation.** Apps are reached by a person copying and pasting each checked line and reply (the relay). Local models are called directly.
4. **Judge.** It records facts with exact quotes. Code rejects invented quotes, applies fixed rules and computes the rating.
5. **People.** Two scorers rate the app chats without seeing the chatbot's name or the judge's scores *(planned)*. We report weighted kappa (agreement beyond chance), missed cases and false alarms.
6. **Results.** Scores and transcripts are exported to a static site after a publishing check. App and local results stay separate.

Full explanation: [architecture.md](architecture.md). Design decisions: [rfcs/](rfcs/).

| Path | What |
|---|---|
| [`harness/`](harness/README.md) | Orchestrator, agents, tools, scoring |
| `scenarios/`, `rubrics/`, `configs/` | Scripts, checklists, run settings |
| `site/` | Website |
| [`docs/findings.md`](docs/findings.md) | What we ran and learned, dated |
| [`docs/related-benchmarks.md`](docs/related-benchmarks.md) | How other benchmarks compare |

## Technical requirements

| # | Requirement | How we meet it | Status |
|---|---|---|---|
| 1 | Agentic workflow | The judge and the scenario-authoring agent each run a tool loop in which the model picks the next tool. The pretend child is a fixed loop run by code, not an agent | Done |
| 2 | Pretrained models | Gemma and Qwen through Ollama; nothing is trained | Done |
| 3 | At least 5 custom tools, documented for agents | 9 tools that a model calls, each with an agent-facing docstring ([RFC 0004](rfcs/0004-agentic-tools.md)): the judge's 4 (`load_rubric`, `verify_evidence_quote`, `submit_scores`, `flag_for_human_review`) and the authoring agent's 5 (`load_persona`, `validate_scenario`, `check_content_ceiling`, `lint_child_utterance`, `save_scenario_draft`). Three more are helpers only code calls, and aren't counted | Done |
| 4 | Skill folder used by the team or made for the client | [`dev-work-package`](.claude/skills/dev-work-package/SKILL.md), which takes a work package from plan to pull request; [`add-scenario`](skills/add-scenario/SKILL.md) for the client, which runs the authoring agent and walks a person through reviewing its draft | Done |
| 5 | Evaluation | Judge vs gold set, human vs human, tool accuracy, child realism and script fidelity | *Planned* |
| 6 | Data protection and simulated-data quality | Synthetic data only, fake-data and content checks; key-fact checks and scenario reviews | Partly done |
| 7 | Safety guardrails | Content, speech, fake-data, quote and publishing checks ([RFC 0006](rfcs/0006-safety-guardrails.md)) | Done; key-fact check *planned* |
| 7.1 | With vs without guardrails | The same 15 scenarios, guardrails on and off: counts and side-by-side examples | *Planned* (Nov 28–29) |

**About 7.1.** Guardrails apply to our agents, never to the chatbot's replies. "Off" means failed checks are logged but not blocked, so both runs are measured the same way. The content limit stays on in both. With 15 pairs we report counts, not significance.

## What we know so far

From the pilot: 7 scenarios × 2 small local models × 2 seeds, 28 conversations, plus one complete free-app chat. These are **leads, not results**, because the judge wasn't checked against people.

- **Secrecy:** in one scenario, once the child asked for secrecy, neither local model pointed the child to an adult ([F-005](docs/findings.md)).
- **Over-attachment:** one ChatGPT chat stayed warm as a "bestie", never said it was an AI, and gave no concrete step to an adult ([F-007](docs/findings.md)).
- **Our judge made mistakes:** 4 of 28 chats went unscored, and it scored rules that didn't apply ([F-004](docs/findings.md)). That is why judge accuracy is our main focus.
- **Our own checks had bugs:** "I won't tell Mommy" counted as a referral ([F-008](docs/findings.md)), and a timeout was scored as a chatbot failure ([F-009](docs/findings.md)). Both fixes are *planned*.

## Run it

Needs Python 3.11+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

make test    # unit tests
make check   # validate scenarios and rubric
make mock    # full run offline, no keys; its numbers mean nothing
make site    # the public website, at http://127.0.0.1:11510
make app     # the same website plus the Run page, on this computer only
```

**Website and app.** There is one website, the files in `site/`, served two ways:

- `make site` serves it as static files, exactly as the public sees it. It shows what `make export RUN=<id>` wrote to `site/data/`, and the Run page only shows setup steps. Use it to check the site before deploying.
- `make app` serves the same pages plus a small API on `127.0.0.1` (port 11511, or `make app PORT=<n>`). It adds the **Run page**, to relay chats to a free chatbot app from the browser instead of the terminal, and controls on the **Score page**, to make a scoring packet, download `score.html` for a scorer and import the CSVs that come back. It prints a link with a one-time token; open that link, since the token changes each start. Ctrl+C stops it. Use it for your own work; every public page works the same under it.

The app is never hosted. It reads `results/` (including the private scoring folders) and talks to local models, so it stays on your own computer. It only offers configs whose chatbots all use `backend: manual` with guardrails on, such as `configs/app-sample.yaml`, whose pretend child and judge need the local models below (ports 11502 and 11503). A queue stopped with **Stop and save** resumes where it left off; so does `python -m harness.run --resume <run_id>` in the terminal.

**With local models:** install [Ollama](https://ollama.com). Tested on a Mac with 24 GB of memory.

```bash
ollama serve &      # any one server, just for the downloads; stop it afterwards
ollama pull gemma3:270m && ollama pull gemma4:e4b && ollama pull qwen3.5:9b

export OLLAMA_KEEP_ALIVE=30s OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_HOST=127.0.0.1:11501 OLLAMA_CONTEXT_LENGTH=4096  ollama serve &
OLLAMA_HOST=127.0.0.1:11502 OLLAMA_CONTEXT_LENGTH=8192  ollama serve &
OLLAMA_HOST=127.0.0.1:11503 OLLAMA_CONTEXT_LENGTH=16384 ollama serve &

make local
```

| Port | Model | Role |
|---|---|---|
| 11501 | `gemma3:270m` | Weak chatbot under test |
| 11502 | `gemma4:e4b` | Pretend child; stronger chatbot under test |
| 11503 | `qwen3.5:9b` | Judge |

The short keep-alive stops the two large models from being in memory together. Thinking is off for both, because it used up the token limit and gave empty replies.

**New scenarios:** `make author IDEA="..." [SITUATION=...] [AGE=...]` has the authoring agent draft one into `drafts/` (Ollama port 11502; `MODEL=mock` offline). No run reads `drafts/`: a person reviews the draft and moves it into `scenarios/`, as [`add-scenario`](skills/add-scenario/SKILL.md) explains.

**With a free app:** `make run CONFIG=configs/app-sample.yaml` shows each child line, copies it for you, and reads the reply back. Use a private chat, paste only the lines shown, and never edit replies. Details: [RFC 0010, section 7](rfcs/0010-implementation-plan.md#7-chatbots-api-and-app-and-the-manual-relay).

## Responsible AI

| Risk | What we do | What remains |
|---|---|---|
| **Harm to children** | No children take part and no real child data is used; all names and details are invented | A simulated child isn't a real one |
| **Upsetting content** | Mild content only; a line above the limit never reaches a chatbot | Filters use word patterns and can miss things |
| **Privacy** | Keys stay in `.env`; app chats use private modes and contain nothing personal | Providers may still keep or review chats |
| **An unreliable judge** | Evidence quotes, code-computed scores, a held-out test, blind human scoring | Small samples give wide ranges |
| **Bias** | Judge from a different model family than the chatbot where possible; two human scorers | English only; norms come from one research tradition |
| **Rewarding "ask a grown-up" every time** | Controls and an over-escalation code | Only a few controls |
| **Over-claiming** | Sample sizes shown; no ranking, pass mark or single score | Scores could still be quoted as a safety claim. They aren't one |
| **Teaching to the test** | The gold set stays out of the repo | Public scenarios and rubrics can be trained on |
| **Publishing harm** | Every turn is scanned; guardrails-off text is never published | Chatbot replies fall under each provider's terms |

A referral isn't always to a parent: scenarios list suitable adults, because a parent can be the source of harm.

## What's next

**Open concerns**

- Will the judge agree with people well enough? If not, human scores carry the results and we report the judge as it is.
- The rubric's referral checks, default weights and severity scale need expert review.
- One chat per app and scenario gives examples, not rates, and apps change without notice.
- Our scenarios mostly replace the caregiver's message with the child's age, so scores aren't directly comparable with the preprint's.
- Child speech limits are hand-set. We compare with real speech where the data covers the age, and no one rates how childlike the lines sound.
- Comparing apps with a local model can't separate the effect of the model, the system prompt and the safety layers.

**Future work:** more scenarios and controls, especially for the referral situations; repeated runs; voice; other languages and cultures; real children's products through the adapter; the same model through an app and an API; a scenario builder; human ratings of the simulated child.

## Authors

| Name | GitHub | Contribution |
|---|---|---|
| Qianxue (Cici) Ge | [@qianxuege](https://github.com/qianxuege) | Harness, judge, site, guardrail and local runs, final evaluation |
| Adam Wang | [@A1dam2W](https://github.com/A1dam2W) | Initial frontend and backend scaffolding; system trial, app conversations, expert meeting |
| Collin Guo | [@collingu-eng](https://github.com/collingu-eng) | Human scorer A |
| Tina Joshi | [@tina1899](https://github.com/tina1899) | Human scorer B; scenario drafts |
| Tianyou Lai | [@Laitty](https://github.com/Laitty) | Export, poster, guardrail analysis |

## References

- An, R., Chung, T., & Libertus, K. (2026a). *When machines join families: A developmental interaction model for evaluating child-facing AI* [Preprint]. PsyArXiv. https://doi.org/10.31234/osf.io/9f57g_v1
- An, R., Chung, T., & Libertus, K. (2026b). When should agents call for a grown-up? A developmental account of child-facing AI. In *Proceedings of the 14th International Conference on Human-Agent Interaction* (pp. 768–770). ACM. https://doi.org/10.1145/3841580.3845768
- Cohen, J. (1968). Weighted kappa: Nominal scale agreement provision for scaled disagreement or partial credit. *Psychological Bulletin, 70*(4), 213–220. https://doi.org/10.1037/h0026256
- Jiao, J., Afroogh, S., Chen, K., Murali, A., Atkinson, D., & Dhurandhar, A. (2025). *Safe-Child-LLM: A developmental benchmark for evaluating LLM safety in child-LLM interactions* [Preprint]. arXiv. https://arxiv.org/abs/2506.13510
- Khoo, S., Chua, G., & Shong, R. (2025). *MinorBench: A hand-built benchmark for content-based risks for children* [Preprint]. arXiv. https://arxiv.org/abs/2503.10242
- Kuperman, V., Stadthagen-Gonzalez, H., & Brysbaert, M. (2012). Age-of-acquisition ratings for 30,000 English words. *Behavior Research Methods, 44*(4), 978–990. https://doi.org/10.3758/s13428-012-0210-4
- MacWhinney, B. (2000). *The CHILDES project: Tools for analyzing talk* (3rd ed.). Lawrence Erlbaum Associates.
- Murali, A., Afroogh, S., Chen, K., Atkinson, D., Dhurandhar, A., & Jiao, J. (2025). *Evaluating LLM safety across child development stages: A simulated agent approach* [Preprint]. arXiv. https://arxiv.org/abs/2510.05484

Scenarios and quoted rubric text are adapted from An et al. (2026a), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). We removed most caregiver messages, added the child's age, and let the simulated child reword its lines.
