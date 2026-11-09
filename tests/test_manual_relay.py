"""A11: the manual relay, driven by scripted input instead of a person. No clipboard, servers or keys."""

import json

import pytest
import yaml

from harness import REPO_ROOT
from harness.llm.manual import ManualModel, ManualStop
from harness.run import main

REPLY = "That sounds really special. You can tell your mom or a grown-up you trust about it too."


class Person:
    """Answers the relay's prompts like a person would. `stop_at_turn` makes them press s on that turn."""

    def __init__(self, model_shown="TestModel 1", stop_at_turn=None):
        self.model_shown, self.stop_at_turn = model_shown, stop_at_turn
        self.turn, self.pasted, self.prompts = 0, False, []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if prompt.startswith("Which model"):
            return self.model_shown
        if prompt == "> ":                       # after "3. Press Enter here (or s to stop and save)."
            self.turn += 1
            return "s" if self.turn == self.stop_at_turn else ""
        if prompt == "":                         # pasting by hand: the reply, then END
            self.pasted = not self.pasted
            return REPLY if self.pasted else "END"
        if prompt.strip().startswith("Enter = keep"):
            return ""
        raise AssertionError(f"unexpected prompt: {prompt!r}")


def _relay_config(tmp_path, **guardrails):
    cfg = yaml.safe_load((REPO_ROOT / "configs/mock.yaml").read_text())
    cfg["agents"] = [{"name": "chatgpt-app",
                      "model": {"backend": "manual", "app": "ChatGPT", "mode": "Temporary Chat", "clipboard": False}}]
    cfg["guardrails"].update(guardrails)
    p = tmp_path / "relay.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def _run(tmp_path, person, monkeypatch, **guardrails):
    monkeypatch.setattr("builtins.input", person)
    out = tmp_path / "r"
    assert main(["--config", str(_relay_config(tmp_path, **guardrails)), "--out", str(out),
                 "--only", "attach-001", "--seeds", "1"]) == 0
    d = out / (out / "LATEST").read_text().strip()
    transcripts = [json.loads(l) for l in (d / "transcripts.jsonl").read_text().splitlines()]
    return d, transcripts


def test_one_relay_conversation_is_recorded_and_scored(tmp_path, monkeypatch, capsys):
    d, ts = _run(tmp_path, Person(), monkeypatch)
    assert len(ts) == 1
    t = ts[0]
    assert t["channel"] == "app" and t["app_mode"] == "Temporary Chat" and t["app_model_shown"] == "TestModel 1"
    assert t["complete"] is True and t["agent_model"] == "manual:ChatGPT"
    assert [x["text"] for x in t["turns"] if x["speaker"] == "agent"][0] == REPLY  # reply kept exactly
    scores = (d / "scores.jsonl").read_text()
    assert t["transcript_id"] in scores
    out = capsys.readouterr().out
    assert "Paste it into ChatGPT (Temporary Chat)" in out and "attach-001 · chatgpt-app · turn 1 of 6" in out


def test_stop_saves_the_conversation_as_incomplete_and_skips_scoring(tmp_path, monkeypatch):
    d, ts = _run(tmp_path, Person(stop_at_turn=2), monkeypatch)
    t = ts[0]
    assert t["complete"] is False and t["ended_because"] == "stopped by the person relaying"
    assert [x["speaker"] for x in t["turns"]] == ["child", "agent"]  # turn 2's line never reached the app
    assert not (d / "scores.jsonl").exists() or t["transcript_id"] not in (d / "scores.jsonl").read_text()
    m = json.loads((d / "manifest.json").read_text())
    assert m["counts"]["incomplete"] == [t["transcript_id"]] and m["counts"]["scored"] == 0


def test_relay_refuses_guardrails_off(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", Person())
    with pytest.raises(SystemExit, match="guardrails.enforce must be true for relay runs"):
        main(["--config", str(_relay_config(tmp_path, enforce=False)), "--out", str(tmp_path / "r"),
              "--only", "attach-001", "--seeds", "1"])


def test_manual_backend_is_not_allowed_for_the_child_or_judge(tmp_path):
    cfg = yaml.safe_load((REPO_ROOT / "configs/mock.yaml").read_text())
    cfg["roles"]["judge"] = {"backend": "manual", "app": "ChatGPT"}
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(cfg))
    with pytest.raises(SystemExit, match="roles.judge can't use backend: manual"):
        main(["--config", str(p), "--out", str(tmp_path / "r"), "--only", "attach-001"])


class FakeClipboard:
    def __init__(self, pastes):
        self.copied, self.pastes = [], list(pastes)

    available = True

    def copy(self, text):
        self.copied.append(text)
        return True

    def paste(self):
        return self.pastes.pop(0)


def test_clipboard_flow_copies_the_line_and_catches_a_stale_clipboard():
    line = "You my best friend. The bestest."
    # First paste still holds the child's line (person forgot to copy), then the real reply.
    clip = FakeClipboard([line, REPLY])
    answers = iter(["", "", "", ""])  # model name, Enter after pasting, Enter after re-copying, keep
    m = ManualModel("ChatGPT", "Temporary Chat", clipboard=clip, input_fn=lambda p: next(answers), print_fn=lambda *a: None)
    assert m.complete([{"role": "user", "content": line}]) == REPLY
    assert clip.copied == [line] and m.model_shown is None


def test_stop_raises_manualstop():
    m = ManualModel("Gemini", clipboard=FakeClipboard([]), input_fn=lambda p: "s", print_fn=lambda *a: None)
    m.model_shown = "x"
    with pytest.raises(ManualStop):
        m.complete([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "again"}])


def test_estimate_uses_relay_time_for_manual_chatbots(tmp_path, capsys):
    # An empty results folder, so there's no earlier app-sample run to learn from.
    assert main(["--config", "configs/app-sample.yaml", "--estimate", "--out", str(tmp_path)]) == 0
    assert "relay conversations are run by a person, about 5 min each" in capsys.readouterr().out


def _two_app_config(tmp_path):
    cfg = yaml.safe_load(_relay_config(tmp_path).read_text())
    cfg["agents"].append({"name": "gemini-app",
                          "model": {"backend": "manual", "app": "Gemini", "mode": "Temporary chat", "clipboard": False}})
    p = tmp_path / "relay2.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


class CtrlCPerson(Person):
    """Presses Ctrl+C at the "> " prompt on the given turn, counting turns across the whole run."""

    def __init__(self, ctrl_c_at_turn):
        super().__init__()
        self.ctrl_c_at_turn = ctrl_c_at_turn

    def __call__(self, prompt=""):
        if prompt == "> " and self.turn + 1 == self.ctrl_c_at_turn:
            raise KeyboardInterrupt
        return super().__call__(prompt)


def _run_two_apps(tmp_path, monkeypatch, person):
    monkeypatch.setattr("builtins.input", person)
    out = tmp_path / "r"
    assert main(["--config", str(_two_app_config(tmp_path)), "--out", str(out), "--only", "attach-001", "--seeds", "1"]) == 0
    d = out / (out / "LATEST").read_text().strip()
    ts = [json.loads(l) for l in (d / "transcripts.jsonl").read_text().splitlines()]
    return d, ts, json.loads((d / "manifest.json").read_text())


def test_ctrl_c_saves_the_conversation_skips_the_rest_and_still_finishes(tmp_path, monkeypatch, capsys):
    d, ts, m = _run_two_apps(tmp_path, monkeypatch, CtrlCPerson(ctrl_c_at_turn=2))
    assert [t["agent_name"] for t in ts] == ["chatgpt-app"]          # Gemini never started
    t = ts[0]
    assert t["complete"] is False and t["ended_because"] == "stopped with Ctrl+C"
    assert [x["speaker"] for x in t["turns"]] == ["child", "agent"]   # turn 2's line never reached the app
    assert m["counts"]["incomplete"] == [t["transcript_id"]] and (d / "summary.json").exists()
    assert "Press Ctrl+C again to quit at once" in capsys.readouterr().out


def test_ctrl_c_on_the_second_app_keeps_and_scores_the_first(tmp_path, monkeypatch):
    # Count how many turns one full conversation takes, then press Ctrl+C on the second app's first turn.
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = Person()
    _run(probe_dir, probe, monkeypatch)
    turns_per_conversation = probe.turn
    d, ts, m = _run_two_apps(tmp_path, monkeypatch, CtrlCPerson(ctrl_c_at_turn=turns_per_conversation + 1))
    assert [t["agent_name"] for t in ts] == ["chatgpt-app", "gemini-app"]
    assert ts[0]["complete"] is True and ts[1]["complete"] is False and ts[1]["turns"] == []
    assert m["counts"]["scored"] == 1 and ts[0]["transcript_id"] in (d / "scores.jsonl").read_text()


def test_ctrl_c_in_an_automated_run_still_quits_at_once(tmp_path, monkeypatch):
    from harness.agents.agent_under_test import AgentUnderTest

    def interrupt(self, history, seed):
        raise KeyboardInterrupt
    monkeypatch.setattr(AgentUnderTest, "reply", interrupt)
    with pytest.raises(KeyboardInterrupt):
        main(["--config", "configs/mock.yaml", "--out", str(tmp_path), "--only", "attach-001"])
