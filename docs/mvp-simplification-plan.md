# MVP simplification plan

**Status:** active  
**Scope:** reduce maintenance and reporting risk without weakening the course requirements or the planned evaluation  
**Branch:** `codex/mvp-simplification`

## Goal

Ship one reliable ChildBench workflow whose results can be explained and audited. Prefer one supported path over several optional paths, honest empty states over sample results, and deterministic checks over unused model roles.

This is a maintenance plan, not a new RFC work package. RFC 0010 remains the source of truth for T2, B13, the real runs, human scoring, and the final freeze.

## MVP boundary

Keep:

- the CLI runner, resume support, and terminal manual relay;
- the scenario-authoring agent, its tools, and the `add-scenario` and `dev-work-package` skills;
- rubric v1, scoring profiles, deterministic ratings, and browser scoring controls;
- gold-set validation, human-label packets, judge evaluation, realism and script-fidelity checks;
- guardrails and the required guardrails-on/off comparison;
- the static results, scenario, transcript, judge-reliability, and scorer pages;
- the 15 final DIM scenarios and the small scenarios that are still test fixtures.

Do not remove evaluation evidence merely because its module is large. In particular, `evaluate.py`, `gold.py`, `labels.py`, `realism.py`, `export.py`, and the authoring and judge agents serve explicit requirements.

## Work order

### Phase 1 — reporting reliability

- [x] Remove invented leaderboard, scenario, transcript, and guardrail fallback data. With no export, show an explicit empty state and the command that creates data.
- [x] Correct stale public claims about review, age coverage, judge repetition, ensembles, and API runs.
- [x] Update `architecture.md` and `harness/README.md` to describe the implemented v1 workflow.
- [ ] Refuse real app runs with unreviewed scenarios after T1 supplies reviewer metadata.

This phase comes first because inaccurate public text can invalidate an otherwise correct result.

### Phase 2 — remove unused execution paths

- [x] Remove the optional browser Run application: `harness/app.py`, `site/run.html`, `site/assets/run.js`, `site/assets/run.css`, `tests/test_app.py`, and the `make app` target.
- [x] Keep `harness/llm/manual.py`, `make run`, terminal resume, `configs/app-sample.yaml`, the static scorer, and CLI label import/export.
- [x] Remove the direct `hf_local` backend and its heavy optional dependencies and documentation. Ollama remains supported through `openai_compat`.
- [x] Remove the disabled child-reviewer LLM role. Keep the content ceiling, speech lint, fake-data scan, key-fact checks, retries, fallback, and audit sampling.

Each removal should be a separate commit so it can be reverted independently.

### Phase 3 — repository housekeeping

- [x] Delete historical diagrams that `docs/diagrams/README.md` identifies as unused. Keep `childbench-concept.svg` and `childbench-relevance.svg`.
- [x] Remove obsolete `local.yaml` and `gemma-local.yaml` examples after confirming operations and gold evaluation use `pilot.yaml`, `full.yaml`, and the other focused configs.
- [x] Declare every directly imported runtime dependency explicitly.

### Phase 4 — after T2 and B13

- [ ] Archive the pilot artifacts that need rubric v0.
- [ ] Give gold evaluation a focused judge config if it still depends on `configs/pilot.yaml`.
- [ ] Make rubric v1 the only supported runtime and export format.
- [ ] Remove v0 branches and their compatibility tests from schemas, rating, summary, scoring, export, and the site.

Do not begin this phase before T2 and the judge freeze. It has the largest simplification payoff but also the highest regression risk.

## Deliberately deferred refactors

- Do not consolidate `_read_jsonl` and `resolve_run` helpers during the experiment critical path. Their error and path behavior differs slightly.
- Do not split large evaluation modules merely to reduce file length.
- Do not delete the seven pilot/example scenarios while tests and findings still use them; label them as fixtures instead.
- Do not add OpenRouter or another provider unless a planned run requires it.

## Acceptance checks

For every phase:

1. `make test` passes.
2. Run `make mock` whenever the runner, agents, model backends, tools, or configs change.
3. `make check` passes before a real run or publication.
4. No paid model is called as part of cleanup verification.
5. A site without `site/data/` shows no fabricated measurements or conversations.
6. A site with an export still renders the v1 rubric and exported results.
7. The tested chatbot's replies remain stored and displayed unchanged.

## Decision log

| Date | Decision | Reason |
|---|---|---|
| 2026-11-26 | Put this plan in `docs/`, not `RFCs/` | It records cross-cutting maintenance and does not change the benchmark design or create a new operation. |
| 2026-11-26 | Start with truthful presentation and documentation | Lowest regression risk and highest reporting-integrity benefit before T2. |
| 2026-11-26 | Defer v0 removal until after T2/B13 | Gold, pilot, export, and compatibility tests still reference v0. |
