"""A1: preflight, token counting, timing in the manifest, and --estimate. No servers or keys needed."""

import json
from types import SimpleNamespace

import pytest

from harness.llm.openai_compat import OpenAICompatModel
from harness.run import main, preflight


def _model(model="gemma3:270m", base_url="http://127.0.0.1:9/v1"):
    return OpenAICompatModel(model=model, base_url=base_url)


def test_preflight_unreachable_server_says_what_to_do():
    # Port 9 (discard) has no server, so the connection is refused straight away.
    err = _model().preflight(timeout=2)
    assert err and "can't reach http://127.0.0.1:9/v1" in err
    assert "ollama serve" in err


def test_preflight_missing_model_says_how_to_pull(monkeypatch):
    m = _model(model="qwen3.5:9b")
    listing = [SimpleNamespace(id="gemma3:270m"), SimpleNamespace(id="gemma4:e4b")]
    fake = SimpleNamespace(models=SimpleNamespace(list=lambda: listing))
    monkeypatch.setattr(m.client, "with_options", lambda **kw: fake)
    err = m.preflight()
    assert "doesn't serve model qwen3.5:9b" in err and "ollama pull qwen3.5:9b" in err


def test_preflight_ready_model_passes(monkeypatch):
    m = _model(model="gemma3:270m")
    fake = SimpleNamespace(models=SimpleNamespace(list=lambda: [SimpleNamespace(id="gemma3:270m")]))
    monkeypatch.setattr(m.client, "with_options", lambda **kw: fake)
    assert m.preflight() is None


def test_preflight_checks_each_server_model_pair_once(monkeypatch):
    a, b = _model(), _model()  # same server and model: only one check
    calls = []
    for m in (a, b):
        monkeypatch.setattr(m, "preflight", lambda calls=calls: calls.append(1) or None)
    assert preflight([a, b]) == [] and len(calls) == 1


def test_run_stops_before_anything_when_preflight_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAICompatModel, "preflight", lambda self, timeout=5.0: f"can't reach {self.base_url}")
    with pytest.raises(SystemExit) as exc:
        main(["--config", "configs/pilot.yaml", "--out", str(tmp_path), "--only", "dist-003", "--seeds", "1"])
    assert "preflight failed, nothing was run" in str(exc.value)
    assert not any(tmp_path.iterdir())  # no run folder was created


def test_token_usage_is_counted(monkeypatch):
    m = _model()
    resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=" hi "))],
                           usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3))
    monkeypatch.setattr(m.client.chat.completions, "create", lambda **kw: resp)
    assert m.complete([{"role": "user", "content": "x"}]) == "hi"
    m.complete([{"role": "user", "content": "x"}])
    assert m.usage() == {"calls": 2, "prompt_tokens": 24, "completion_tokens": 6}


def test_estimate_prints_count_and_writes_nothing(tmp_path, capsys):
    assert main(["--config", "configs/pilot.yaml", "--out", str(tmp_path), "--estimate"]) == 0
    out = capsys.readouterr().out
    assert "7 scenarios x 2 chatbots x 2 seeds = 28 conversations" in out
    assert "no earlier 'pilot' run" in out
    assert not any(tmp_path.iterdir())


def test_manifest_has_timing_and_tokens_and_estimate_learns_from_it(tmp_path, capsys):
    assert main(["--config", "configs/mock.yaml", "--out", str(tmp_path), "--only", "dist-003"]) == 0
    run_id = (tmp_path / "LATEST").read_text().strip()
    m = json.loads((tmp_path / run_id / "manifest.json").read_text())
    t = m["timing"]
    assert t["conversations"] == 3 and len(t["per_conversation"]) == 3
    one = next(iter(t["per_conversation"].values()))
    assert {"talk_seconds", "score_seconds", "talk_tokens", "score_tokens"} <= set(one)
    assert m["tokens"]["total"] == {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}  # mock models
    assert set(m["tokens"]["by_role"]["agents"]) == {"careful-bot", "eager-bot", "cautious-bot"}
    capsys.readouterr()
    assert main(["--config", "configs/mock.yaml", "--out", str(tmp_path), "--estimate"]) == 0
    assert "from the last 1 'mock' run(s)" in capsys.readouterr().out
