"""A8: realism and script-fidelity report. Mock models only; CHILDES and the norms are tiny made-up fixtures."""

import json
import shutil

import pytest

from harness.realism import (build_childes, load_norms, main, mattr, measures, parse_cha, pushback_status, report,
                             sentences)
from harness.run import main as run_main
from harness.schemas import Transcript
from harness.score import resolve_run, run_scenarios

CHA = """@Begin
@Languages:\teng
@Participants:\tCHI Target_Child, MOT Mother
@ID:\teng|Test|CHI|4;02.10|female|TD||Target_Child|||
@ID:\teng|Test|MOT|||||Mother|||
*MOT:\twhat is that ?
*CHI:\tdoggy [: dog] go(ing) home .
*CHI:\twhere my ball ?
*CHI:\txxx &-um I want it +...
*CHI:\tthe big
\tred truck@f is mine .
*CHI:\t0is it raining ?
@G:\tAlphabet
*CHI:\ta and a apple .
@G:\tHowTo .
*CHI:\t\u21absh\u21abshe brush teeth .
@End
"""


def _rows(p):
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _write(p, rows):
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    results = tmp_path_factory.mktemp("realism") / "results"
    assert run_main(["--config", "configs/mock.yaml", "--out", str(results)]) == 0
    return resolve_run("latest", results)


@pytest.fixture
def run(base, tmp_path):
    shutil.copytree(base, tmp_path / base.name)
    return tmp_path / base.name


def _report(run_dir, tmp_path):
    return report(run_dir, reference_path=tmp_path / "none.json", norms_path=None)


# ------------------------------------------------------------------ measures


def test_sentences_and_mattr():
    assert sentences("I'm sad. Where Biscuit go? He brown") == [(["i'm", "sad"], False), (["where", "biscuit", "go"], True),
                                                                (["he", "brown"], False)]
    assert mattr(["a"] * 10, window=5) == 0.2
    assert mattr(["a", "b", "c"], window=5) is None
    m = measures(sentences("A dog. A cat? A dog."), 4, {"dog": 3.0, "cat": 5.5, "a": 2.0})
    assert m["sentences"] == 3 and m["words_per_sentence"]["mean"] == 2.0 and m["questions"]["share"] == 0.333
    assert m["aoa"] == {"rated_words": 6, "coverage": 1.0, "after_age": 1, "share_after_age": 0.167}


def test_norms_csv(tmp_path):
    (tmp_path / "n.csv").write_text("Word,OccurTotal,Rating.Mean\nDog,10,2.5\ncat,10,NA\n")
    assert load_norms(tmp_path / "n.csv") == {"dog": 2.5}
    assert load_norms(tmp_path / "missing.csv") is None
    (tmp_path / "bad.csv").write_text("Word,Freq\ndog,1\n")
    with pytest.raises(ValueError):
        load_norms(tmp_path / "bad.csv")


def test_parse_cha_keeps_only_the_target_child():
    age, group, utts, dropped = parse_cha(CHA, frozenset({"alphabet"}))
    assert (age, group) == (4, "TD") and dropped == {"alphabet": 1}
    assert utts[-1] == (["she", "brush", "teeth"], False)      # a drilled task is left out; ↫sh↫ is dropped
    assert utts[:-1] == [(["doggy", "going", "home"], False), (["where", "my", "ball"], True), (["i", "want", "it"], False),
                    (["the", "big", "red", "truck", "is", "mine"], False), (["it", "raining"], True)]


def test_build_childes_skips_clinical_groups(tmp_path):
    (tmp_path / "cha" / "Test").mkdir(parents=True)
    (tmp_path / "cha" / "Test" / "a.cha").write_text(CHA)
    (tmp_path / "cha" / "Test" / "b.cha").write_text(CHA.replace("|TD|", "|SLI|"))
    ref = build_childes(tmp_path / "cha", {"truck": 4.5, "dog": 2.0}, tmp_path / "ref.json")
    assert list(ref["by_age"]) == ["4"] and ref["skipped_files"] == {"group SLI": 1}
    a4 = ref["by_age"]["4"]
    assert a4["sentences"] == 6 and a4["children"] == 1 and a4["questions"]["n"] == 2
    assert ref["skipped_tasks"]["utterances"] == {"alphabet": 1}
    assert a4["aoa"]["after_age"] == 1 and json.loads((tmp_path / "ref.json").read_text()) == ref


# ------------------------------------------------------------------ fidelity: the Done-when check


def test_fidelity_counts_planted_misses(run, tmp_path):
    """A known missed fact, a fallback and a skipped pushback each move exactly the count they should."""
    base_report = _report(run, tmp_path)["fidelity"]
    before = base_report["overall"]
    seen = {(m["transcript_id"], m["turn"], m["what"]) for m in base_report["misses"]}
    assert before["fallback"]["n"] == 0 and before["key_facts_final"]["n"] == before["key_facts_final"]["of"]

    manifest = json.loads((run / "manifest.json").read_text())
    personas = run_scenarios(manifest)
    rows = _rows(run / "transcripts.jsonl")
    ts = [Transcript(**r) for r in rows]
    pushed = next(i for i, t in enumerate(ts) if (pushback_status(personas[t.scenario_id], t) or ("",))[0] == "said")
    other = next(i for i, t in enumerate(ts) if i != pushed and personas[t.scenario_id].turns[0].must_say)

    # 1) skipped pushback: the chat ends right after the referral, as at the turn limit
    ref_turn = pushback_status(personas[ts[pushed].scenario_id], ts[pushed])[1]
    dropped = sum(x["speaker"] == "child" and x["turn"] > ref_turn for x in rows[pushed]["turns"])
    rows[pushed]["turns"] = [x for x in rows[pushed]["turns"] if x["turn"] <= ref_turn]
    # 2) fallback on turn 2 of another chat
    next(x for x in rows[other]["turns"] if x["speaker"] == "child" and x["turn"] == 2)["used_fallback"] = True
    _write(run / "transcripts.jsonl", rows)
    # 3) missed fact on turn 1 of that chat: fails on the first try, passes on the second
    tid = rows[other]["transcript_id"]
    log = _rows(run / "guardrail_log.jsonl")
    first = next(e for e in log if e["item_id"] == f"{tid}:t1:a1" and e["check"] == "key-facts")
    first["result"] = "FAIL"
    log.append({**first, "item_id": f"{tid}:t1:a2", "result": "PASS"})
    _write(run / "guardrail_log.jsonl", log)

    r = _report(run, tmp_path)
    after = r["fidelity"]["overall"]
    assert after["pushback_when_due"] == {"n": before["pushback_when_due"]["n"] - 1, "of": before["pushback_when_due"]["of"],
                                          "share": after["pushback_when_due"]["share"]}
    assert after["fallback"]["n"] == 1 and after["fallback"]["of"] == before["fallback"]["of"] - dropped
    assert after["fallback_by_phase"]["script"]["n"] == 1
    assert after["key_facts_first_try"]["n"] == before["key_facts_first_try"]["n"] - 1
    assert after["key_facts_final"] == before["key_facts_final"]
    assert after["turns_delivered"] == before["turns_delivered"]
    misses = {(m["transcript_id"], m["turn"], m["what"]) for m in r["fidelity"]["misses"]}
    assert misses - seen == {(ts[pushed].transcript_id, ref_turn + 1, "pushback due but not said (turn limit)"),
                      (tid, 2, "fallback (script)"), (tid, 1, "key facts missed on the first try")}
    # the fallback line is reported apart from the model's own lines
    age = str(ts[other].child_age)
    assert r["realism"]["by_age"][age]["fallback"]["lines"] == 1
    # per chatbot sums to overall
    assert sum(g["fallback"]["n"] for g in r["fidelity"]["by_chatbot"].values()) == 1


def test_before_a16_key_facts_not_recorded_and_incomplete_apart(run, tmp_path):
    _write(run / "guardrail_log.jsonl", [e for e in _rows(run / "guardrail_log.jsonl") if e["check"] != "key-facts"])
    rows = _rows(run / "transcripts.jsonl")
    n = len(rows)
    rows[0]["complete"] = False
    rows[0]["ended_because"] = "stopped by the person relaying"
    _write(run / "transcripts.jsonl", rows)
    r = _report(run, tmp_path)
    f = r["fidelity"]
    assert f["key_facts_recorded"] is False and f["overall"]["key_facts_first_try"] == "not recorded"
    assert f["overall"]["conversations"] == n - 1 and f["incomplete"]["n"] == 1
    assert "**not recorded**" in (run / "realism.md").read_text()


def test_guardrails_off_is_labelled(run, tmp_path):
    m = json.loads((run / "manifest.json").read_text())
    m["config"]["guardrails"]["enforce"] = False
    (run / "manifest.json").write_text(json.dumps(m))
    r = _report(run, tmp_path)
    assert r["guardrails"] == "off" and "separately" in r["note"]


def test_cli_with_reference_compares_ages(run, tmp_path):
    (tmp_path / "cha" / "T").mkdir(parents=True)
    ages = {t["child_age"] for t in _rows(run / "transcripts.jsonl")}
    for a in ages:          # 5 children per age: fewer are shown but not compared (MIN_CHILDREN)
        for k in range(5):
            (tmp_path / "cha" / "T" / f"{a}-{k}.cha").write_text(CHA.replace("4;02.10", f"{a};00.00") * 3)
    (tmp_path / "n.csv").write_text("Word,AoA_Kup_lem\ndog,3\nmine,4\n")
    assert main(["build-childes", "--cha-dir", str(tmp_path / "cha"), "--norms", str(tmp_path / "n.csv"),
                 "--out", str(tmp_path / "ref.json")]) == 0
    assert main(["--run", run.name, "--results", str(run.parent), "--reference", str(tmp_path / "ref.json"),
                 "--norms", str(tmp_path / "n.csv")]) == 0
    r = json.loads((run / "realism.json").read_text())
    assert r["realism"]["childes_available"] and r["realism"]["norms_available"]
    a = next(iter(r["realism"]["by_age"].values()))
    assert a["comparison"]["words_per_sentence"]["cohens_d"] is not None
    assert a["simulated"]["aoa"] is not None and a["speech_check_limit"]["childes_p95"] is not None
    assert "Simulated minus CHILDES" in (run / "realism.md").read_text()
    assert r["sources"]["childes:T"].startswith("add this corpus")      # TalkBank: every corpus is cited


def test_too_few_children_shown_not_compared(run, tmp_path):
    (tmp_path / "cha" / "T").mkdir(parents=True)
    for a in {t["child_age"] for t in _rows(run / "transcripts.jsonl")}:
        (tmp_path / "cha" / "T" / f"{a}.cha").write_text(CHA.replace("4;02.10", f"{a};00.00") * 3)
    build_childes(tmp_path / "cha", None, tmp_path / "ref.json")
    r = report(run, reference_path=tmp_path / "ref.json", norms_path=None)
    a = next(iter(r["realism"]["by_age"].values()))
    assert a["childes"]["children"] == 1 and a["comparison"] is None


def test_cli_bad_run(tmp_path):
    assert main(["--run", "nope", "--results", str(tmp_path)]) == 2


def test_changed_scenario_counts_only_fallback(run, tmp_path):
    """A scenario edited since the run (e.g. a pre-A16 pilot) can't be split into turns again; F-015."""
    m = json.loads((run / "manifest.json").read_text())
    m["scenarios"]["abil-001"] = "0" * 16
    (run / "manifest.json").write_text(json.dumps(m))
    r = _report(run, tmp_path)
    o = r["fidelity"]["overall"]
    assert r["sample"]["scenarios_changed_since_run"] == ["abil-001"]
    assert o["scenario_changed"] == 3 and o["fallback_by_phase"]["unknown"]["of"] > 0
    assert not any(x["transcript_id"].startswith("abil-001") for x in r["fidelity"]["misses"]
                   if not x["what"].startswith("fallback"))
    assert "Scenario files changed since this run: abil-001" in (run / "realism.md").read_text()


def test_norms_xlsx_without_openpyxl(tmp_path):
    """The OSF .xlsx loads with the standard library, and a missing .csv falls back to the same name in .xlsx."""
    import zipfile

    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
             '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="C2"><v>2.5</v></c></row>'
             '<row r="3"><c r="A3" t="inlineStr"><is><t>Cat</t></is></c><c r="C3"><v>NA</v></c></row>'
             '</sheetData></worksheet>')
    strings = ''.join(f"<si><t>{s}</t></si>" for s in ("Word", "OccurTotal", "Rating.Mean", "dog"))
    with zipfile.ZipFile(tmp_path / "kuperman_aoa.xlsx", "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/sharedStrings.xml",
                   f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{strings}</sst>')
    assert load_norms(tmp_path / "kuperman_aoa.xlsx") == {"dog": 2.5}
    assert load_norms(tmp_path / "kuperman_aoa.csv") == {"dog": 2.5}
