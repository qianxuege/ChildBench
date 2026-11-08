import json

from harness.agents.toolloop import extract_json
from harness.labels import build
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
    s = json.loads((d / "summary.json").read_text())
    assert s["careful-bot"]["all"]["pass_rate_referral_scenarios"] == 1.0
    assert s["eager-bot"]["all"]["pass_rate_referral_scenarios"] == 0.0
    assert s["cautious-bot"]["all"]["false_referral_rate_controls"] == 1.0
    m = json.loads((d / "manifest.json").read_text())
    assert m["counts"]["transcripts"] == m["counts"]["scored"] == 21
    log = (d / "guardrail_log.jsonl").read_text()
    assert "adult words" in log                      # the realism check caught the demo draft
    traces = (d / "judge_traces.jsonl").read_text()
    assert '"tool": "submit_scores", "ok": false' in traces  # the evidence check made the judge retry
    out = build(d, load_rubric(REPO_ROOT / "rubrics/v0.yaml"))
    sheet = (out / "label_sheet.csv").read_text()
    assert "careful-bot" not in sheet and "careful-bot" in (out / "blind_key.csv").read_text()


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
