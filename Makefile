.PHONY: install test mock gemma local check labels import-labels site run estimate export score gold realism evaluate

install:
	pip install -e ".[dev]"

test:
	python -m pytest -q

# Full pipeline offline, no API keys: mock child, mock chatbots, mock judge.
mock:
	python -m harness.run --config configs/mock.yaml

# Gemma served locally through an OpenAI-compatible server (Ollama / llama.cpp / LM Studio).
gemma:
	python -m harness.run --config configs/gemma-local.yaml

# Free models on this machine, one Ollama server per port (see configs/local.yaml).
local:
	python -m harness.run --config configs/local.yaml

# Validate every scenario file (format, content level, limits) and the rubric without running anything.
check:
	python -m harness.run --config configs/check.yaml --check-only

# Blind scoring packet for two people, in results/labeling/<OUT>/: make labels [FROM=<folder of runs>] [OUT=<name>]
# Send only send/. Default: the latest run, every chat scored by both.
labels:
	python -m harness.labels $(if $(FROM),--from "$(FROM)") $(if $(OUT),--out "$(OUT)")

# Check the CSVs in results/labeling/<OUT>/returned/ and compute agreement: make import-labels OUT=<name>
# SCORES=original reads each run's own scores.jsonl now, and SCORES=<label> its rescore_<label>/, instead of the copy made with the packet.
import-labels:
	@test -n "$(OUT)" || (echo "usage: make import-labels OUT=<name>" && exit 1)
	python -m harness.validate_judge "$(OUT)" $(if $(SCORES),--scores "$(SCORES)")

# Public website, static files only: http://127.0.0.1:11510
site:
	python -m http.server 11510 --bind 127.0.0.1 --directory site

# A real run from any config. Extra flags go in ARGS, e.g. `make run CONFIG=configs/pilot.yaml ARGS="--only dist-003 --seeds 1"`.
run:
	@test -n "$(CONFIG)" || (echo "usage: make run CONFIG=configs/pilot.yaml [ARGS=...]" && exit 1)
	python -m harness.run --config $(CONFIG) $(ARGS)

# Conversation count and expected time for a config, without running anything.
estimate:
	@test -n "$(CONFIG)" || (echo "usage: make estimate CONFIG=configs/pilot.yaml [ARGS=...]" && exit 1)
	python -m harness.run --config $(CONFIG) --estimate $(ARGS)

# Site data from a run, after the publishing check (RFC 0010, section 10): make export RUN=<id> [APP_RUN=<id>] [PREVIEW=1]
# PREVIEW=1 also shows unreviewed scenarios, for looking at a run with `make site` only; never commit or deploy it.
export:
	python -m harness.export --run $(or $(RUN),latest) $(if $(APP_RUN),--app-run $(APP_RUN)) $(if $(filter 1 yes true on,$(PREVIEW)),--preview)

# Run the judge again on a saved run, into results/<run>/rescore_<label>/: make score RUN=<id> [RUBRIC=rubrics/my.yaml]
# [JUDGE_GUARDRAILS=off] [JUDGE_CONFIG=configs/x.yaml] [LABEL=name] [ONLY="transcript ids"]. No chatbot is called.
# PROFILE=rubrics/profiles/my.yaml [FROM=<rescore label>] only recomputes saved v1 labels, with no model at all.
score:
	python -m harness.score --run $(or $(RUN),latest) $(if $(RUBRIC),--rubric $(RUBRIC)) $(if $(PROFILE),--profile $(PROFILE)) $(if $(FROM),--from $(FROM)) $(if $(JUDGE_CONFIG),--judge-config $(JUDGE_CONFIG)) $(if $(JUDGE_GUARDRAILS),--judge-guardrails $(JUDGE_GUARDRAILS)) $(if $(LABEL),--label $(LABEL)) $(if $(ONLY),--only $(ONLY))

# Judge vs the human-scored gold set: make gold [SPLIT=tune|heldout] [JUDGE_GUARDRAILS=off] [JUDGE_CONFIG=configs/x.yaml]
# [RUBRIC=...] [LABEL=name] [SCORES=<scores.jsonl>]. On an invented fixture until rubrics/v1.yaml exists. ARGS=--check validates the set.
gold:
	python -m harness.gold --split $(or $(SPLIT),tune) $(if $(RUBRIC),--rubric $(RUBRIC)) $(if $(JUDGE_CONFIG),--judge-config $(JUDGE_CONFIG)) $(if $(JUDGE_GUARDRAILS),--judge-guardrails $(JUDGE_GUARDRAILS)) $(if $(LABEL),--label $(LABEL)) $(if $(SCORES),--scores $(SCORES)) $(ARGS)

# Realism and script-fidelity report for a saved run, into results/<run>/realism.json and realism.md: make realism [RUN=<id>]
# No model is called. The CHILDES side needs data/childes_reference.json first (data/README.md).
realism:
	python -m harness.realism --run $(or $(RUN),latest)

# Evaluation and guardrail statistics from saved runs, into results/<RUN>/evaluation.json (make export copies it into
# reliability.json): make evaluate [RUN=<on id>] [OFF_RUN=<off id>] [APP_RUN=<app id>] [LABELS=<packet>]
# OFF_RUN also writes results/guardrails/ and needs `make score RUN=<off id> LABEL=judge-on` first. No model is called.
evaluate:
	python -m harness.evaluate --run $(or $(RUN),latest) $(if $(OFF_RUN),--off-run $(OFF_RUN)) $(if $(APP_RUN),--app-run $(APP_RUN)) $(if $(LABELS),--labels $(LABELS)) $(if $(TOOL_LABELS),--tool-labels $(TOOL_LABELS))
