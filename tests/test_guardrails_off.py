"""A6: the guardrails-off config and its automatic run-name suffix."""
import json

import yaml

from harness import REPO_ROOT
from harness.run import load_personas, main, run_name


def _cfg(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / "configs" / name).read_text())


def test_run_name_suffix():
    assert run_name({"run_name": "full", "guardrails": {"enforce": False}}) == "full-guardrails-off"
    assert run_name({"run_name": "full-guardrails-off", "guardrails": {"enforce": False}}) == "full-guardrails-off"
    assert run_name({"run_name": "full", "guardrails": {"enforce": True}}) == "full"
    assert run_name({"run_name": "full"}) == "full"          # enforce defaults to true
    assert run_name({"guardrails": {"enforce": False}}) == "run-guardrails-off"


def test_guardrails_off_config_matches_full_run():
    off, on = _cfg("guardrails-off.yaml"), _cfg("full.yaml")
    assert off["guardrails"]["enforce"] is False and off["rubric"] == "rubrics/v1.yaml"
    assert off["seeds"] == [1]
    assert [a["model"]["model"] for a in off["agents"]] == ["llama3.1:8b"]
    personas, _ = load_personas(off["scenarios"])
    assert len(personas) == 15
    # The comparison only means something if guardrails are the one difference from the "on" run (A9).
    for key in ["scenarios", "seeds", "rubric", "roles", "agents", "temperatures"]:
        assert off[key] == on[key], key
    assert run_name(off) == "full-guardrails-off"


def test_mock_run_folder_gets_suffix(tmp_path):
    cfg = _cfg("mock.yaml")
    cfg["guardrails"]["enforce"] = False
    p = tmp_path / "off.yaml"
    p.write_text(yaml.safe_dump(cfg))
    assert main(["--config", str(p), "--out", str(tmp_path / "r"), "--only", "dist-003"]) == 0
    run_id = (tmp_path / "r" / "LATEST").read_text().strip()
    assert f"_{cfg['run_name']}-guardrails-off_" in run_id
    m = json.loads((tmp_path / "r" / run_id / "manifest.json").read_text())
    assert m["guardrails"]["enforce"] is False
