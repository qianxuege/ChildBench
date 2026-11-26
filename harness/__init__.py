"""ChildBench baseline harness.

Layout (see rfcs/0009):
    schemas.py      Pydantic records for personas, rubric, transcripts, scores, review items
    llm/            model backends behind one ChatModel interface (mock, OpenAI-compatible, local HF)
    tools/          RFC 0004's tools: 9 that the judge or authoring agent call, 3 code-only helpers
    agents/         pretend child (a fixed loop run by code), chatbot under test, judge agent
    author.py       scenario-authoring agent: drafts new scenarios into drafts/ for a person to review
    run.py          orchestrator: scenarios x chatbots x seeds -> results/<run_id>/
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
__version__ = "0.1.0"
