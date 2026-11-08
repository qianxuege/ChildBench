# ChildBench harness (baseline)

The backend that runs the benchmark: pretend child -> chatbot under test -> judge -> results.
Design: [architecture.md](../architecture.md) and [rfcs/](../rfcs/). This is a **baseline**: it runs
end to end and every RFC component exists, but several pieces use placeholders (see "Not done yet").

## Run it

```bash
pip install -e ".[dev]"     # Python 3.11+
make test                   # 22 tests
make mock                   # full run offline, no keys: mock child, 3 mock chatbots, mock judge
make labels                 # blind label sheets for the latest run
```

`make mock` prints a leaderboard and writes `results/<run_id>/`. The mock numbers mean nothing;
they only prove the wiring (the "careful" bot passes, the "eager" bot fails, the "cautious" bot
fails the controls).

### With Gemma on your machine

1. Install [Ollama](https://ollama.com), then `ollama pull gemma3:270m` and a bigger one for
   the judge and child, e.g. `ollama pull gemma3:4b` and `gemma3:12b`.
2. `make gemma` (uses `configs/gemma-local.yaml`).

Or run the `gemma-3-270m-it` folder from Drive directly: download the whole folder to
`models/gemma-3-270m-it/`, `pip install -e ".[local]"`, and use
`{backend: hf_local, path: models/gemma-3-270m-it}` in the config.

**270M is too small to be the judge or the pretend child.** It ignores the JSON tool protocol,
so its transcripts end up `JUDGE_FAILED` in the review queue. Use it as a weak chatbot under test;
use Gemma 4B+ or Gemini (Ran's credit, `GEMINI_API_KEY` in `.env`) for the other roles.

## What's where

| Path | What |
|---|---|
| `harness/run.py` | Orchestrator: scenarios x chatbots x seeds, stop conditions, manifest, summary |
| `harness/agents/child_simulator.py` | Generator -> content ceiling -> realism lint -> fake-data check -> reviewer -> retry x3 -> fallback |
| `harness/agents/agent_under_test.py` | The chatbot being graded. Replies are never edited |
| `harness/agents/judge.py` | Judge agent: tool loop over load_rubric / verify_evidence_quote / submit_scores |
| `harness/agents/toolloop.py` | Provider-neutral tool calling: the model replies `{"tool": ..., "args": ...}` |
| `harness/tools/` | The ten RFC 0004 tools. Their docstrings are what the agents read |
| `harness/rating.py` | pass / borderline / fail by fixed logic (RFC 0008) |
| `harness/llm/` | Backends: `mock`, `openai_compat` (Ollama, llama.cpp, LM Studio, Gemini, OpenAI), `hf_local` |
| `harness/labels.py`, `validate_judge.py` | Blind label sheets; kappa per rule between two sheets |
| `rubrics/v0.yaml` | Rules from RFC 0008 with levels and sources |
| `scenarios/<situation>/*.yaml` | The 7 draft scenarios (no second reviewer yet) |
| `configs/` | `mock.yaml`, `gemma-local.yaml`, `local.yaml` |

## Results folder

`results/<run_id>/`: `transcripts.jsonl`, `scores.jsonl`, `summary.json` (leaderboard numbers),
`review_queue.jsonl`, `guardrail_log.jsonl` (every check, for the ablation), `judge_traces.jsonl`,
`tool_calls.jsonl`, `manifest.json` (git commit, model ids, seeds, rubric and scenario hashes).
`labeling/blind_key.csv` is gitignored: never give it to labelers.

## Decisions made in this baseline (change freely)

- **Tool calling is JSON-in-text, not each provider's native API**, so Gemma, Gemini, Claude and
  GPT all work the same way. Native function calling can be added per backend later.
- **The stop condition is checked only after the last scripted beat**, so every scenario reaches
  its key moment even if the chatbot refers early. Then the child pushes back once (if
  `pushback_once`), and the run stops when the chatbot refers again.
- **The content ceiling is enforced even in shadow mode**: a too-intense line never reaches the
  chatbot (RFC 0002), so the ablation's arm A only relaxes the realism and reviewer checks.
- **Quotes are required only when a label says something happened** (yes on MUST, no on MUST_NOT).
  "No adult was mentioned" has nothing to quote.
- **S2.1 comes from `detect_pii_request`** and overrides the judge.
- Headline numbers in `summary.json` leave out transcripts with fallback lines, too-intense
  personas, judge failures or unsupported evidence; the `all` block keeps everything.

## Not done yet (in rough priority order for the Nov 12 pilot)

1. **Calibrate `lint_child_utterance` from CHILDES** (RFC 0002). Limits and the adult-word list
   are placeholders; results say `"calibrated": false`.
2. **Try the judge with a capable model** on hand-written transcripts, and add anchor examples to the rubric.
3. **Second reviewer on every scenario**, then set `allow_unreviewed: false`.
4. Publishing scan (run `check_content_ceiling` on every turn before the site shows it).
5. Site export from `summary.json`, judge test-retest (3 runs), length-bias check, two-judge option.
6. Free-app spot check through a manual relay (`backend: manual`): the harness hands each checked child line to a person to paste into the app, and reads the reply back from the clipboard. This replaces the paste-in import idea. See RFC 0010, section 4 and work package A11.
7. `skills/` folder (RFC 0007), GitHub Actions, SQLite build, async parallel runs.
