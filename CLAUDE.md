# ChildBench: notes for Claude Code

Benchmark of how chatbots respond to children: does the chatbot refer the child to a trusted
adult at the right moments (and not otherwise)? Team 20, CMU Intro to AI. Never name the client in this repo; citing the papers is fine.
Framework: An, Chung & Libertus, HAI '26 ("When Should Agents Call for a Grown-Up?"), five
referral situations S1-S5 plus controls, and the DIM preprint's scoring sheet. Final poster presentation Dec 3.
**Current plan: `rfcs/0010-implementation-plan.md`** (wins where docs differ).

## Read first
- `architecture.md` (plain-language overview), `rfcs/0002` (child simulator), `0003` (judge),
  `0004` (tools), `0006` (guardrails), `0008` (rules and rubric), `0009` (stack).
- `harness/README.md`: how to run, what's built, decisions taken, what's not done yet.

## Commands
- `make test` (pytest), `make mock` (offline end-to-end run), `make check` (validate scenarios), `make labels` and `make import-labels` (human scoring), `make local` (free local models, see README), `make site` (website on port 11510).
- One scenario only: `python -m harness.run --config configs/mock.yaml --only dist-003 --seeds 1`.

## Rules that must not be broken
- Never edit, filter or "fix" the chatbot-under-test's replies. Guardrails apply to OUR pretend
  child and judge only; the chatbot's age-appropriateness is scored (X4, X5), not enforced.
- A pretend-child line above the scenario's content level never reaches the chatbot, in any mode.
- The overall rating is computed by `harness/rating.py`, never chosen by the judge.
- `detect_pii_request` overrides the judge on S2.1.
- No real child data, real names, or realistic phone numbers/addresses in scenarios.
- API keys only in `.env` (gitignored). `results/labeling/*/private/` (`blind_key.csv`, judge scores) is never shared with scorers: send them only `send/`.
- Agent-facing tool docstrings follow the RFC 0004 convention (purpose, When to use, When NOT to use,
  Args, Returns). Tools return `{"ok": false, "error": ...}` instead of raising.
- Every rubric rule keeps a source tag ([paper], [scope], [DIM], [ext]); [ext] rules need the client's review.

## Open questions waiting on the client
See `rfcs/0010-implementation-plan.md`, "Questions for the client".

## Models
Gemma 3 270M (in Adam's Drive, HF safetensors format) is only usable as a weak chatbot under test.
Use Gemma 4B+ (Ollama) or Gemini for child generator, reviewer and judge. Prefer a
judge from a different model family than the chatbot being scored.
