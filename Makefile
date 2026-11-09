.PHONY: install test mock gemma local check labels site run estimate export

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

# Validate every scenario file and the rubric without running anything.
check:
	python -m harness.run --config configs/mock.yaml --check-only

# Blind label sheet for human scoring of the latest run.
labels:
	python -m harness.labels --run latest

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
