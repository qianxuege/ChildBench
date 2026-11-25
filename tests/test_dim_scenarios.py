"""A17: the 15 scenarios adapted from the DIM preprint's cards."""

import json
import re

import pytest
import yaml

from harness import REPO_ROOT
from harness.run import load_personas, main
from harness.tools.lint import lint_child_utterance

ILLUSTRATION = {"dim-4.1-context", "dim-4.1-no-context", "dim-9.4-age", "dim-9.4-no-age", "dim-9.7", "dim-7.1", "dim-6.1", "dim-6.1b"}
OTHER = {f"dim-{c}" for c in ("9.1", "9.2", "9.3", "9.5", "9.6", "9.8", "5.1")}
CARDS = load_personas("scenarios/dim/**/*.yaml")[0]


def _config(name):
    return yaml.safe_load((REPO_ROOT / "configs" / name).read_text())


def test_each_config_loads_the_right_files():
    assert set(load_personas(_config("full.yaml")["scenarios"])[0]) == set(CARDS) == ILLUSTRATION | OTHER
    assert set(load_personas(_config("app-sample.yaml")["scenarios"])[0]) == ILLUSTRATION      # R1: no other card in the apps
    assert set(CARDS) < set(load_personas(_config("check.yaml")["scenarios"])[0])
    assert not set(CARDS) & set(load_personas(_config("pilot.yaml")["scenarios"])[0])          # the pilot stays the pilot


def test_real_runs_use_rubric_v1():
    assert all(_config(c)["rubric"] == "rubrics/v1.yaml" for c in ("full.yaml", "app-sample.yaml"))


def test_rubric_v1_scores_every_card(tmp_path):
    cfg = _config("mock.yaml") | {"rubric": "rubrics/v1.yaml", "scenarios": "scenarios/dim/**/*.yaml"}
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(cfg))
    assert main(["--config", str(tmp_path / "c.yaml"), "--out", str(tmp_path / "r")]) == 0
    scores = next((tmp_path / "r").glob("*/scores.jsonl")).read_text().splitlines()
    assert len(scores) == len(CARDS) * len(cfg["agents"])
    for s in map(json.loads, scores):
        p = CARDS[s["transcript_id"].split("__")[0]]
        assert set(s["domains"]) == {"cognitive", "emotional", "scaffolding", "responsiveness"}
        assert (s["overall"] != "na") == p.is_referral
        scripted = {c for t in p.turns for c in t.judge_codes}
        assert all(s["codes"][c] == "na" for c in {"promise", "pushback", "follow_up"} - scripted)


def test_a_full_run_refuses_unreviewed_scenarios(tmp_path, capsys):
    if all(p.review.approved for p in CARDS.values()):
        pytest.skip("every card has a reviewer")
    with pytest.raises(SystemExit, match="no second reviewer"):
        main(["--config", "configs/full.yaml", "--out", str(tmp_path)])


@pytest.mark.parametrize("p", CARDS.values(), ids=list(CARDS))
def test_card_is_complete(p):
    assert p.source.startswith("DIM Card ") and "(adapted)" in p.source and "CC BY 4.0" in p.source
    assert p.good and p.bad and p.dim_items and all(t.must_say for t in p.turns)
    assert p.max_turns == len(p.turns) and p.pushback_line is None      # the chat ends where the card does
    assert p.is_referral == (p.referral.expectation == "required")
    assert not p.review.client_approved
    released = "released in the DIM preprint, Appendix A" if p.situation == "distressing_disclosure" else None
    assert p.review.approval_basis == released
    first = f"{p.age_cue or ''} {p.turns[0].line}".strip()               # as it's sent when the fallback is used
    assert lint_child_utterance(None, first, p.child_age, [])["pass"]
    assert not p.age_cue or str(p.child_age) in p.age_cue


def _differing(a, b):
    a, b = CARDS[a].model_dump(), CARDS[b].model_dump()
    return {k for k in a if a[k] != b[k]} - {"id", "source"}


def test_pairs_differ_only_in_what_they_test():
    assert _differing("dim-4.1-context", "dim-4.1-no-context") == {"caregiver_message"}
    assert _differing("dim-9.4-age", "dim-9.4-no-age") == {"age_cue"}
    assert {i for i, p in CARDS.items() if p.caregiver_message} == {"dim-4.1-context"}
    assert {i for i, p in CARDS.items() if not p.age_cue} == {"dim-4.1-context", "dim-4.1-no-context", "dim-9.4-no-age"}


def test_caregiver_message_is_the_preprints():
    gold = REPO_ROOT / "private/gold/chats.jsonl"
    if not gold.exists():
        pytest.skip("no gold set on this machine")
    from harness.gold import load_gold
    flat = lambda s: re.sub(r"\s+", " ", s.replace("’", "'")).strip()  # noqa: E731
    said = {flat(t.text) for c in load_gold().chats if c.card == "4.1" and c.profile_present for t in c.turns if t.speaker == "caregiver"}
    assert said == {CARDS["dim-4.1-context"].caregiver_message}


def test_no_real_looking_data_in_9_2():
    line = CARDS["dim-9.2"].turns[0].line
    assert "1234 5678 9012 3456" in line and "Pretend Street in Exampletown" in line


def test_key_turns_are_labelled():
    codes = {i: [t.judge_codes for t in p.turns] for i, p in CARDS.items()}
    for i in ("dim-9.4-age", "dim-9.4-no-age", "dim-9.5"):
        assert codes[i] == [["promise"], [], ["pushback"]] and CARDS[i].referral.trigger_turn == 2
    assert codes["dim-6.1b"] == [[], ["follow_up"]]
    for i in ("dim-9.1", "dim-9.2", "dim-9.3", "dim-9.6"):
        assert codes[i] == [[], ["pushback"]]
