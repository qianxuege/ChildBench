import json

import pytest

from harness.agents.toolloop import extract_json
from harness.llm import ModelError
from harness.llm.mock import MockModel
from harness.run import load_personas, load_rubric, main
from harness import REPO_ROOT


def test_scenarios_and_rubric_valid():
    personas, _ = load_personas("scenarios/**/*.yaml")
    rubric = load_rubric(REPO_ROOT / "rubrics/v0.yaml")
    assert len(personas) >= 7
    for code in ["S1", "S2", "S3", "S4", "S5", "C"]:
        assert rubric.for_situation(code), code
    assert {r.id for r in rubric.for_situation("C")} == {"X4", "X5", "C1"}


def test_extract_json():
    assert extract_json('sure! ```json\n{"tool": "x", "args": {"a": "}"}}\n```') == {"tool": "x", "args": {"a": "}"}}
    assert extract_json("no json here") is None


def test_mock_run_end_to_end(tmp_path):
    assert main(["--config", "configs/mock.yaml", "--out", str(tmp_path)]) == 0
    run_id = (tmp_path / "LATEST").read_text().strip()
    d = tmp_path / run_id
    for f in ["transcripts.jsonl", "scores.jsonl", "manifest.json", "summary.json", "guardrail_log.jsonl", "judge_traces.jsonl"]:
        assert (d / f).exists(), f
    s = json.loads((d / "summary.json").read_text())        # rubric v1: a case grid per chatbot and batch
    assert set(s["careful-bot"]["batches"]) == {"ollama"}
    careful, eager = s["careful-bot"]["batches"]["ollama"], s["eager-bot"]["batches"]["ollama"]
    assert careful["referral"]["n_rated"] == careful["referral"]["pass"] == 5
    assert eager["referral"]["fail"] == 5 and eager["referral"]["pass"] == 0
    assert len(careful["cases"]) == 7 and all(c["domains"] and c["total"] is not None for c in careful["cases"])
    assert {c["referral"] for c in careful["cases"] if c["situation_code"] == "C"} == {"na"}   # controls aren't rated
    m = json.loads((d / "manifest.json").read_text())
    assert m["counts"]["transcripts"] == m["counts"]["scored"] == 21
    log = (d / "guardrail_log.jsonl").read_text()
    assert "adult words" in log                      # the realism check caught the demo draft
    traces = (d / "judge_traces.jsonl").read_text()
    assert '"tool": "submit_scores", "ok": false' in traces  # the evidence check made the judge retry


def test_shadow_mode_does_not_block(tmp_path):
    import yaml
    cfg = yaml.safe_load((REPO_ROOT / "configs/mock.yaml").read_text())
    cfg["guardrails"]["enforce"] = False
    p = tmp_path / "shadow.yaml"
    p.write_text(yaml.safe_dump(cfg))
    assert main(["--config", str(p), "--out", str(tmp_path / "r"), "--only", "dist-003"]) == 0
    run_id = (tmp_path / "r" / "LATEST").read_text().strip()
    log = (tmp_path / "r" / run_id / "guardrail_log.jsonl").read_text()
    assert "SHADOW_FAIL" in log


@pytest.mark.parametrize("role, turns_saved", [("child", 2), ("chatbot", 3)])
def test_model_timeout_is_incomplete_and_not_scored(tmp_path, monkeypatch, capsys, role, turns_saved):
    """F-009: a timeout on turn 2 must not become a score, and the server address must not be saved."""
    address = "http://127.0.0.1:11502/v1"
    real = MockModel.complete

    def flaky(self, messages, **kw):
        c = kw.get("context") or {}
        child_turn_2 = c.get("role") == "child_generator" and c["turn"] == 2
        bot_turn_2 = c.get("role") == "chatbot" and c["history"][-1].turn == 2
        if (role == "child" and child_turn_2) or (role == "chatbot" and bot_turn_2):
            raise ModelError(f"gemma4:e4b @ {address}: Request timed out.") from TimeoutError("timed out")
        return real(self, messages, **kw)

    monkeypatch.setattr(MockModel, "complete", flaky)
    assert main(["--config", "configs/mock.yaml", "--out", str(tmp_path), "--only", "dist-003", "--seeds", "1"]) == 0
    d = tmp_path / (tmp_path / "LATEST").read_text().strip()
    rows = [json.loads(line) for line in (d / "transcripts.jsonl").read_text().splitlines()]
    assert len(rows) == 3
    for t in rows:
        assert t["complete"] is False and t["ended_because"] == f"{role} model error: TimeoutError"
        assert len(t["turns"]) == turns_saved
    m = json.loads((d / "manifest.json").read_text())
    assert m["counts"]["scored"] == 0 and len(m["counts"]["incomplete"]) == 3
    assert not (d / "scores.jsonl").exists()
    assert not any(address in f.read_text() for f in d.rglob("*") if f.is_file())
    assert address in capsys.readouterr().err     # still shown to the person running it


@pytest.mark.parametrize("after_submit", [False, True])
def test_judge_timeout_does_not_stop_the_run(tmp_path, monkeypatch, capsys, after_submit):
    """One failed judge call flags that conversation and the rest are scored. A score already submitted is kept."""
    address = "http://127.0.0.1:11503/v1"
    bad = "dist-003__careful-bot__s1"
    real = MockModel.complete

    def flaky(self, messages, **kw):
        c = kw.get("context") or {}
        if c.get("role") == "judge" and c["transcript"].transcript_id == bad:
            submitted = messages[-1]["content"].startswith("TOOL_RESULT submit_scores") and '"ok": true' in messages[-1]["content"]
            if submitted or not after_submit:
                raise ModelError(f"qwen @ {address}: Request timed out.") from TimeoutError("timed out")
        return real(self, messages, **kw)

    monkeypatch.setattr(MockModel, "complete", flaky)
    assert main(["--config", "configs/mock.yaml", "--out", str(tmp_path), "--only", "dist-003", "--seeds", "1"]) == 0
    d = tmp_path / (tmp_path / "LATEST").read_text().strip()
    scored = {json.loads(line)["transcript_id"] for line in (d / "scores.jsonl").read_text().splitlines()}
    assert (bad in scored) is after_submit and len(scored) == (3 if after_submit else 2)
    assert json.loads((d / "manifest.json").read_text())["counts"]["scored"] == len(scored)
    assert (d / "summary.json").exists()
    saved = [f.read_text() for f in d.rglob("*") if f.is_file()]
    assert any("JUDGE_FAILED" in x for x in saved) is not after_submit
    assert not any(address in x for x in saved)
    err = capsys.readouterr().err
    # B10: the judge's loop stops once submit_scores is ok, so after a submit no further call is made to time out
    assert (address in err) is not after_submit
