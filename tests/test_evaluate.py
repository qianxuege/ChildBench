"""B6: make evaluate, the guardrail comparison, tool accuracy, and human scores in the export. Mock models only."""

import csv
import json
import shutil

import pytest
import yaml

from harness import REPO_ROOT
from harness.evaluate import (MEASURES, TOOL_FIXTURE, EvaluateError, RunData, card_pairs, chat_guardrails, evaluate,
                              gold_on_off, judge_vs_gold, main, resolve_run, tool_accuracy)
from harness.export import export
from harness.run import main as run_main
from harness.schemas import ScoreRecord, Transcript, Turn
from harness.score import rescore

ONLY = ["dist-003", "ctrl-001", "attach-001"]
APP_REPLY = "That sounds really special! You can show your drawing to your mom or a grown-up you trust too."


class Relay:
    """Answers the manual relay's prompts like the person relaying: the same reply on every turn."""

    def __call__(self, prompt=""):
        if prompt.startswith("Which model"):
            return "Test model"
        if prompt == "> ":
            return ""
        if prompt == "":
            self.pasted = not getattr(self, "pasted", False)
            return APP_REPLY if self.pasted else "END"
        if "Enter = keep" in prompt:
            return ""
        raise AssertionError(f"unexpected relay prompt: {prompt!r}")


def _config(base, scenarios, **changes):
    cfg = yaml.safe_load((REPO_ROOT / "configs/mock.yaml").read_text())
    cfg["scenarios"] = str(scenarios / "*" / "*.yaml")
    for k, v in changes.items():
        if k == "enforce":
            cfg["guardrails"]["enforce"] = v
        else:
            cfg[k] = v
    path = base / f"{cfg['run_name']}-{changes.get('enforce', True)}.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """A guardrails-on mock run, the same run with guardrails off (scored again with the judge's guardrails on), and a
    relay run with attach-001 in one app. Scenarios are copies, all reviewed and client-approved, so the export works."""
    base = tmp_path_factory.mktemp("evaluate")
    scen = base / "scenarios"
    shutil.copytree(REPO_ROOT / "scenarios", scen)
    for f in scen.glob("*/*.yaml"):
        d = yaml.safe_load(f.read_text())
        d["review"] = {"author": "a", "reviewers": ["b"], "client_approved": True}
        f.write_text(yaml.safe_dump(d, sort_keys=False))
    results = base / "results"
    assert run_main(["--config", str(_config(base, scen)), "--out", str(results), "--only", *ONLY]) == 0
    on = resolve_run("latest", results)
    assert run_main(["--config", str(_config(base, scen, enforce=False)), "--out", str(results), "--only", *ONLY]) == 0
    off = resolve_run("latest", results)
    apps = [{"name": "chatgpt-app", "model": {"backend": "manual", "app": "ChatGPT", "mode": "Temporary Chat",
                                              "clipboard": False}}]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("builtins.input", Relay())
        assert run_main(["--config", str(_config(base, scen, run_name="app-sample", agents=apps)),
                         "--out", str(results), "--only", "attach-001", "--seeds", "1"]) == 0
    app = resolve_run("latest", results)
    return {"base": base, "results": results, "on": on, "off": off, "app": app}


@pytest.fixture(scope="module")
def judged(runs):
    """The off run scored again with the judge's guardrails on (A6-run), and the on run with them off."""
    rescore(runs["off"], label="judge-on")
    rescore(runs["on"], guardrails=False)
    return runs


def _ev(runs, tmp_path, **kw):
    kw.setdefault("tool_labels", TOOL_FIXTURE)
    return evaluate(runs["on"], results=runs["results"], gold_dir=tmp_path / "no-gold",
                    guardrails_out=tmp_path / "guardrails", **kw)


def test_every_section_is_written_with_missing_inputs_named(runs, tmp_path):
    ev = _ev(runs, tmp_path)
    assert json.loads((runs["on"] / "evaluation.json").read_text()) == ev
    sections = ["judge_vs_gold", "judge_vs_labels", "human_vs_human", "realism", "tool_accuracy", "guardrail_comparison",
                "context_4_1", "age_9_4", "app_vs_api", "app_grid", "over_escalation"]
    for s in sections:
        assert ev[s]["status"] in ("ok", "missing"), s
        if ev[s]["status"] == "missing":
            assert ev[s]["missing"] and ev["missing"][s] == ev[s]["missing"]
    assert "gold" in ev["judge_vs_gold"]["missing"]
    assert "import-labels" in ev["human_vs_human"]["missing"]
    assert "OFF_RUN" in ev["guardrail_comparison"]["missing"] and "LABEL=judge-on" in ev["guardrail_comparison"]["missing"]
    assert "dim-4.1-context" in ev["context_4_1"]["missing"] and "dim-9.4-age" in ev["age_9_4"]["missing"]
    assert ev["app_grid"]["missing"] == ev["app_vs_api"]["missing"] == "no APP_RUN"
    assert ev["realism"]["on"]["status"] == "ok" and (runs["on"] / "realism.json").exists()
    assert ev["tool_accuracy"]["source"].startswith("INVENTED FIXTURE")
    oe = ev["over_escalation"]["ollama"]
    assert oe["n"] == 9 and oe["controls"]["n"] == 3
    assert ev["mock"] is True and not (tmp_path / "guardrails").exists()


def test_off_run_needs_its_judge_on_rescore(runs, tmp_path):
    bare = runs["base"] / "bare-off"
    shutil.copytree(runs["off"], bare, ignore=shutil.ignore_patterns("rescore_*"))
    with pytest.raises(EvaluateError, match=f"make score RUN={runs['off'].name} LABEL=judge-on"):
        _ev(runs, tmp_path, off_dir=bare)
    assert not (tmp_path / "guardrails").exists()


def test_runs_in_the_wrong_place_are_refused(runs, tmp_path):
    with pytest.raises(EvaluateError, match="guardrails off; pass it as OFF_RUN"):
        evaluate(runs["off"], results=runs["results"], gold_dir=tmp_path)
    with pytest.raises(EvaluateError, match="had guardrails on"):
        _ev(runs, tmp_path, off_dir=runs["app"])
    with pytest.raises(EvaluateError, match="no app chats"):
        _ev(runs, tmp_path, app_dir=runs["off"])


def test_guardrail_comparison_files(judged, tmp_path):
    ev = _ev(judged, tmp_path, off_dir=judged["off"])
    g = ev["guardrail_comparison"]
    assert g["status"] == "ok" and g["scores"]["off"].endswith("rescore_judge-on/scores.jsonl")
    assert g["differences_besides_guardrails"] == []
    out = tmp_path / "guardrails"
    rows = list(csv.DictReader((out / "guardrails.csv").open()))
    n = {s: len([t for t in (judged[s] / "transcripts.jsonl").read_text().splitlines() if t.strip()]) for s in ("on", "off")}
    assert len(rows) == n["on"] + n["off"] == 18                        # one row per chat
    assert len({(r["side"], r["transcript_id"]) for r in rows}) == len(rows)
    assert all(r["judge_guardrails"] == "True" for r in rows if r["scored"] == "True")

    md = (out / "guardrails.md").read_text()
    assert "| Measure | On | Off | Difference (off − on) |" in md
    for key, label, _, _ in MEASURES:
        m = g["measures"][key]
        assert set(m) >= {"on", "off", "difference"} and m["on"]["of"] >= 0
        assert m["difference"] == m["off"]["n"] - m["on"]["n"]
        assert f"| {label} |" in md
    # The mock child's first draft on turn 2 sounds adult: retried with guardrails on, sent with them off.
    assert g["measures"]["speech"]["on"]["n"] == 0 and g["measures"]["speech"]["off"]["n"] > 0
    c = g["scores_change"]
    assert c["pairs"] == 9 and c["changed_traced_to_guardrail_failure"] <= c["changed"] <= 9
    assert len(g["paired"]) == 9 and "Paired score changes per scenario" in md
    j = g["judge_quote_check"]
    assert j["judge_on"]["chats"] == j["judge_off"]["chats"] and j["judge_off"]["unsupported"] >= j["judge_on"]["unsupported"]
    assert j["gold_tune"]["status"] == "missing"
    assert json.loads((out / "guardrails.json").read_text())["measures"] == g["measures"]


def _t(tid, turns):
    return Transcript(transcript_id=tid, run_id="r", scenario_id="s", situation="control", child_age=7, agent_name="bot",
                      agent_model="m", seed=1, turns=turns, ended_because="max_turns")


def _e(item, check, result, reason=""):
    return {"item_id": item, "check": check, "result": result, "reason": reason}


def test_only_the_draft_that_was_sent_counts():
    t = _t("c", [Turn(turn=1, speaker="child", text="x", attempts=2), Turn(turn=1, speaker="agent", text="y"),
                 Turn(turn=2, speaker="child", text="x", attempts=3, used_fallback=True),
                 Turn(turn=3, speaker="child", text="x", attempts=1)])
    events = [
        _e("c:t1:a1", "sounds-like-child", "FAIL", "adult words: rather"),        # retried: never sent
        _e("c:t1:a1", "key-facts", "FAIL", 'say "cat"'),
        _e("c:t1:a2", "sounds-like-child", "PASS"), _e("c:t1:a2", "key-facts", "PASS"),
        _e("c:t2:a1", "content-level", "FAIL", "moderate"),
        _e("c:t2:a2", "sounds-like-child", "FAIL", "realistic personal data: phone"),
        _e("c:t2:a3", "key-facts", "FAIL"), _e("c:t2", "fallback", "PASS"),        # the hand-written line went
        _e("c:t3:a1", "sounds-like-child", "SHADOW_FAIL", "stage direction: *x*; realistic personal data: email"),
        _e("c:t3:a1", "key-facts", "SHADOW_FAIL", 'say "dog"'),
    ]
    g = chat_guardrails(t, events)
    assert g == {"child_lines": 3, "drafts": 6, "speech_reached": 1, "fake_data_reached": 1, "key_moments": 3,
                 "key_facts_missed": 1, "content_level_stopped": 1, "fallbacks": 1, "shadow_fails": 2}


def test_tool_accuracy(tmp_path):
    r = tool_accuracy(TOOL_FIXTURE)
    assert r["status"] == "ok" and r["not_labelled"] == [] and r["problems"] == []
    for m in r["tools"].values():
        assert m["n"] == m["bad"] + m["good"] > 0 and m["caught"] + m["missed"] == m["bad"]
    # Labels the tool disagrees with show up as row numbers, never as text; bad files and rows are named.
    (tmp_path / "scan_realistic_pii.csv").write_text("text,expected\nmy number is 555-0123,ok\nhello,flag\nhi,maybe\n")
    (tmp_path / "lint_child_utterance.csv").write_text("text,expected\nhi,ok\n")
    (tmp_path / "nonsense.csv").write_text("text,expected\nhi,ok\n")
    r = tool_accuracy(tmp_path)
    pii = r["tools"]["scan_realistic_pii"]
    assert pii["wrong_rows"] == [2, 3] and pii["unusable_rows"] == [4] and pii["accuracy"] == 0.0
    assert pii["catch_rate"] == 0.0 and pii["false_alarm_rate"] == 1.0
    assert any("missing column(s) age" in p for p in r["problems"]) and any("nonsense" in p for p in r["problems"])
    assert "555" not in json.dumps(r)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert tool_accuracy(empty)["status"] == "missing"


def _gold(tmp_path):
    g = tmp_path / "gold"
    shutil.copytree(REPO_ROOT / "tests/fixtures/gold", g)
    for label, flag in (("on", True), ("off", False)):
        d = g / "runs" / "tune" / f"rescore_{label}"
        d.mkdir(parents=True)
        shutil.copy(g / "judge_scores.jsonl", d / "scores.jsonl")
        (d / "score_manifest.json").write_text(json.dumps({"judge_guardrails": flag, "judge": {"model": "j"},
                                                          "created_at": label}))
    return g


def test_gold_sections(tmp_path):
    g = _gold(tmp_path)
    r = judge_vs_gold(g, "j")
    assert r["status"] == "ok" and r["tune"]["status"] == "ok" and r["tune"]["judge_guardrails"] is True
    assert r["tune"]["n"] > 0 and set(r["tune"]["domains"]) == {"cognitive", "emotional", "scaffolding", "responsiveness"}
    assert r["heldout"]["status"] == "missing" and "B13" in r["heldout"]["missing"]
    assert "not RUN's judge" in judge_vs_gold(g, "other")["tune"]["note"]
    both = gold_on_off(g, "j")
    assert both["on"]["judge_guardrails"] is True and both["off"]["judge_guardrails"] is False
    assert all(v == 0 for v in both["exact_change"].values())           # same scores both ways here
    assert judge_vs_gold(tmp_path / "none", "j")["status"] == "missing"


def _rec(tid, sid, total, rating="na"):
    d = {"cognitive": 2, "emotional": 2, "scaffolding": total - 4, "responsiveness": 0}
    return ScoreRecord(transcript_id=tid, run_id="r", scenario_id=sid, situation="everyday", agent_name="bot",
                       rubric_version="v1", judge_model="j", rules=[], overall=rating, first_referral_turn=None,
                       domains=d, total=total, codes={"over_escalation": 0})


def test_card_pairs_match_chatbot_and_seed(tmp_path):
    ts, recs = {}, {}
    for sid, total in (("dim-4.1-context", 6), ("dim-4.1-no-context", 4)):
        tid = f"{sid}__bot__s1"
        ts[tid] = _t(tid, []).model_copy(update={"scenario_id": sid, "situation": "everyday"})
        recs[tid] = _rec(tid, sid, total)
    run = RunData(dir=tmp_path, manifest={"run_id": "r"}, transcripts=ts, scores=recs, scores_from="x")
    r = card_pairs({"ollama": run}, "dim-4.1-context", "dim-4.1-no-context", "full context")
    assert r["status"] == "ok" and r["ollama"]["pairs"] == 1 and r["ollama"]["mean_total_change"] == 2
    assert card_pairs({"ollama": run}, "dim-9.4-age", "dim-9.4-no-age", "age")["status"] == "missing"


def _packet(runs, app_tid):
    p = runs["results"] / "labeling" / "p1" / "private"
    p.mkdir(parents=True, exist_ok=True)
    answers = {"A": {"cognitive": "2", "emotional": "1", "scaffolding": "2", "responsiveness": "2", "X3": "yes"},
               "B": {"cognitive": "1", "emotional": "1", "scaffolding": "2", "responsiveness": "2", "X3": "partial"}}
    (p / "human_labels.json").write_text(json.dumps({"packet_id": "p1", "scorers": ["A", "B"], "chats": {
        app_tid: {"chat_id": "bxk", "labels": answers, "ratings": {"A": "pass", "B": "borderline"},
                  "notes": {"A": "a private note"}, "agreed": {}}}}))
    (p / "agreement.json").write_text(json.dumps({"packet_id": "p1", "chats": 1, "judge_scores": "original",
                                                  "judge_settings": [], "comparisons": {
        "A_vs_B": {"chats": 1, "domains": {}}, "A_vs_judge": {"chats": 1}, "B_vs_judge": {"chats": 1}}}))


def test_human_scores_and_app_sections_reach_the_export(judged, tmp_path):
    app_tid = next(json.loads(x)["transcript_id"] for x in (judged["app"] / "transcripts.jsonl").read_text().splitlines()
                   if json.loads(x)["complete"])
    _packet(judged, app_tid)
    ev = _ev(judged, tmp_path, off_dir=judged["off"], app_dir=judged["app"])
    assert ev["label_packet"] == "p1" and ev["human_vs_human"]["status"] == "ok" and ev["human_vs_human"]["chats"] == 1
    assert set(ev["judge_vs_labels"]["comparisons"]) == {"A_vs_judge", "B_vs_judge"}
    h = ev["human_scores"][app_tid]
    assert h["scorers"]["A"]["total"] == 7 and h["scorers"]["B"]["rating"] == "borderline"
    assert "a private note" not in json.dumps(ev)
    grid = ev["app_grid"]
    assert grid["status"] == "ok" and grid["human_scored"] == 1 and grid["rows"][0]["humans"]["A"]["total"] == 7
    assert ev["app_vs_api"]["status"] == "ok" and ev["app_vs_api"]["scenarios"] == 1
    assert ev["over_escalation"]["app"]["run_id"] == judged["app"].name

    site = tmp_path / "site"
    export(judged["on"], site, app_run_dir=judged["app"], allow_mock=True)
    page = json.loads((site / "transcripts" / f"{app_tid}.json").read_text())
    assert page["human_scores"] == h
    lb = json.loads((site / "leaderboard.json").read_text())
    (cell,) = [c for r in lb["app_sample"]["chatbots"] for c in r["cases"] if c["transcript_id"] == app_tid]
    assert cell["human_scores"] == h
    rel = json.loads((site / "reliability.json").read_text())
    assert rel["status"] == "evaluated" and rel["evaluation"]["off_run_id"] == judged["off"].name
    assert rel["human_vs_human"]["chats"] == 1 and rel["app_grid"]["rows"][0]["transcript_id"] == app_tid
    assert rel["guardrail_comparison"]["measures"] and "paired" not in rel["guardrail_comparison"]
    assert "warning" not in rel["evaluation"]
    # Evaluated with another APP_RUN than the one exported: said, not hidden.
    alone = json.loads((export(judged["on"], tmp_path / "alone", allow_mock=True)["out_dir"] / "reliability.json").read_text())
    assert "make evaluate again" in alone["evaluation"]["warning"]


def test_command_line(judged, capsys):
    rel = judged["results"].relative_to(REPO_ROOT) if judged["results"].is_relative_to(REPO_ROOT) else judged["results"]
    assert main(["--run", judged["on"].name, "--results", str(rel)]) == 0
    out = capsys.readouterr().out
    assert "evaluation.json" in out and "MISSING: no OFF_RUN" in out and "MOCK RUN" in out
    assert main(["--run", judged["off"].name, "--results", str(rel)]) == 1
    assert "pass it as OFF_RUN" in capsys.readouterr().err
