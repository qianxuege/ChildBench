.PHONY: install test mock gemma local check labels site

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
