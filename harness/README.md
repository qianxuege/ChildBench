# ChildBench harness

The current plan is [RFC 0010](../rfcs/0010-implementation-plan.md). New runs use rubric v1 and the turn-based scenario format. Rubric v0 remains only for pilot compatibility until after T2 and B13.

The backend that runs the benchmark: pretend child -> chatbot under test -> judge -> results.
Design: [architecture.md](../architecture.md) and [rfcs/](../rfcs/). The harness runs end to end; RFC 0010 tracks the remaining human operations and real runs.

## Run it

```bash
pip install -e ".[dev]"     # Python 3.11+
make test                   # unit tests
make mock                   # full run offline, no keys: mock child, 3 mock chatbots, mock judge
make labels OUT=<name>      # blind scoring packet from the latest run (FROM=<folder> for others)
make import-labels OUT=<name>  # check the scorers' CSVs and compute agreement
```

`make mock` prints a leaderboard and writes `results/<run_id>/`. The mock numbers mean nothing;
they only prove the wiring (the "careful" bot passes, the "eager" bot fails, the "cautious" bot
fails the controls).

## What's where

| Path | What |
|---|---|
| `harness/run.py` | Orchestrator: scenarios x chatbots x seeds, stop conditions, manifest, summary |
| `harness/agents/child_simulator.py` | The pretend child: a fixed loop run by code, not an agent. Generator -> content ceiling -> realism lint -> fake-data check -> key facts -> retry x3 -> fallback |
| `harness/agents/agent_under_test.py` | The chatbot being graded. Replies are never edited |
| `harness/agents/judge.py` | Judge agent: tool loop over load_rubric / verify_evidence_quote / submit_scores (see "How the judge works") |
| `harness/agents/toolloop.py` | Provider-neutral tool calling: the model replies `{"tool": ..., "args": ...}` |
| `harness/author.py` | Scenario-authoring agent (A7): tool loop over load_persona / validate_scenario / check_content_ceiling / lint_child_utterance / save_scenario_draft (see "How the authoring agent works") |
| `harness/tools/` | RFC 0004's tools: 9 that a model calls (their docstrings are what it reads), plus 3 helpers only code calls; `tools/__init__.py` lists which ones each agent is offered and which only code calls |
| `harness/rating.py` | pass / borderline / fail by fixed logic (RFC 0008) |
| `harness/llm/` | Backends: `mock`, `openai_compat` (Ollama, llama.cpp, LM Studio, Gemini, OpenAI), and `manual` |
| `harness/labels.py`, `validate_judge.py` | Blind scoring packet; check the scorers' CSVs, then kappa, missed cases and false alarms |
| `rubrics/v1.yaml` | DIM domains, judge codes, referral checks, caps, levels, and sources |
| `scenarios/dim/` | The 15 final scenario files adapted from the DIM cards |
| `scenarios/<situation>/*.yaml` | Earlier scenarios retained as test fixtures and examples |
| `configs/` | Run settings: mock, local, pilot, app sample and others |

## Results folder

`results/<run_id>/`: `transcripts.jsonl`, `scores.jsonl`, `summary.json` (leaderboard numbers),
`review_queue.jsonl`, `guardrail_log.jsonl` (every check, for the ablation), `judge_traces.jsonl`,
`tool_calls.jsonl`, `manifest.json` (git commit, model ids, seeds, rubric and scenario hashes).
`results/labeling/<name>/private/` (blind key, judge scores) is gitignored: never give it to scorers. Send only `send/`.

## Implementation decisions

- **Tool calling is JSON-in-text, not each provider's native API**, so Gemma, Gemini, Claude and
  GPT all work the same way. Native function calling can be added per backend later.
- **The stop condition is checked only after the last scripted beat**, so every scenario reaches
  its key moment even if the chatbot refers early. Then the child pushes back once (if
  `pushback_once`), and the run stops when the chatbot refers again.
- **The content ceiling is enforced even in shadow mode**: a too-intense line never reaches the
  chatbot (RFC 0002), so the ablation only relaxes the non-ceiling child-line checks.
- **Quotes are required only when a label says something happened** (yes on MUST, no on MUST_NOT).
  "No adult was mentioned" has nothing to quote.
- **S2.1 comes from `detect_pii_request`** and overrides the judge.
- Headline numbers in `summary.json` leave out transcripts with fallback lines, too-intense
  personas, judge failures or unsupported evidence; the `all` block keeps everything.

## How the judge works (rubric v1, B10)

- **One step is one model reply with one tool call.** The judge gets 14 steps. Each tool has a call limit
  (`verify_evidence_quote` 3, `load_rubric` 2, `flag_for_human_review` 3), and the last
  3 steps only accept `submit_scores`. The loop stops as soon as `submit_scores` returns ok; a judge that stops
  before that is told nothing is saved yet. On the gold tuning half no chat needed more than 7 steps.
- **Facts first, then levels.** The judge records what happened (secrecy asked, adult named, crisis line, ...)
  before giving each domain a 0–2 level with one line of reasoning, using the length guide for the child's age.
  A caregiver message is context only; scoring starts from the reply to the child's first line. Turns after
  the last scripted turn count for referral checks only.
- **Scenario guidance goes to the judge only.** From the scenario file: what a good and a bad reply look like,
  the referral expectation, trigger turn and adults, the last scripted turn, and which turn carries which code.
  The pretend child and the chatbot never see it. Gold chats have no scenario file, so `make gold` doesn't test
  the guidance; the report says so.
- **Code decides some things the judge used to.** `pushback` is `na` unless the child resisted, `promise` is `na`
  when nobody asked for secrecy (word checks in `harness/tools/secret.py`), and X7 and X8 come from the facts and
  codes (`harness/rating.py`).
- **Contradictions go back once.** Examples: X3 "yes" with no adult named, or a "promise" quote that promises
  nothing. The judge gets one chance to fix them; if it sends the same answer again, it is saved and queued
  as `CHECK_DISAGREE` for a person. Code never overrules the judge here.
- **The judge's only flag is `LOW_CONFIDENCE`.** Other review items (`CHECK_DISAGREE`, unverified quotes,
  `JUDGE_FAILED`) come from code.
- **Small format slips are read, not refused:** `"0"` for the number 0, a `null` quote, a rule that isn't for
  this situation (set aside and listed in `rules_set_aside`).
- **New fields in each record's `automatic_checks`:** `scenario_guidance` (true when the judge had a scenario
  file), `rules_set_aside`, `heuristic_secrecy_asked_turn`, `heuristic_secrecy_promise`, alongside
  `heuristic_referral_agrees`.

- **`detect_pii_request` isn't offered to the judge (A7).** Code runs it on every reply first, shows the judge
  the result, and `submit_scores` overrides S2.1 with it, so the judge's own calls added nothing.

## How the authoring agent works (A7)

- `make author IDEA="..." [SITUATION=...] [AGE=...] [MODEL=mock]` runs `harness/author.py` with the model in
  `configs/author.yaml` (`gemma4:e4b` on port 11502). The agent gets the idea, a free id, example scenario ids
  and `skills/add-scenario/template.yaml`, and picks its own tool calls until `save_scenario_draft` returns ok.
- `load_persona` returns only the child's side of an example (lines, style, age), never `referral`, `good`,
  `bad` or `must_say`.
- `validate_scenario` runs the `Persona` checks, the content ceiling and fake-data scan (`scenario_content_problems`),
  the speech check on every line, a unique id, and whether the referral expectation fits the situation. It
  returns short reasons and writes nothing. `save_scenario_draft` runs the same check, refuses ids already in
  `scenarios/` or `drafts/`, sets `review` itself (the agent as author, no reviewers, not client-approved), and
  writes only `drafts/scenarios/<situation>/<id>.yaml`.
- After 6 failed validations the tools refuse and the loop stops: nothing is saved.
- Each attempt's `tool_calls.jsonl` and `summary.json` (steps, validations, failures) go to `drafts/<draft id>/`.
  `drafts/` is gitignored and no config reads it. A person reviews a draft and moves it into `scenarios/`
  ([`skills/add-scenario`](../skills/add-scenario/SKILL.md)).

## Not done yet

The ordered list is in [RFC 0010](../rfcs/0010-implementation-plan.md#who-does-what-in-order).
