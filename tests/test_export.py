"""Publishing check and site export (harness/export.py, RFC 0010 packages B5 and B14). Mock models and scripted relay
input only. configs/mock.yaml is on rubric v1, so these exports are case grids."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

import harness.export as export_mod
from harness import REPO_ROOT
from harness.export import ExportError, _boards, _published_reason, export, resolve_run, scan_text, scenario_change
from harness.run import main as run_main
from harness.schemas import ScoreRecord, Transcript
from harness.summary import summarize

FILES = ["meta.json", "leaderboard.json", "scenarios.json", "rubric.json", "reliability.json"]   # + transcripts/<id>.json
API_BOTS = {"careful-bot", "eager-bot", "cautious-bot"}
APP_REPLY = "That sounds really special! You can show your drawing to your mom or a grown-up you trust too."
TEMPORARY = "Temporary Chat, private browser window"


class Relay:
    """Answers the manual relay's prompts (A11) like the person relaying: the same reply on every turn,
    and `s` at (conversation, turn) `stop_at` to stop and save that conversation as incomplete."""

    def __init__(self, stop_at=None):
        self.stop_at, self.conversation, self.turn, self.pasted = stop_at, 0, 0, False

    def __call__(self, prompt=""):
        if prompt.startswith("Which model"):
            self.conversation, self.turn = self.conversation + 1, 0
            return f"Test model {self.conversation}"
        if prompt == "> ":                       # "3. Press Enter here (or s to stop and save)."
            self.turn += 1
            return "s" if (self.conversation, self.turn) == self.stop_at else ""
        if prompt == "":                         # no clipboard: paste the reply by hand, then END
            self.pasted = not self.pasted
            return APP_REPLY if self.pasted else "END"
        if "Enter = keep" in prompt:
            return ""
        raise AssertionError(f"unexpected relay prompt: {prompt!r}")


def _config(base, scenarios, **changes):
    cfg = yaml.safe_load((REPO_ROOT / "configs/mock.yaml").read_text())
    cfg["scenarios"] = str(scenarios / "*" / "*.yaml")
    cfg.update(changes)
    path = base / f"{cfg['run_name']}.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """A mock run and a mock relay run, on copies of the scenarios that are all reviewed and approved by the client,
    except dist-003 (reviewed, not approved by the client) and ctrl-002 (no second reviewer).
    The relay run has attach-001 in two apps; the person stops the Gemini one on turn 2."""
    base = tmp_path_factory.mktemp("export")
    scen = base / "scenarios"
    shutil.copytree(REPO_ROOT / "scenarios", scen)
    for f in scen.glob("*/*.yaml"):
        d = yaml.safe_load(f.read_text())
        d["review"] = {"author": "a", "reviewers": [] if d["id"] == "ctrl-002" else ["b"],
                       "client_approved": d["id"] != "dist-003"}
        f.write_text(yaml.safe_dump(d, sort_keys=False))
    results = base / "results"
    assert run_main(["--config", str(_config(base, scen)), "--out", str(results)]) == 0
    api = resolve_run("latest", results)

    apps = [{"name": "chatgpt-app", "model": {"backend": "manual", "app": "ChatGPT", "mode": TEMPORARY,
                                              "clipboard": False}},
            {"name": "gemini-app", "model": {"backend": "manual", "app": "Gemini",
                                             "mode": "Temporary chat, private browser window", "clipboard": False}}]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("builtins.input", Relay(stop_at=(2, 2)))
        assert run_main(["--config", str(_config(base, scen, run_name="app-sample", agents=apps)),
                         "--out", str(results), "--only", "attach-001", "--seeds", "1"]) == 0
    app = resolve_run("latest", results)
    assert app != api
    return api, app


def _copy(run, folder):
    dst = folder / run.name
    shutil.copytree(run, dst)
    return dst


def _json(p):
    return json.loads(p.read_text())


def _rows(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _write_rows(p, rows):
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_mock_run_needs_preview_or_allow_mock(runs, tmp_path):
    api, _ = runs
    with pytest.raises(ExportError, match="mock models"):
        export(api, tmp_path / "site")
    assert not (tmp_path / "site").exists()
    assert export(api, tmp_path / "preview", preview=True)["meta"]["mock"] is True


def test_mock_run_plus_mock_app_run_writes_every_file(runs, tmp_path):
    api, app = runs
    site = tmp_path / "site"
    res = export(api, site, app_run_dir=app, allow_mock=True)
    for f in FILES:
        assert (site / f).exists(), f
    pages = {p.stem for p in (site / "transcripts").iterdir()}
    meta = _json(site / "meta.json")
    assert meta["schema_version"] == 2 and meta["rubric_version"] == "v1" and meta["preview"] is False
    assert meta["format"] == "case_grid" and meta["counts"]["exported_by_batch"] == {"ollama": 15, "app": 1}
    assert meta["profile"]["names"] == ["default"] and meta["profile"]["custom"] is False
    assert meta["run_id"] == api.name and meta["app_run_ids"] == [app.name]
    c = meta["counts"]
    assert c["transcripts_in_runs"] == 21 + 2 and c["exported"] == len(pages) == 15 + 1
    assert (c["exported_api"], c["exported_app"]) == (15, 1)

    # App conversations are only in app_sample: the API rates are the same as without the app run.
    lb = _json(site / "leaderboard.json")
    alone = _json(export(api, tmp_path / "alone", allow_mock=True)["out_dir"] / "leaderboard.json")
    assert alone["app_sample"] is None
    assert lb["chatbots"] == alone["chatbots"] and {r["chatbot"] for r in lb["chatbots"]} == API_BOTS
    block = lb["app_sample"]
    (row,) = block["chatbots"]
    assert (row["chatbot"], row["app"], row["app_mode"], row["models_shown"]) == (
        "chatgpt-app", "ChatGPT", TEMPORARY, ["Test model 1"])
    assert block["run_ids"] == [app.name] and block["n_exported"] == 1 and block["n_incomplete_withheld"] == 1
    assert row["transcripts"] == ["attach-001__chatgpt-app__s1"]
    assert {r["chatbot"]: len(r["transcripts"]) for r in lb["chatbots"]} == {b: 5 for b in API_BOTS}
    assert res["withheld"]["attach-001__gemini-app__s1"] == ["INCOMPLETE"]

    # An app conversation's page says which app, which private mode and when; replies are as the app wrote them.
    t = _json(site / "transcripts" / "attach-001__chatgpt-app__s1.json")
    assert t["channel"] == "app" and t["app"]["app"] == "ChatGPT" and t["app"]["app_mode"] == TEMPORARY
    assert t["app"]["app_model_shown"] == "Test model 1" and t["app"]["date"] == t["date"]
    assert t["child_model"] == "mock:child" and t["run_id"] == app.name
    assert all(x["text"] == APP_REPLY for x in t["turns"] if x["speaker"] == "agent")
    scen = {s["id"]: s for s in _json(site / "scenarios.json")}
    assert "attach-001__chatgpt-app__s1" in scen["attach-001"]["transcripts"]

    # API pages carry rule answers with quotes, and guardrail reasons without the reviewer's free text.
    t = _json(site / "transcripts" / "abil-001__careful-bot__s1.json")
    assert t["overall"] == "pass" and t["channel"] == "api" and t["app"] is None
    assert any(r["quote"] for r in t["rules"]) and t["guardrail_log"]
    assert all(g["reason"] == "" for g in t["guardrail_log"] if g["check"] == "reviewer")
    assert not any(k in (site / "scenarios.json").read_text() for k in ("must_say", "judge_codes"))
    assert all(x["line"] for s in scen.values() for x in s["turns"])
    rel = _json(site / "reliability.json")
    assert rel["status"].startswith("not evaluated") and rel["judge_quote_check"]["rule_answers"] > 0
    assert (api / "publish_scan.json").exists() and (app / "publish_scan.json").exists()


def test_unreviewed_scenarios_appear_only_with_preview(runs, tmp_path):
    api, _ = runs
    normal = tmp_path / "normal"
    res = export(api, normal, allow_mock=True)
    assert res["withheld"] == {**{f"dist-003__{b}__s1": ["NOT_APPROVED_BY_CLIENT"] for b in API_BOTS},
                               **{f"ctrl-002__{b}__s1": ["NOT_REVIEWED"] for b in API_BOTS}}
    assert not any(p.stem.startswith(("dist-003", "ctrl-002")) for p in (normal / "transcripts").iterdir())
    assert {s["id"] for s in _json(normal / "scenarios.json")} == {
        "abil-001", "data-001", "attach-001", "unsafe-001", "ctrl-001"}
    rows = {r["chatbot"]: r for r in _json(normal / "leaderboard.json")["chatbots"]}
    assert "S4" not in {c["situation_code"] for c in rows["careful-bot"]["cases"]}   # left out of the grid too

    prev = tmp_path / "preview"
    meta = export(api, prev, preview=True)["meta"]
    assert meta["preview"] is True and meta["preview_note"].startswith("Internal preview")
    assert meta["counts"]["exported"] == 21 and meta["counts"]["unreviewed_in_preview"] == 6
    assert {"dist-003", "ctrl-002"} <= {s["id"] for s in _json(prev / "scenarios.json")}
    rows = {r["chatbot"]: r for r in _json(prev / "leaderboard.json")["chatbots"]}
    assert "S4" in {c["situation_code"] for c in rows["careful-bot"]["cases"]}
    t = _json(prev / "transcripts" / "ctrl-002__careful-bot__s1.json")
    assert t["scenario_review"] == {"reviewed": False, "second_reviewers": 0, "client_approved": True,
                                    "approval_basis": None, "needs_client": False, "client_cleared": True,
                                    "held_out": False}


def test_a_plain_mock_run_shows_only_in_preview(tmp_path, capsys):
    """F1's check: `make mock`, then `make export RUN=latest PREVIEW=1`. No scenario in the repo has a second
    reviewer yet, so a normal export of the same run shows nothing."""
    results = tmp_path / "results"
    assert run_main(["--config", str(REPO_ROOT / "configs/mock.yaml"), "--out", str(results), "--only", "attach-001"]) == 0
    assert export_mod.main(["--results", str(results), "--out", str(tmp_path / "x")]) == 1      # mock, no flag
    assert "PREVIEW=1" in capsys.readouterr().err
    assert export_mod.main(["--results", str(results), "--out", str(tmp_path / "preview"), "--preview"]) == 0
    meta = _json(tmp_path / "preview" / "meta.json")
    assert meta["counts"]["exported"] == 3 and meta["ages"] == [5]
    assert [s["id"] for s in _json(tmp_path / "preview" / "scenarios.json")] == ["attach-001"]   # only what was run
    assert export_mod.main(["--results", str(results), "--out", str(tmp_path / "normal"), "--allow-mock"]) == 0
    assert _json(tmp_path / "normal" / "meta.json")["counts"]["exported"] == 0
    assert "add PREVIEW=1" in capsys.readouterr().out


def test_content_ceiling_hit_is_left_out_until_a_person_approves(runs, tmp_path):
    run = _copy(runs[0], tmp_path)
    rows = _rows(run / "transcripts.jsonl")
    tid = "abil-001__eager-bot__s1"
    for r in rows:
        if r["transcript_id"] == tid:
            r["turns"][1]["text"] += " There was blood everywhere."
    _write_rows(run / "transcripts.jsonl", rows)

    for preview in (False, True):                # the publishing check applies in preview mode too
        out = tmp_path / f"preview-{preview}"
        assert export(run, out, allow_mock=True, preview=preview)["withheld"][tid] == ["SCAN_HIT_NEEDS_PERSON"]
        assert not (out / "transcripts" / f"{tid}.json").exists()
    hit = _json(run / "publish_scan.json")["transcripts"][tid]["hits"][0]
    assert hit["speaker"] == "agent" and hit["found_level"] == "severe"

    (run / "publish_approved.txt").write_text(f"# read by a person\n{tid}\n")
    res = export(run, tmp_path / "approved", allow_mock=True)
    assert tid not in res["withheld"]
    t = _json(tmp_path / "approved" / "transcripts" / f"{tid}.json")
    assert t["turns"][1]["text"].endswith("There was blood everywhere.")      # exported unedited
    assert t["publishing_check"] == {"hits": 1, "person_approved": True}


def test_long_reply_is_scanned_past_4000_characters():
    assert scan_text("la " * 2000 + "and then the knife", "mild")[0]["found_level"] == "severe"
    assert scan_text("la " * 2000, "mild") == []


def test_refuses_guardrails_off_run(runs, tmp_path):
    run = _copy(runs[0], tmp_path)
    m = _json(run / "manifest.json")
    m["guardrails"]["enforce"] = False
    (run / "manifest.json").write_text(json.dumps(m))
    for preview in (False, True):
        with pytest.raises(ExportError, match="guardrails off"):
            export(run, tmp_path / "site", allow_mock=True, preview=preview)
    del m["guardrails"]
    (run / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ExportError, match="whether guardrails were on"):
        export(run, tmp_path / "site", preview=True)
    assert not (tmp_path / "site").exists()                                   # nothing was written


def test_app_run_holds_only_app_conversations(runs, tmp_path):
    api, app = runs
    with pytest.raises(ExportError, match="has API conversations"):
        export(app, tmp_path / "a", app_run_dir=api, preview=True)
    with pytest.raises(ExportError, match="reuses transcript ids"):
        export(_copy(app, tmp_path / "copy"), tmp_path / "b", app_run_dir=app, preview=True)
    with pytest.raises(ExportError, match="same run"):
        export(api, tmp_path / "c", app_run_dir=api, preview=True)
    lb = _json(export(app, tmp_path / "app-alone", preview=True)["out_dir"] / "leaderboard.json")
    assert lb["chatbots"] == [] and lb["run"]["judge_models"] == lb["app_sample"]["judge_models"] == ["mock:judge"]


def test_run_saved_before_the_relay_fields_exports_as_api(runs, tmp_path):
    """Runs from before A11 (e.g. the Nov 8 trial runs) have no channel, app_mode, app_model_shown or complete."""
    run = _copy(runs[0], tmp_path)
    rows = _rows(run / "transcripts.jsonl")
    for r in rows:
        for k in ("channel", "app_mode", "app_model_shown", "complete"):
            r.pop(k, None)
    _write_rows(run / "transcripts.jsonl", rows)
    res = export(run, tmp_path / "site", preview=True)
    assert res["meta"]["counts"]["exported_api"] == 21
    assert _json(tmp_path / "site" / "leaderboard.json")["app_sample"] is None


def test_scenario_changed_only_in_its_review_is_still_exported(tmp_path, monkeypatch):
    f = tmp_path / "s.yaml"
    old = b"id: s\nbeats: [a, b]\nreview: {author: a, reviewers: []}\n"
    run_hash = export_mod._sha(old)
    monkeypatch.setattr(export_mod, "_file_at_commit", lambda commit, path: old)
    f.write_bytes(b"id: s\nbeats: [a, b]\nreview: {author: a, reviewers: [b], client_approved: true}\n")
    assert scenario_change(f, run_hash, "abc") is None
    f.write_bytes(b"id: s\nbeats: [a, c]\nreview: {author: a, reviewers: [b]}\n")
    assert "not only in its review" in scenario_change(f, run_hash, "abc")
    monkeypatch.setattr(export_mod, "_file_at_commit", lambda commit, path: None)
    assert "isn't in git" in scenario_change(f, run_hash, "abc")


def test_changed_scenario_is_left_out(runs, tmp_path, monkeypatch):
    monkeypatch.setattr(export_mod, "scenario_change",
                        lambda path, h, c: "changed since the run" if path.stem == "abil-001" else None)
    res = export(runs[0], tmp_path / "site", preview=True)
    assert res["withheld"] == {f"abil-001__{b}__s1": ["SCENARIO_CHANGED_SINCE_RUN"] for b in API_BOTS}
    assert "abil-001" in res["meta"]["scenarios_changed_since_run"]
    assert "abil-001" not in {s["id"] for s in _json(tmp_path / "site" / "scenarios.json")}


def test_guardrail_reasons_never_repeat_a_rejected_draft():
    assert _published_reason({"check": "content-level", "result": "FAIL", "reason": "severe: ['blood']"}) == "severe"
    assert _published_reason({"check": "reviewer", "result": "REJECT", "reason": "says 'my address is'"}) == ""
    assert _published_reason({"check": "sounds-like-child", "result": "FAIL", "reason": "adult words: considerable"}) \
        == "adult words: considerable"
    assert _published_reason({"check": "format", "result": "FAIL", "reason": "mentions a knife"}) == ""
    assert _published_reason({"check": "sounds-like-child", "result": "FAIL",
                              "reason": "adult words: considerable; stage direction: *hugs Mommy's red phone*"}) \
        == "adult words: considerable; stage direction"                       # no words from the draft itself


def test_exact_ages_only(runs, tmp_path):
    meta = export(runs[0], tmp_path / "site", preview=True)["meta"]
    assert meta["age_groups"] is None and meta["ages"] == [3, 4, 5, 10]       # the age filter: the scenarios' ages
    scen = _json(tmp_path / "site" / "scenarios.json")
    assert all("age_group" not in s and s["source"] for s in scen)
    rows = {r["chatbot"]: r for r in _json(tmp_path / "site" / "leaderboard.json")["chatbots"]}
    assert all("by_age_group" not in r for r in rows.values())
    assert {c["child_age"] for r in rows.values() for c in r["cases"]} == {3, 4, 5, 10}


def test_conversation_cut_off_by_a_model_error_is_left_out(runs, tmp_path):
    """Runs from before A15 saved a failed model call as "agent error: ...", still complete and scored (F-009)."""
    run = _copy(runs[0], tmp_path)
    rows = _rows(run / "transcripts.jsonl")
    tid = "abil-001__careful-bot__s1"
    for r in rows:
        if r["transcript_id"] == tid:
            r["ended_because"] = "agent error: gemma4:e4b @ http://127.0.0.1:11502/v1: Request timed out."
    _write_rows(run / "transcripts.jsonl", rows)
    for preview in (False, True):
        out = tmp_path / f"preview-{preview}"
        assert export(run, out, allow_mock=True, preview=preview)["withheld"][tid] == ["ENDED_IN_ERROR"]
        rows = {r["chatbot"]: r for r in _json(out / "leaderboard.json")["chatbots"]}
        assert tid not in rows["careful-bot"]["transcripts"]
        assert "S1" not in {c["situation_code"] for c in rows["careful-bot"]["cases"]}


def test_judge_quote_and_app_model_name_are_scanned(runs, tmp_path):
    api, app = _copy(runs[0], tmp_path / "a"), _copy(runs[1], tmp_path / "b")
    scores = _rows(api / "scores.jsonl")
    tid = "data-001__careful-bot__s1"
    rec = next(r for r in scores if r["transcript_id"] == tid)
    rec["rules"][0].update(evidence_quote="there was blood everywhere", supported=False)   # the judge's own words
    _write_rows(api / "scores.jsonl", scores)
    relayed = _rows(app / "transcripts.jsonl")
    for r in relayed:
        r["app_model_shown"] = "Knife Model 2"
    _write_rows(app / "transcripts.jsonl", relayed)

    res = export(api, tmp_path / "site", app_run_dir=app, preview=True)
    assert res["withheld"][tid] == res["withheld"]["attach-001__chatgpt-app__s1"] == ["SCAN_HIT_NEEDS_PERSON"]
    hits = _json(api / "publish_scan.json")["transcripts"][tid]["hits"]
    assert [h["field"] for h in hits] == [f"judge quote {rec['rules'][0]['rule_id']}"]
    app_hits = _json(app / "publish_scan.json")["transcripts"]["attach-001__chatgpt-app__s1"]["hits"]
    assert [h["field"] for h in app_hits] == ["app_model_shown"]


def test_held_out_scenario_only_in_preview(runs, tmp_path, monkeypatch):
    run = _copy(runs[0], tmp_path)
    m = _json(run / "manifest.json")
    scen = tmp_path / "scenarios"
    shutil.copytree(Path(m["config"]["scenarios"].split("*")[0]), scen)
    f = next(scen.glob("*/abil-001.yaml"))
    d = yaml.safe_load(f.read_text())
    d["held_out"] = True
    f.write_text(yaml.safe_dump(d, sort_keys=False))
    m["config"]["scenarios"] = str(scen / "**" / "*.yaml")
    (run / "manifest.json").write_text(json.dumps(m))
    monkeypatch.setattr(export_mod, "scenario_change", lambda path, h, c: None)   # only held_out changed

    res = export(run, tmp_path / "normal", allow_mock=True)
    assert res["withheld"] == {**{f"abil-001__{b}__s1": ["HELD_OUT"] for b in API_BOTS},
                               **{f"dist-003__{b}__s1": ["NOT_APPROVED_BY_CLIENT"] for b in API_BOTS},
                               **{f"ctrl-002__{b}__s1": ["NOT_REVIEWED"] for b in API_BOTS}}
    assert "abil-001" not in {s["id"] for s in _json(tmp_path / "normal" / "scenarios.json")}
    export(run, tmp_path / "preview", preview=True)
    t = _json(tmp_path / "preview" / "transcripts" / "abil-001__careful-bot__s1.json")
    assert t["scenario_review"]["held_out"] is True



# ------------------------------------------------------------------ B14: rubric v1


def _copy_with_scenarios(run, tmp_path, monkeypatch):
    """A copy of the run that reads its own copy of the scenarios, so a test can change one review block."""
    run = _copy(run, tmp_path)
    m = _json(run / "manifest.json")
    scen = tmp_path / "scenarios"
    shutil.copytree(Path(m["config"]["scenarios"].split("*")[0]), scen)
    m["config"]["scenarios"] = str(scen / "**" / "*.yaml")
    (run / "manifest.json").write_text(json.dumps(m))
    monkeypatch.setattr(export_mod, "scenario_change", lambda path, h, c: None)   # only the review changes
    return run, scen


def _set_review(scen, sid, **review):
    f = next(scen.glob(f"*/{sid}.yaml"))
    d = yaml.safe_load(f.read_text())
    d["review"].update(review)
    f.write_text(yaml.safe_dump(d, sort_keys=False))


def test_leaderboard_is_a_case_grid_with_domains_codes_and_referral(runs, tmp_path):
    lb = _json(export(runs[0], tmp_path / "site", allow_mock=True)["out_dir"] / "leaderboard.json")
    assert lb["format"] == "case_grid" and lb["batch"] == "ollama" and "every chat" in lb["over_escalation"]
    rows = {r["chatbot"]: r for r in lb["chatbots"]}
    for r in rows.values():
        assert r["batch"] == "ollama" and "overall" not in r                # no rates, no ranking
        assert {c["transcript_id"] for c in r["cases"]} == set(r["transcripts"])
        for c in r["cases"]:
            assert set(c["domains"]) == {"cognitive", "emotional", "scaffolding", "responsiveness"}
            assert c["total"] == sum(c["domains"].values()) and c["batch"] == "ollama"
            assert set(c["codes"]) == {"promise", "pushback", "follow_up", "over_escalation"}
            assert c["referral"] in (("pass", "borderline", "fail") if c["situation_code"].startswith("S") else ("na",))
            assert "human_scores" not in c                                  # judge-scored only
    careful = rows["careful-bot"]
    assert careful["referral"] == {"n_rated": 4, "pass": 4, "borderline": 0, "fail": 0}   # dist-003 is withheld
    assert careful["over_escalation"] == {"chats": 0, "of": 5}
    t = _json(tmp_path / "site" / "transcripts" / "abil-001__careful-bot__s1.json")
    assert t["batch"] == "ollama" and "human_scores" not in t and t["total"] == sum(t["domains"].values())
    assert {d["domain"] for d in t["domain_scores"]} == set(t["domains"]) and t["codes"]["over_escalation"] == 0
    assert all(r["level"] in ("MUST", "MUST_NOT", "SHOULD") for r in t["rules"])   # from the default profile


def test_over_escalation_is_counted_on_every_chat(runs, tmp_path):
    run = _copy(runs[0], tmp_path)
    scores = _rows(run / "scores.jsonl")
    for r in scores:
        if r["transcript_id"] in ("ctrl-001__cautious-bot__s1", "abil-001__cautious-bot__s1"):
            r["codes"]["over_escalation"] = 1
    _write_rows(run / "scores.jsonl", scores)
    rows = {r["chatbot"]: r for r in _json(export(run, tmp_path / "s", allow_mock=True)["out_dir"] / "leaderboard.json")["chatbots"]}
    assert rows["cautious-bot"]["over_escalation"] == {"chats": 2, "of": 5}
    assert rows["careful-bot"]["over_escalation"]["chats"] == 0
    assert [c["scenario_id"] for c in rows["cautious-bot"]["cases"] if c["over_escalation"]] == ["abil-001", "ctrl-001"]


def test_distress_scenario_needs_the_client_or_an_approval_basis(runs, tmp_path, monkeypatch):
    run, scen = _copy_with_scenarios(runs[0], tmp_path, monkeypatch)
    res = export(run, tmp_path / "neither", allow_mock=True)              # dist-003: reviewed, neither approval
    assert all(res["withheld"][f"dist-003__{b}__s1"] == ["NOT_APPROVED_BY_CLIENT"] for b in API_BOTS)
    assert "dist-003" not in {s["id"] for s in _json(tmp_path / "neither" / "scenarios.json")}

    _set_review(scen, "dist-003", approval_basis="   ")                    # a blank basis is no basis
    assert "dist-003__careful-bot__s1" in export(run, tmp_path / "blank", allow_mock=True)["withheld"]

    basis = "released in the DIM preprint, Appendix A"
    _set_review(scen, "dist-003", approval_basis=basis)
    res = export(run, tmp_path / "basis", allow_mock=True)
    assert not any(t.startswith("dist-003") for t in res["withheld"])
    s = next(s for s in _json(tmp_path / "basis" / "scenarios.json") if s["id"] == "dist-003")
    assert s["review"]["approval_basis"] == basis and s["review"]["client_approved"] is False
    assert s["review"]["client_cleared"] is True and s["batches"] == ["ollama"]
    t = _json(tmp_path / "basis" / "transcripts" / "dist-003__careful-bot__s1.json")
    assert t["scenario_review"]["approval_basis"] == basis

    _set_review(scen, "dist-003", approval_basis=None, client_approved=True)
    assert not any(t.startswith("dist-003") for t in export(run, tmp_path / "client", allow_mock=True)["withheld"])


def test_app_and_ollama_batches_are_never_counted_together(runs, tmp_path):
    api, app = runs
    site = tmp_path / "site"
    export(api, site, app_run_dir=app, allow_mock=True)
    lb = _json(site / "leaderboard.json")
    alone = _json(export(api, tmp_path / "alone", allow_mock=True)["out_dir"] / "leaderboard.json")
    assert lb["chatbots"] == alone["chatbots"]                              # the app chat changes no Ollama count
    assert all(c["batch"] == "ollama" for r in lb["chatbots"] for c in r["cases"])
    block = lb["app_sample"]
    (row,) = block["chatbots"]
    assert block["batch"] == row["batch"] == "app" and row["n_transcripts"] == 1
    (cell,) = row["cases"]
    assert cell["batch"] == "app" and cell["human_scores"] == {}            # B6 fills it
    t = _json(site / "transcripts" / "attach-001__chatgpt-app__s1.json")
    assert t["batch"] == "app" and t["human_scores"] == {}
    scen = {s["id"]: s for s in _json(site / "scenarios.json")}
    assert scen["attach-001"]["batches"] == ["app", "ollama"] and scen["abil-001"]["batches"] == ["ollama"]

    # The same chatbot name in both batches still gives two separate blocks, each counting only its own chats.
    ts = {}
    sc = {}
    for run, channel in ((api, "api"), (app, "app")):
        for r in _rows(run / "transcripts.jsonl"):
            if r["transcript_id"] in ("abil-001__careful-bot__s1", "attach-001__chatgpt-app__s1"):
                r.update(agent_name="same-bot", channel=channel)
                ts[r["transcript_id"]] = Transcript(**r)
        for r in _rows(run / "scores.jsonl"):
            if r["transcript_id"] in ts:
                sc[r["transcript_id"]] = ScoreRecord(**{**r, "agent_name": "same-bot"})
    s = summarize(ts, sc, set())["same-bot"]["batches"]
    assert {b: [c["transcript_id"] for c in v["cases"]] for b, v in s.items()} == {
        "app": ["attach-001__chatgpt-app__s1"], "ollama": ["abil-001__careful-bot__s1"]}
    with pytest.raises(ExportError, match="another batch"):
        _boards([(export_mod.load_run(api, allow_mock=True), "abil-001__careful-bot__s1")],
                {"abil-001__careful-bot__s1": "api"}, "app")


def test_rubric_json_has_domains_checks_and_the_default_profile(runs, tmp_path):
    site = export(runs[0], tmp_path / "site", allow_mock=True)["out_dir"]
    rub, meta = _json(site / "rubric.json"), _json(site / "meta.json")
    assert rub["version"] == "v1" and rub["labels_hash"] == meta["rubric_hash"]
    assert [d["id"] for d in rub["domains"]] == ["cognitive", "emotional", "scaffolding", "responsiveness"]
    assert all(set(d["levels"]) == {"0", "1", "2"} and d["source"] for d in rub["domains"])
    assert {c["id"] for c in rub["codes"]} == {"promise", "pushback", "follow_up", "over_escalation"}
    assert {c["id"] for c in rub["caps"]} == {"secrecy_promise", "task_done"}
    prof = rub["default_profile"]
    assert prof["name"] == "default" and set(prof["domains"]) == {d["id"] for d in rub["domains"]}
    assert rub["default_profile_hash"] == meta["profile"]["default_hash"]
    rules = {r["id"]: r for r in rub["rules"]}
    assert rules["X7"]["level"] == "MUST_NOT" and rules["X3"]["level"] == "MUST" and "judge_hint" not in rules["X3"]
    assert all(r["source"] for r in rub["rules"]) and "rating.py" in rub["arithmetic"]


def test_judge_reasons_and_domain_quotes_are_scanned(runs, tmp_path):
    run = _copy(runs[0], tmp_path)
    scores = _rows(run / "scores.jsonl")
    tid = "data-001__careful-bot__s1"
    rec = next(r for r in scores if r["transcript_id"] == tid)
    rec["domain_scores"][1]["reasoning"] = "there was blood everywhere"
    _write_rows(run / "scores.jsonl", scores)
    assert export(run, tmp_path / "site", preview=True)["withheld"][tid] == ["SCAN_HIT_NEEDS_PERSON"]
    hits = _json(run / "publish_scan.json")["transcripts"][tid]["hits"]
    assert [h["field"] for h in hits] == [f"judge reason {rec['domain_scores'][1]['domain']}"]


def test_app_run_scored_with_another_rubric_is_refused(runs, tmp_path):
    app = _copy(runs[1], tmp_path)
    m = _json(app / "manifest.json")
    m["config"]["rubric"], m["rubric"] = "rubrics/v0.yaml", {"version": "v0"}
    (app / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(ExportError, match="same rubric"):
        export(runs[0], tmp_path / "site", app_run_dir=app, preview=True)


def test_a_v0_run_still_exports_rates(tmp_path):
    """pilot.yaml stays on rubric v0, so its runs keep the old leaderboard."""
    cfg = yaml.safe_load((REPO_ROOT / "configs/mock.yaml").read_text()) | {"rubric": "rubrics/v0.yaml"}
    (tmp_path / "v0.yaml").write_text(yaml.safe_dump(cfg))
    results = tmp_path / "results"
    assert run_main(["--config", str(tmp_path / "v0.yaml"), "--out", str(results), "--only", "ctrl-001"]) == 0
    site = export(resolve_run("latest", results), tmp_path / "site", preview=True)["out_dir"]
    lb, rub = _json(site / "leaderboard.json"), _json(site / "rubric.json")
    assert lb["format"] == "rates" and "C1" in lb["over_escalation"]
    rows = {r["chatbot"]: r for r in lb["chatbots"]}
    assert rows["cautious-bot"]["overall"]["over_escalation_rate"] == 1.0    # it refers on controls
    assert rub["version"] == "v0" and rub["default_profile"] is None and rub["domains"] == []
    assert _json(site / "meta.json")["profile"] is None
