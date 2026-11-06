"""ChildBench baseline harness.

Layout (see rfcs/0009):
    schemas.py      Pydantic records for personas, rubric, transcripts, scores, review items
    llm/            model backends behind one ChatModel interface (mock, OpenAI-compatible, local HF)
    tools/          the ten deterministic tools from RFC 0004, with agent-readable docstrings
    agents/         child simulator (generator + reviewer), agent under test, judge
    run.py          orchestrator: scenarios x agents x seeds -> results/<run_id>/
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
__version__ = "0.1.0"
