"""B1: scoring a saved run again. Mock models only."""

import json
import shutil

import pytest
import yaml

from harness import REPO_ROOT
from harness.agents.agent_under_test import AgentUnderTest
from harness.agents.child_simulator import ChildSimulator
from harness.llm import ModelError
from harness.llm.mock import MockModel
from harness.run import main as run_main
from harness.schemas import ScoreRecord
from harness.score import ScoreError, main, rescore, resolve_run


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    results = tmp_path_factory.mktemp("score") / "results"
    assert run_main(["--config", "configs/mock.yaml", "--out", str(results)]) == 0
    return resolve_run("latest", results)


@pytest.fixture
def run(base, tmp_path):
    shutil.copytree(base, tmp_path / base.name)
    return tmp_path / base.name


def _rows(p):
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def _labels(p):
    return {r["transcript_id"]: (r["overall"], r["first_referral_turn"], [(s["rule_id"], s["label"], s["supported"]) for s in r["rules"]])
            for r in _rows(p)}


def _submits(p):
    """Transcript id -> whether each submit_scores call was accepted."""
    return {r["transcript_id"]: [s["ok"] for s in r["trace"] if s.get("tool") == "submit_scores"] for r in _rows(p)}


def test_rescoring_gives_the_same_ratings_and_changes_nothing_else(run, monkeypatch):
    def never(*a, **k):
        raise AssertionError("re-scoring must not call the child or a chatbot")

    monkeypatch.setattr(AgentUnderTest, "reply", never)
    monkeypatch.setattr(ChildSimulator, "utterance", never)
    before = {f.name: f.read_bytes() for f in run.iterdir()}
    out = rescore(run, label="same")
    assert out == run / "rescore_same"
    assert _labels(out / "scores.jsonl") == _labels(run / "scores.jsonl") and len(_labels(out / "scores.jsonl")) == 21
    assert {f.name: f.read_bytes() for f in run.iterdir() if f.is_file()} == before
    assert json.loads((out / "summary.json").read_text()) == json.loads((run / "summary.json").read_text())
    m = json.loads((out / "score_manifest.json").read_text())
    assert m["run_id"] == run.name and m["judge_guardrails"] is True and m["judge"]["model"] == "mock:judge"
    assert m["counts"] == {"transcripts": 21, "scored": 21, "failed": 0, "skipped": {}, "review_items": m["counts"]["review_items"]}
    assert all(r["judge_guardrails"] for r in _rows(out / "scores.jsonl"))


def test_with_guardrails_off_the_judge_makes_no_retry(run):
    on = _submits(rescore(run, label="on") / "judge_traces.jsonl")
    off_dir = rescore(run, guardrails=False, label="off")
    off = _submits(off_dir / "judge_traces.jsonl")
    assert any(calls == [False, True] for calls in on.values())       # the mock judge's bad quote was sent back
    assert all(calls == [True] for calls in off.values()) and len(off) == 21
    scores = _rows(off_dir / "scores.jsonl")
    assert any(not s["supported"] for r in scores for s in r["rules"])  # kept, but still marked unsupported
    assert all(r["judge_guardrails"] is False for r in scores)
    assert any(r["reason_code"] == "UNSUPPORTED_EVIDENCE" for r in _rows(off_dir / "review_queue.jsonl"))


def test_a_guardrails_off_run_is_scored_with_judge_guardrails_on(tmp_path):
    cfg = yaml.safe_load((REPO_ROOT / "configs/mock.yaml").read_text())
    cfg["guardrails"]["enforce"] = False
    (tmp_path / "off.yaml").write_text(yaml.safe_dump(cfg))
    assert run_main(["--config", str(tmp_path / "off.yaml"), "--out", str(tmp_path / "r"), "--only", "abil-001"]) == 0
    run = resolve_run("latest", tmp_path / "r")
    assert not any(r["judge_guardrails"] for r in _rows(run / "scores.jsonl"))
    out = rescore(run, label="x")
    assert all(r["judge_guardrails"] for r in _rows(out / "scores.jsonl"))
    assert json.loads((out / "score_manifest.json").read_text())["run_guardrails"]["enforce"] is False


def test_another_rubric(run, tmp_path):
    rubric = yaml.safe_load((REPO_ROOT / "rubrics/v0.yaml").read_text())
    rubric["version"] = "mine"
    rubric["rules"] = [r for r in rubric["rules"] if r["id"] in ("X3", "C1")]
    (tmp_path / "mine.yaml").write_text(yaml.safe_dump(rubric))
    out = rescore(run, rubric_file=str(tmp_path / "mine.yaml"))
    assert out.name.endswith("_mine")
    rows = _rows(out / "scores.jsonl")
    assert {r["rubric_version"] for r in rows} == {"mine"} and {s["rule_id"] for r in rows for s in r["rules"]} == {"X3", "C1"}


def test_another_judge(run, tmp_path):
    cfg = {"roles": {"judge": {"backend": "mock", "name": "other-judge"}}}
    (tmp_path / "j.yaml").write_text(yaml.safe_dump(cfg))
    out = rescore(run, judge_config=str(tmp_path / "j.yaml"), label="j")
    assert {r["judge_model"] for r in _rows(out / "scores.jsonl")} == {"mock:other-judge"}


def test_cut_off_conversations_are_skipped(run):
    rows = _rows(run / "transcripts.jsonl")
    rows[0]["complete"] = False
    rows[1]["ended_because"] = "agent error: gemma @ http://127.0.0.1:1: timed out"      # saved before A15
    (run / "transcripts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = rescore(run, label="x")
    assert not {rows[0]["transcript_id"], rows[1]["transcript_id"]} & set(_labels(out / "scores.jsonl"))
    assert json.loads((out / "score_manifest.json").read_text())["counts"]["skipped"] == {
        rows[0]["transcript_id"]: "incomplete", rows[1]["transcript_id"]: "cut off by a model error"}


def test_fallback_flags_from_the_run_still_keep_a_chat_out_of_the_headline(run):
    item = {"item_id": "dist-003__careful-bot__s1:t3", "run_id": run.name, "reason_code": "FALLBACK_USED"}
    with open(run / "review_queue.jsonl", "a") as f:
        f.write(json.dumps(item) + "\n")
    s = json.loads((rescore(run, label="x") / "summary.json").read_text())["careful-bot"]
    assert s["all"]["n_transcripts"] == 7 and s["n_excluded_pending_review"] == 1


def test_one_failed_judge_call_does_not_stop_the_rest(run, monkeypatch, capsys):
    bad, address, real = "dist-003__careful-bot__s1", "http://127.0.0.1:11503/v1", MockModel.complete

    def flaky(self, messages, **kw):
        if kw["context"]["transcript"].transcript_id == bad:
            raise ModelError(f"qwen @ {address}: timed out") from TimeoutError()
        return real(self, messages, **kw)

    monkeypatch.setattr(MockModel, "complete", flaky)
    out = rescore(run, label="x")
    assert bad not in _labels(out / "scores.jsonl") and len(_labels(out / "scores.jsonl")) == 20
    assert json.loads((out / "score_manifest.json").read_text())["counts"]["failed"] == 1
    assert not any(address in f.read_text() for f in out.iterdir()) and address in capsys.readouterr().err


def test_command_line(run, capsys):
    args = ["--run", run.name, "--results", str(run.parent), "--label", "cli", "--only", "dist-003__careful-bot__s1"]
    assert main([*args, "--judge-guardrails", "off"]) == 0
    assert list(_labels(run / "rescore_cli" / "scores.jsonl")) == ["dist-003__careful-bot__s1"]
    before = (run / "rescore_cli" / "scores.jsonl").read_bytes()
    assert main(args) == 1 and "already exists" in capsys.readouterr().err      # never overwritten
    assert (run / "rescore_cli" / "scores.jsonl").read_bytes() == before
    assert main(["--run", "nope", "--results", str(run.parent)]) == 1


@pytest.mark.parametrize("kwargs, message", [
    ({"label": "../x"}, "may only have"), ({"only": ["nope"]}, "not in this run"), ({"rubric_file": "nope.yaml"}, "no file"),
])
def test_bad_options_write_nothing(run, kwargs, message):
    with pytest.raises(ScoreError, match=message):
        rescore(run, **kwargs)
    assert not list(run.glob("rescore_*"))


def test_scores_saved_before_b1_still_load(run):
    row = _rows(run / "scores.jsonl")[0]
    del row["judge_guardrails"]
    assert ScoreRecord(**row).judge_guardrails is True
