"""B15: the scoring packet and the CSV import. Mock models only."""

import csv
import json
import shutil
from pathlib import Path

import pytest
import yaml

from harness import REPO_ROOT
from harness import validate_judge as vj
from harness.labels import Chat, LabelError, applicable, assign, build, bundle_page, main
from harness.run import load_personas, load_rubric
from harness.run import main as run_main
from harness.schemas import Transcript
from harness.score import rescore
from harness.tools.agreement import compute_agreement

V1 = REPO_ROOT / "rubrics/v1.yaml"
RUBRIC = load_rubric(V1)
NO_PAGE = REPO_ROOT / "site/no-such-page.html"


def _run(folder, seed, rubric):
    cfg = yaml.safe_load((REPO_ROOT / "configs/mock.yaml").read_text()) | {"rubric": rubric, "seeds": [seed]}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "c.yaml").write_text(yaml.safe_dump(cfg))
    assert run_main(["--config", str(folder / "c.yaml"), "--out", str(folder)]) == 0


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Two mock runs under one folder: seed 1 scored with v1, seed 2 with v0 (so without usable judge scores)."""
    d = tmp_path_factory.mktemp("labels") / "runs"
    _run(d, 1, "rubrics/v1.yaml")
    _run(d, 2, "rubrics/v0.yaml")
    return d


@pytest.fixture(scope="module")
def made(runs, tmp_path_factory):
    out = tmp_path_factory.mktemp("packet") / "labeling" / "p"
    return out, build(runs, V1, out, page=NO_PAGE)


@pytest.fixture
def folder(made, tmp_path):
    shutil.copytree(made[0], tmp_path / "labeling" / "p")
    return tmp_path / "labeling" / "p"


@pytest.fixture(autouse=True)
def fewer_resamples(monkeypatch):
    """The real import resamples 1,000 times per question, which is slow to repeat in every test."""
    monkeypatch.setattr(vj, "compute_agreement", lambda a, b, ordinal=False: compute_agreement(a, b, ordinal=ordinal, n_boot=40))


def _packet(d):
    return json.loads((d / "send/packet.json").read_text())


def _rows(p):
    with open(p, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _save(p, rows):
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _filled(d, who):
    """That scorer's sheet, filled with answers that vary by chat and differ between scorers now and then."""
    key = {r["chat_id"]: r["transcript_id"] for r in _rows(d / "private/blind_key.csv")}
    rows = _rows(d / f"send/labels_{who}.csv")
    ids = sorted(key, key=key.get)
    qs = {q["id"]: [v["value"] for v in q["values"]] for q in _packet(d)["questions"]}
    for r in rows:
        n = ids.index(r["chat_id"])
        for q, values in qs.items():
            if r[q] == "":
                r[q] = values[(n + (who == "B" and n % 5 == 0)) % len(values)]
    return rows


def _return(d, **sheets):
    for who in "AB":
        _save(d / f"returned/labels_{who}.csv", sheets.get(who) or _filled(d, who))


def _transcript_files(d):
    return sorted(d.rglob("transcripts.jsonl"))


def _read(f):
    return [json.loads(x) for x in f.read_text().splitlines()]


def _write(f, ts):
    f.write_text("".join(json.dumps(t) + "\n" for t in ts))


# -------------------------------------------------------------- the packet


def test_two_runs_make_one_packet(made):
    out, r = made
    p = _packet(out)
    assert r["chats"] == len(p["chats"]) == 42 and r["judged"] == 21 and r["overlap"] == 42 and not r["page"]
    assert sorted(p["assignment"]["A"]) == sorted(p["assignment"]["B"]) == [c["id"] for c in p["chats"]]
    assert p["assignment"]["A"] != p["assignment"]["B"]                              # another order for each scorer
    assert p["rubric_hash"] == f"v1-{RUBRIC.labels_hash()}"
    assert {f.name for f in (out / "send").iterdir()} == {"packet.json", "packet_A.md", "packet_B.md", "labels_A.csv", "labels_B.csv"}
    assert {f.name for f in (out / "private").iterdir()} == {"blind_key.csv", "judge_labels.csv", "rubric.yaml"}
    sheet = _rows(out / "send/labels_A.csv")
    assert list(sheet[0]) == p["csv_header"] and [r["chat_id"] for r in sheet] == p["assignment"]["A"]
    chats = {c["id"]: c for c in p["chats"]}
    for row in sheet:
        assert all((row[q["id"]] == "") == (q["id"] in chats[row["chat_id"]]["questions"]) for q in p["questions"])


def test_nothing_in_send_unblinds(made):
    out, _ = made
    key = _rows(out / "private/blind_key.csv")
    secret = {r[k] for r in key for k in ("agent_name", "transcript_id", "run_id", "run_folder", "scenario_id")}
    secret |= {"mock:", "provisional", "overall", "beyond_ability", "personal_data", "over_attachment", "distressing_disclosure", "unsafe_request"}
    for f in (out / "send").iterdir():
        text = f.read_text()
        assert not [s for s in secret if s in text], f.name
    assert set(_packet(out)["chats"][0]) == {"id", "child_age", "length_guide", "questions", "last_scored_turn", "guidance", "turns"}
    for who in "AB":
        for row in _rows(out / f"send/labels_{who}.csv"):
            assert all(v in ("", "-") for k, v in row.items() if k not in ("packet_id", "rubric_hash", "scorer", "chat_id"))
    ids = [r["chat_id"] for r in key]
    assert len(set(ids)) == 42 and not any(ch.isdigit() for i in ids for ch in i)


def test_ids_are_random_and_replies_are_unchanged(runs, made, tmp_path):
    build(runs, V1, tmp_path / "again", page=NO_PAGE)
    assert {c["id"] for c in _packet(made[0])["chats"]}.isdisjoint(c["id"] for c in _packet(tmp_path / "again")["chats"])
    key = {r["chat_id"]: r["transcript_id"] for r in _rows(made[0] / "private/blind_key.csv")}
    saved = {t["transcript_id"]: t for f in _transcript_files(runs) for t in _read(f)}
    for c in _packet(made[0])["chats"]:
        assert [x["text"] for x in c["turns"]] == [x["text"] for x in saved[key[c["id"]]]["turns"]]


def test_questions_that_apply():
    personas, _ = load_personas("scenarios/**/*.yaml")
    t = Transcript(transcript_id="t", run_id="r", scenario_id="s", situation="distressing_disclosure", child_age=4, agent_name="a",
                   agent_model="m", seed=1, ended_because="x", turns=[])
    p = personas["dist-003"]
    got = applicable(RUBRIC, t, p)
    assert {"X3", "X7", "X8", "X9", "promise", "pushback"} <= set(got) and "follow_up" not in got
    p.referral.expectation = "not_required"
    assert "X3" not in applicable(RUBRIC, t, p)
    assert "follow_up" in applicable(RUBRIC, t, None)                                  # no scenario file: the person decides
    c = applicable(RUBRIC, t.model_copy(update={"situation": "control"}), personas["ctrl-001"])
    assert not [q for q in c if q[0] in "XS"] and "over_escalation" in c


def test_caregiver_message_is_context(runs, tmp_path):
    shutil.copytree(_transcript_files(runs)[0].parent, tmp_path / "r")
    f = tmp_path / "r/transcripts.jsonl"
    t = _read(f)[0]
    t["turns"] = [{"turn": 0, "speaker": "caregiver", "text": "Leo is 4."}, {"turn": 0, "speaker": "agent", "text": "Thank you."}] + t["turns"]
    _write(f, [t])
    build(tmp_path / "r", V1, tmp_path / "o", page=NO_PAGE)
    turns = _packet(tmp_path / "o")["chats"][0]["turns"]
    assert [(x["speaker"], x["scored"]) for x in turns[:4]] == [("caregiver", False), ("chatbot", False), ("child", False), ("chatbot", True)]
    assert "**[context, not scored] CAREGIVER:**" in (tmp_path / "o/send/packet_B.md").read_text()


def test_incomplete_chats_are_skipped_and_may_be_redone(runs, tmp_path):
    shutil.copytree(runs, tmp_path / "r")
    f = _transcript_files(tmp_path / "r")[0]
    ts = _read(f)
    again = dict(ts[0])
    ts[0]["complete"] = False
    ts[1]["ended_because"] = "agent error: x"
    _write(f, ts)
    (tmp_path / "r/redo").mkdir()
    _write(tmp_path / "r/redo/transcripts.jsonl", [again])              # no manifest, no scores
    r = build(tmp_path / "r", V1, tmp_path / "o", page=NO_PAGE)
    assert r["chats"] == 41 and len(r["skipped"]) == 2 and "incomplete" in r["skipped"][0]


def test_duplicate_chats_stop_it(runs, tmp_path):
    shutil.copytree(runs, tmp_path / "r")
    shutil.copytree(_transcript_files(runs)[0].parent, tmp_path / "r/copy")
    with pytest.raises(LabelError, match="is in both"):
        build(tmp_path / "r", V1, tmp_path / "o", page=NO_PAGE)
    assert not (tmp_path / "o").exists()


def test_a_reply_that_names_the_app_is_reported_not_edited(runs, tmp_path):
    shutil.copytree(runs, tmp_path / "r")
    f = _transcript_files(tmp_path / "r")[0]
    ts = _read(f)
    ts[0]["turns"][1]["text"] = "I'm ChatGPT!"
    _write(f, ts)
    r = build(tmp_path / "r", V1, tmp_path / "o", page=NO_PAGE)
    assert len(r["names_itself"]) == 1 and "I'm ChatGPT!" in (tmp_path / "o/send/packet.json").read_text()


def _chats(agents=3, scenarios=4):
    return [Chat(Transcript(transcript_id=f"s{s}__a{a}", run_id="r", scenario_id=f"s{s}", situation="control", child_age=6, agent_name=f"a{a}",
                            agent_model="m", seed=1, turns=[], ended_because="x"), None, None, None) for a in range(agents) for s in range(scenarios)]


@pytest.mark.parametrize("overlap", [0, 3, 6, 7, 12])
def test_overlap_is_balanced_and_repeatable(overlap):
    chats = _chats()
    got = {w: [c.t.transcript_id for c in cs] for w, cs in assign(chats, overlap).items()}
    assert got == {w: [c.t.transcript_id for c in cs] for w, cs in assign(list(reversed(chats)), overlap).items()}
    shared = set(got["A"]) & set(got["B"])
    assert len(shared) == overlap and set(got["A"]) | set(got["B"]) == {c.t.transcript_id for c in chats}
    assert abs(len(got["A"]) - len(got["B"])) <= 1 and len(set(got["A"])) == len(got["A"])
    n = [sum(i.endswith(a) for i in shared) for a in ("a0", "a1", "a2")]
    assert max(n) - min(n) <= 1
    n = [sum(i.startswith(s) for i in shared) for s in ("s0", "s1", "s2", "s3")]
    assert max(n) - min(n) <= 1


def test_bad_overlap():
    for n in (-1, 13):
        with pytest.raises(LabelError, match="0 to 12"):
            assign(_chats(), n)


def test_command_line(runs, tmp_path, capsys):
    args = ["--from", str(runs), "--results", str(tmp_path), "--out", "x"]
    assert main(args) == 0 and (tmp_path / "labeling/x/send/packet.json").exists()
    assert main(args) == 1 and "already exists" in capsys.readouterr().err
    assert main([*args[:-1], "../x"]) == 1 and main(["--results", str(tmp_path), "--out", "y"]) == 1
    assert main([*args[:-1], "v0", "--rubric", "rubrics/v0.yaml"]) == 1
    assert main(["--from", str(tmp_path / "labeling"), "--results", str(tmp_path), "--out", "z"]) == 1
    latest = _transcript_files(runs)[0].parent
    (tmp_path / "LATEST").write_text(latest.name)
    shutil.copytree(latest, tmp_path / latest.name)
    assert main(["--results", str(tmp_path), "--out", "latest"]) == 0
    assert len(_packet(tmp_path / "labeling/latest")["chats"]) == 21


# ---------------------------------------------------------- the built-in page


def _embedded(html):
    return json.loads(html.split('id="packet">')[1].split("</script>")[0])


def test_score_page_is_one_offline_file(runs, tmp_path):
    (tmp_path / "site/assets").mkdir(parents=True)
    (tmp_path / "site/assets/site.css").write_text("body{color:red}")
    (tmp_path / "site/assets/score.js").write_text(r'const x = "\1</script>";')
    page = tmp_path / "site/score.html"
    page.write_text('<html><head><link rel="preconnect" href="https://fonts.example"><link rel="stylesheet" href="https://fonts.example/a">'
                    '<link rel="stylesheet" href="assets/site.css"></head><body><script>let y = 1</script>'
                    '<script src="assets/score.js" defer></script><main></main></body></html>')
    assert build(runs, V1, tmp_path / "o", page=page)["page"]
    html = (tmp_path / "o/send/score.html").read_text()
    assert "https://fonts" not in html and "src=" not in html and "body{color:red}" in html and r'"\1<\/script>"' in html
    assert html.index("<main>") < html.index("const x") and "let y = 1" in html
    assert _embedded(html) == _packet(tmp_path / "o")
    assert not [r["agent_name"] for r in _rows(tmp_path / "o/private/blind_key.csv") if r["agent_name"] in html]
    evil = bundle_page(page, {"chats": ["</script><b>"]})
    assert "</script><b>" not in evil and _embedded(evil) == {"chats": ["</script><b>"]}


@pytest.mark.parametrize("tag", ['<script src="https://x.example/a.js"></script>', '<script src="assets/none.js"></script>',
                                 '<script src="../secret.js"></script>', '<link rel="stylesheet" href="assets/none.css">'])
def test_a_page_that_would_not_work_offline_is_refused(runs, tmp_path, tag):
    (tmp_path / "site").mkdir()
    (tmp_path / "secret.js").write_text("x")
    (tmp_path / "site/score.html").write_text(f"<html><head>{tag}</head><body></body></html>")
    with pytest.raises(LabelError):
        build(runs, V1, tmp_path / "o", page=tmp_path / "site/score.html")
    assert not (tmp_path / "o").exists()


# ---------------------------------------------------------------- the import


def test_two_csvs_give_kappa_per_domain_with_a_range(folder, capsys):
    _return(folder)
    before = {f: f.read_bytes() for f in (folder / "returned").iterdir()}
    assert vj.main(["p", "--results", str(folder.parent.parent)]) == 0
    assert "cognitive" in capsys.readouterr().out
    r = json.loads((folder / "private/agreement.json").read_text())
    assert set(r["comparisons"]) == {"A_vs_B", "A_vs_judge", "B_vs_judge"}
    for d in RUBRIC.domains:
        m = r["comparisons"]["A_vs_B"]["domains"][d.id]
        assert m["n"] == 42 and -1 <= m["ci95"][0] <= m["kappa"] <= m["ci95"][1] <= 1 and m["raw_agreement"] < 1
    j = r["comparisons"]["A_vs_judge"]
    assert j["chats"] == 21 and j["only_first"] == 21 and "missed" in j["rating"] and "missed" in j["codes"]["over_escalation"]
    h = json.loads((folder / "private/human_labels.json").read_text())
    assert len(h["chats"]) == 42 and set(h["chats"]["dist-003__careful-bot__s1"]["labels"]) == {"A", "B"}
    assert {f: f.read_bytes() for f in (folder / "returned").iterdir()} == before       # never edited


def test_answering_like_the_judge_gives_full_agreement_and_the_same_rating(folder):
    judge = {r["chat_id"]: r for r in _rows(folder / "private/judge_labels.csv")}
    _return(folder, **{w: [dict(judge[r["chat_id"]], scorer=w) if r["chat_id"] in judge else r for r in _filled(folder, w)] for w in "AB"})
    r = vj.import_labels(folder)["comparisons"]["A_vs_judge"]
    ms = [*r["domains"].values(), *r["codes"].values(), *r["rules"].values(), r["rating"]]
    assert all(m["raw_agreement"] in (1, None) and not m.get("missed") and not m.get("false_alarms") for m in ms)
    h = json.loads((folder / "private/human_labels.json").read_text())["chats"]
    checked = 0
    for d in {r["run_folder"] for r in _rows(folder / "private/blind_key.csv")}:
        for s in _read(Path(d) / "scores.jsonl"):
            if s["rubric_version"] == "v1":
                checked += 1
                assert h[s["transcript_id"]]["ratings"]["A"] == s["overall"]
    assert checked == 21


def test_missed_cases_and_false_alarms():
    a = {"c1": {"over_escalation": "1", "X7": "no"}, "c2": {"over_escalation": "0", "X7": "yes"}, "c3": {"over_escalation": "1", "X7": "na"}, "c9": {}}
    b = {"c1": {"over_escalation": "0", "X7": "no"}, "c2": {"over_escalation": "1", "X7": "partial"}, "c3": {"over_escalation": "1", "X7": "no"}}
    r = vj.compare(RUBRIC, a, b, ({c: "fail" for c in a}, {"c1": "pass", "c2": "fail", "c3": "na"}))
    o, x7 = r["codes"]["over_escalation"], r["rules"]["X7"]
    assert (o["problems"], o["missed"], o["false_alarms"]) == (2, 1, 1) and r["only_first"] == 1
    assert (x7["problems"], x7["missed"], x7["false_alarms"], x7["n_dropped"], x7["within_1"]) == (1, 0, 1, 1, 1.0)
    assert r["rating"]["confusion"] == {"fail->pass": 1, "fail->fail": 1} and (r["rating"]["missed"], r["rating"]["within_1"]) == (1, 0.5)


def test_domain_scores_are_ordered():
    a = {i: str(i % 3) for i in range(30)}
    near = {i: "1" if i % 9 == 0 else v for i, v in a.items()}
    far = {i: "2" if i % 9 == 0 else v for i, v in a.items()}
    ka, kn, kf = (compute_agreement(a, b, ordinal=True, n_boot=50)["kappa"] for b in (a, near, far))
    assert ka == 1.0 > kn > kf


def test_excel_quirks_are_accepted(folder):
    _return(folder)
    p = folder / "returned/labels_A.csv"
    rows = _rows(p)
    rows[0]["note"] = "Unsure, see\nturn 2"
    for r in rows:
        r.update({k: f" {v.upper()} " for k, v in r.items() if v in ("yes", "no")})
    _save(p, rows)
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes() + b",,,\r\n")
    vj.import_labels(folder)
    h = json.loads((folder / "private/human_labels.json").read_text())["chats"]
    assert [n for c in h.values() for n in c["notes"].values()] == ["Unsure, see\nturn 2"]


def _edit(**cells):
    return lambda rows: rows[0].update(cells)


@pytest.mark.parametrize("change, message", [
    (_edit(cognitive=""), "cognitive: blank"), (_edit(cognitive="3"), "'3' isn't one of 2, 1, 0"),
    (_edit(over_escalation="na"), "over_escalation: 'na' isn't one of"),
    (_edit(packet_id="Pother"), "made for another packet"), (_edit(rubric_hash=""), "another version of the rubric"),
    (_edit(scorer="B"), "scorer must be one of"), (_edit(scorer="C"), "scorer must be one of"),
    (_edit(chat_id="Cnope"), "Cnope isn't in this packet"), (lambda r: r[0].update(chat_id=r[1]["chat_id"]), "appears twice"),
    (lambda r: r.pop(), "missing chats:"), (lambda r: [x.pop("emotional") for x in r], "missing columns: emotional"),
    (lambda r: [x.update(extra="1") for x in r], "unknown columns: extra"),
    (lambda r: next(x for x in r if x["X3"] == "-").update(X3="yes"), "doesn't apply to this chat"),
])
def test_bad_csvs_are_refused_with_reasons(folder, change, message):
    rows = _filled(folder, "A")
    change(rows)
    _return(folder, A=rows)
    with pytest.raises(LabelError) as e:
        vj.import_labels(folder)
    assert "labels_A.csv" in str(e.value) and message in str(e.value)
    assert not (folder / "private/human_labels.json").exists()


def test_unreadable_duplicate_and_missing_files(folder):
    _return(folder)
    p = folder / "returned/labels_B.csv"
    good = p.read_text()
    for raw, message in ((b"", "empty"), (good.replace(",", ";").encode(), "commas"), (b"\xff\xfe\x00", "UTF-8")):
        p.write_bytes(raw)
        with pytest.raises(LabelError, match=message):
            vj.import_labels(folder)
    p.write_text(good)
    (folder / "returned/copy of B.csv").write_text(good)
    with pytest.raises(LabelError, match="scorer B is already in"):
        vj.import_labels(folder)
    (folder / "returned/copy of B.csv").unlink()
    p.unlink()
    with pytest.raises(LabelError, match="no CSV from scorer B yet"):
        vj.import_labels(folder)


def test_chats_assigned_to_the_other_scorer(runs, tmp_path):
    d = tmp_path / "p"
    build(runs, V1, d, overlap=10, page=NO_PAGE)
    _return(d)
    assert vj.import_labels(d)["comparisons"]["A_vs_B"]["chats"] == 10
    rows, who = _filled(d, "B"), _packet(d)["assignment"]
    mine = next(i for i in who["A"] if i not in who["B"])
    rows[0]["chat_id"] = mine
    _return(d, B=rows)
    with pytest.raises(LabelError, match=f"{mine} isn't assigned to B"):
        vj.import_labels(d)


def test_agreed_answers_are_kept_beside_the_originals(folder):
    _return(folder)
    first = vj.import_labels(folder)
    p = folder / "private/disagreements.csv"
    rows = _rows(p)
    assert len(rows) == first["disagreements"] > 0 and all(r["A"] != r["B"] and r["agreed"] == "" for r in rows)
    rows[0]["agreed"] = rows[0]["A"]
    _save(p, rows)
    again = vj.import_labels(folder)
    assert again["settled"] == 1 and again["comparisons"] == first["comparisons"]
    h = json.loads((folder / "private/human_labels.json").read_text())["chats"]
    c = next(c for c in h.values() if c["chat_id"] == rows[0]["chat_id"])
    assert c["agreed"] == {rows[0]["question"]: rows[0]["A"]} and c["labels"]["B"][rows[0]["question"]] == rows[0]["B"]
    assert _rows(p)[0]["agreed"] == rows[0]["A"]
    rows[0]["agreed"] = "nonsense"
    _save(p, rows)
    with pytest.raises(LabelError, match="isn't one of"):
        vj.import_labels(folder)
    rows[0].update(agreed=rows[0]["B"], A="changed")
    _save(p, rows)
    with pytest.raises(LabelError, match="have changed since"):
        vj.import_labels(folder)
    assert _rows(p) == rows                                 # what people settled is never thrown away


def test_without_judge_scores_only_people_are_compared(folder, capsys):
    (folder / "private/judge_labels.csv").unlink()
    _return(folder)
    assert vj.main(["p", "--results", str(folder.parent.parent)]) == 0
    assert "only the two people are compared" in capsys.readouterr().out
    assert list(json.loads((folder / "private/agreement.json").read_text())["comparisons"]) == ["A_vs_B"]
    assert vj.main(["nope", "--results", str(folder.parent.parent)]) == 1


def test_judge_scores_from_a_later_scoring(runs, tmp_path, capsys):
    shutil.copytree(runs, tmp_path / "r")
    d = tmp_path / "labeling/p"
    build(tmp_path / "r", V1, d, page=NO_PAGE)
    _return(d)
    args = ["p", "--results", str(tmp_path), "--scores"]
    dirs = [f.parent for f in _transcript_files(tmp_path / "r")]
    assert vj.main([*args, "new"]) == 1 and capsys.readouterr().err.count("no /") == 2
    for x in dirs:
        rescore(x, rubric_file="rubrics/v1.yaml", label="new")
        rescore(x, rubric_file="rubrics/v0.yaml", label="old")
    f = dirs[0] / "rescore_new/scores.jsonl"
    _write(f, _read(f)[1:])                                  # the judge failed on one chat
    assert vj.main([*args, "new"]) == 0 and "rescore_new, 41 of 42" in capsys.readouterr().out
    r = json.loads((d / "private/agreement.json").read_text())
    assert (r["judge_scores"], r["judge_scored"], r["comparisons"]["A_vs_judge"]["only_first"]) == ("rescore_new", 41, 1)
    assert vj.main(args[:-1]) == 0
    r = json.loads((d / "private/agreement.json").read_text())
    assert (r["judge_scores"], r["judge_scored"]) == ("the copy made with the packet", 21)
    assert vj.main([*args, "old"]) == 1 and "another version of the rubric" in capsys.readouterr().err
    assert vj.main([*args, "../x"]) == 1
