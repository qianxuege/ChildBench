"""F4: the scoring page. Builds the real site/score.html into a mock packet, then runs the page's own CSV code
(site/assets/score.js) in node and checks what it writes with the import's checker. Mock models only.

The browser part (opening send/score.html by double-click, scorer pick, saved progress) is checked by hand."""

import json
import re
import shutil
import subprocess

import pytest
import yaml

from harness import REPO_ROOT
from harness import validate_judge as vj
from harness.labels import build
from harness.run import main as run_main

V1 = REPO_ROOT / "rubrics/v1.yaml"
PAGE = REPO_ROOT / "site/score.html"
JS = REPO_ROOT / "site/assets/score.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node isn't installed")


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    d = tmp_path_factory.mktemp("score")
    cfg = yaml.safe_load((REPO_ROOT / "configs/mock.yaml").read_text()) | {"rubric": "rubrics/v1.yaml", "seeds": [1]}
    (d / "c.yaml").write_text(yaml.safe_dump(cfg))
    assert run_main(["--config", str(d / "c.yaml"), "--out", str(d / "runs")]) == 0
    out = d / "labeling" / "p"
    assert build(d / "runs", V1, out, page=PAGE)["page"]
    return out


def _packet(d):
    return json.loads((d / "send/packet.json").read_text())


def _js(call: str, **data):
    """Run score.js's DOM-free functions in node: `call` is an expression over `S` (the module) and `D` (the data)."""
    code = f"const S = require({json.dumps(str(JS))}); const D = JSON.parse(require('fs').readFileSync(0, 'utf8'));" \
           f"process.stdout.write(JSON.stringify({call}));"
    r = subprocess.run([NODE, "-e", code], input=json.dumps(data), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _answers(p, who, skip=0):
    """An answer for every question of every chat of that scorer, except the first `skip` questions of their first chat."""
    vals = {q["id"]: [v["value"] for v in q["values"]] for q in p["questions"]}
    by = {c["id"]: c for c in p["chats"]}
    out = {}
    for n, cid in enumerate(p["assignment"][who]):
        qs = by[cid]["questions"][skip if n == 0 else 0:]
        out[cid] = {q: vals[q][(n + i) % len(vals[q])] for i, q in enumerate(qs)}
    return out


# ------------------------------------------------------------------ the page file


def test_the_page_ships_empty_and_works_offline(made):
    src = PAGE.read_text()
    assert 'id="packet"' not in src and "assets/score.js" in src and "assets/site.css" in src
    html = (made / "send/score.html").read_text()
    assert not re.search(r"""<(script|link)\b[^>]*\b(src|href)\s*=\s*["']?(https?:)?//""", html, re.I)
    assert "src=" not in html and "function makeCsv" in html and ".sc-grid" in html
    assert json.loads(html.split('id="packet">')[1].split("</script>")[0]) == _packet(made)
    assert html.index('id="packet"') < html.index("function makeCsv")     # the script runs after the packet is on the page


def test_the_script_never_writes_html_or_calls_out():
    js = JS.read_text()
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "fetch(", "XMLHttpRequest", "sendBeacon", "eval(", "WebSocket"):
        assert bad not in js, bad


def test_the_score_menu_and_the_reliability_link():
    assert '"score.html", "/score"' in JS.read_text()
    assert 'href="score.html"' in (REPO_ROOT / "site/judge-reliability.html").read_text()


# ------------------------------------------------------------------ its CSV, through the import's checker


@needs_node
@pytest.mark.parametrize("who", ["A", "B"])
def test_a_finished_sheet_imports_cleanly(made, tmp_path, who):
    p = _packet(made)
    notes = {p["assignment"][who][0]: 'Said "call 911", then, on a new line:\nasked again.'}
    csv_text = _js("S.makeCsv(D.p, D.who, D.a, D.n)", p=p, who=who, a=_answers(p, who), n=notes)
    f = tmp_path / f"labels_{who}.csv"
    f.write_text(csv_text, encoding="utf-8", newline="")
    scorer, answers, got_notes, errors = vj.check_returned(f, p)
    assert errors == [] and scorer == who
    assert answers == _answers(p, who) and got_notes == {k: v.strip() for k, v in notes.items()}
    assert _js("S.blanks(D.p, D.who, D.a)", p=p, who=who, a=_answers(p, who)) == []


@needs_node
def test_blanks_are_found_and_the_import_refuses_them(made, tmp_path):
    p = _packet(made)
    a = _answers(p, "A", skip=2)
    first = p["assignment"]["A"][0]
    missing = next(c for c in p["chats"] if c["id"] == first)["questions"][:2]
    assert _js("S.blanks(D.p, 'A', D.a)", p=p, a=a) == [{"id": first, "missing": missing}]
    f = tmp_path / "labels_A.csv"
    f.write_text(_js("S.makeCsv(D.p, 'A', D.a, {})", p=p, a=a), encoding="utf-8", newline="")
    assert vj.check_returned(f, p)[3] == [f"chat {first}, {q}: blank" for q in missing]


@needs_node
def test_a_downloaded_sheet_loads_back(made):
    p = _packet(made)
    a, n = _answers(p, "B", skip=3), {p["assignment"]["B"][1]: "a, b and \"c\""}
    got = _js("S.readSheet(D.p, 'B', S.makeCsv(D.p, 'B', D.a, D.n))", p=p, a=a, n=n)
    assert got == {"answers": a, "notes": n, "problems": []}


@needs_node
def test_the_blank_backup_sheet_from_make_labels_loads_too(made):
    p = _packet(made)
    got = _js("S.readSheet(D.p, 'A', D.t)", p=p, t=(made / "send/labels_A.csv").read_text())
    assert got["problems"] == [] and all(v == {} for v in got["answers"].values()) and len(got["answers"]) == len(p["assignment"]["A"])


@needs_node
@pytest.mark.parametrize("change, message", [
    (lambda t, p: t.replace(p["packet_id"], "Pother"), "another packet"),
    (lambda t, p: t.replace(p["rubric_hash"], "v0-x"), "another version of the rubric"),
    (lambda t, p: t.replace(",A,", ",B,"), "for scorer B, not A"),
    (lambda t, p: t.replace(",", ";"), "separated by commas"),
    (lambda t, p: t.replace("cognitive,", "", 1), "missing columns: cognitive"),
])
def test_the_wrong_sheet_is_refused(made, change, message):
    p = _packet(made)
    t = change(_js("S.makeCsv(D.p, 'A', D.a, {})", p=p, a=_answers(p, "A")), p)
    got = _js("S.readSheet(D.p, 'A', D.t)", p=p, t=t)
    assert got["answers"] == {} and message in got["problems"][0]


@needs_node
def test_a_bad_cell_is_skipped_and_named(made):
    p = _packet(made)
    t = _js("S.makeCsv(D.p, 'A', D.a, {})", p=p, a=_answers(p, "A"))
    first = p["assignment"]["A"][0]
    lines = t.split("\r\n")
    header = lines[0].lstrip("﻿").split(",")
    row = lines[1].split(",")
    row[header.index("cognitive")] = "7"
    lines[1] = ",".join(row)
    got = _js("S.readSheet(D.p, 'A', D.t)", p=p, t="\r\n".join(lines))
    assert got["problems"] == [f"chat {first}, cognitive: '7' isn't one of 2, 1, 0; left blank"]
    assert "cognitive" not in got["answers"][first] and len(got["answers"]) == len(p["assignment"]["A"])


@needs_node
def test_a_broken_packet_is_named():
    assert _js("S.checkPacket(D)", **{"x": 1})[0].startswith("this isn't a scoring packet")
