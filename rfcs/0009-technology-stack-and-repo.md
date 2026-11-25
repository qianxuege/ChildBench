# RFC 0009: Technology Stack, Repository and Site

- **Status:** Draft
- **Created:** 2026-10-26 (updated 2026-11-23)
- **Owner role:** 3. Child simulator and orchestrator (with 5 for the site)
- **Tech requirements addressed:** 2, 6
- **Related RFCs:** 0002, 0003, 0004, 0007, [0010](0010-implementation-plan.md)

## Summary

Plain Python, YAML and JSONL files, and a static HTML site. No hosted database or hosted server, so a researcher can clone the repo and repeat a run.

## Detailed design

**Repository layout and commands:** [RFC 0010, section 4](0010-implementation-plan.md#4-code-layout-and-commands).

### Stack

| Area | Choice | Why |
|---|---|---|
| Language | Python 3.11+, `make` commands | Model clients and statistics are all Python |
| Orchestration | Plain Python | The outer loop is fixed, so no AI or framework needs to decide it |
| Model access | One OpenAI-compatible client (Ollama, OpenRouter); local Hugging Face; `manual` relay; mock | Changing provider is a config change |
| Agent tools | Python functions with docstrings, called through JSON in text | Works the same on every model |
| Validation | Pydantic | Catches bad scenarios and malformed judge output |
| Config | YAML | Easy to edit and review |
| Results | JSONL per run, in the team Drive | Readable, append-only, not public |
| Statistics | scikit-learn (kappa), bootstrapped ranges | Standard implementations |
| Tests | pytest, mock models, no keys | Anyone can run them |
| Secrets | `.env`, gitignored | Keys never reach the site or the repo |
| Site | Plain HTML, CSS and JavaScript reading `site/data/` | No build step or hosted server |
| Diagrams | Mermaid source plus rendered SVG | Easy to edit, displays everywhere |

### Results and reproducibility

Each run writes `results/<run_id>/` with transcripts, scores, guardrail log, review queue and a `manifest.json`: git commit, config, model ids, seeds, and rubric and scenario hashes. Saved transcripts can be re-scored without calling any chatbot. Where they're kept: [RFC 0010, section 9](0010-implementation-plan.md#9-scoring-evaluation-and-results).

### Site

`make export` writes `site/data/`, after the publishing check in RFC 0006. Pages: overview, method, scenarios, case grid, transcripts and judge reliability. Researchers can change the scoring profile in the browser (RFC 0008). The site never asks for an API key.

**Running from the site:** `make app` adds a Run page on the researcher's own computer (`127.0.0.1`), using Python's standard library (RFC 0010, F5). The public site stays static.

### Data protection

Synthetic data only, fictional names, keys in `.env`. The gold set stays in `private/` (gitignored); the preprint's cards and quoted rubric text are published with credit (CC BY 4.0). Record app and provider privacy settings with each run.

## Alternatives considered

- **Hosted database (e.g. Supabase):** accounts and keys before anyone can repeat a run, for a small amount of data.
- **LangGraph:** more dependencies for a fixed loop.
- **MkDocs or React site:** more tooling than a few static pages need.

## Risks

- Check provider terms before publishing replies, and CHILDES terms before including any data.
- The preprint citation is still to be confirmed (RFC 0010, R10).
