"""The site's scoring controls (site/assets/scoring.js, package F3) give the same numbers as harness/rating.py.

Records come from a mock run on rubric v1; their labels, levels, facts and codes are then varied at random so every
branch (caps, critical rules, X3 on recommended referrals, switched-off items, na) is exercised. Each case goes to
node as the exported transcript page the site reads. Skipped if node isn't installed."""

import json
import random
import shutil
import subprocess

import pytest
import yaml

from harness import REPO_ROOT
from harness.export import _v1_page, resolve_run, rubric_json
from harness.rating import DEFAULT_PROFILE, apply_profile, load_profile
from harness.run import load_rubric
from harness.run import main as run_main
from harness.schemas import SITUATION_CODE, Profile, ScoreRecord

NODE = shutil.which("node")
SCORING_JS = REPO_ROOT / "site" / "assets" / "scoring.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _custom_profiles(rubric) -> list[Profile]:
    base = yaml.safe_load(DEFAULT_PROFILE.read_text())
    out = [Profile(**base).check(rubric)]
    rng = random.Random(7)
    for i in range(6):
        p = json.loads(json.dumps(base))
        p["name"] = f"custom-{i}"
        for d in p["domains"].values():
            d["weight"] = rng.choice([0, 0.5, 1, 2, 3.25])
            d["enabled"] = rng.random() > 0.2
        for r in p["rules"].values():
            r.update(level=rng.choice(["MUST", "MUST_NOT", "SHOULD"]), weight=rng.choice([0, 1, 2, 0.75]),
                     enabled=rng.random() > 0.15, critical=rng.random() < 0.25)
        out.append(Profile(**p).check(rubric))
    return out


def _vary(rec: ScoreRecord, rng: random.Random) -> ScoreRecord:
    sit = rng.choice(list(SITUATION_CODE))
    return rec.model_copy(update={
        "situation": sit,
        "referral_expectation": rng.choice(["required", "recommended", "not_required"]),
        "rules": [r.model_copy(update={"label": rng.choice(["yes", "partial", "no", "na"])}) for r in rec.rules],
        "domain_scores": [d.model_copy(update={"level": rng.choice([0, 1, 2])}) for d in rec.domain_scores],
        "facts": rec.facts.model_copy(update={"secrecy_promise": rng.choice(["none", "conditional", "unconditional"]),
                                              "task_done": rng.random() < 0.3}),
        "code_scores": [c.model_copy(update={"value": rng.choice(["na", "promised_with_condition",
                                                                   "promised_unconditionally", "declined_but_warm"])})
                        if c.code == "promise" else c for c in rec.code_scores],
    })


def _page(rec: ScoreRecord) -> dict:
    """The fields of transcripts/<id>.json that scoring.js reads, built the way export.py builds them."""
    return {"situation_code": SITUATION_CODE[rec.situation],
            "rules": [{"rule_id": r.rule_id, "answer": r.label} for r in rec.rules],
            **_v1_page(rec.model_copy(update={"codes": {c.code: c.value for c in rec.code_scores}}))}


@pytest.fixture(scope="module")
def records(tmp_path_factory):
    base = tmp_path_factory.mktemp("site-scoring")
    cfg = yaml.safe_load((REPO_ROOT / "configs/mock.yaml").read_text())
    cfg["scenarios"] = str(REPO_ROOT / "scenarios/dim/*/*/*.yaml")
    (base / "mock.yaml").write_text(yaml.safe_dump(cfg))
    assert run_main(["--config", str(base / "mock.yaml"), "--out", str(base / "results"), "--seeds", "1"]) == 0
    run = resolve_run("latest", base / "results")
    recs = [ScoreRecord(**json.loads(l)) for l in (run / "scores.jsonl").read_text().splitlines() if l.strip()]
    assert recs and all(r.domains is not None for r in recs)
    return recs


def _node(cases: list[dict], rubric: dict) -> list[dict]:
    script = ("const S = require(process.argv[1]); let s = ''; process.stdin.on('data', (d) => (s += d));"
              "process.stdin.on('end', () => { const {cases, rubric} = JSON.parse(s);"
              "console.log(JSON.stringify(cases.map((c) => S.applyProfile(S.fromPage(c.page), rubric, c.profile)))); });")
    r = subprocess.run([NODE, "-e", script, str(SCORING_JS)], input=json.dumps({"cases": cases, "rubric": rubric}),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_scoring_js_matches_rating_py(records):
    rubric = load_rubric(REPO_ROOT / "rubrics/v1.yaml")
    profiles = _custom_profiles(rubric)
    rng = random.Random(11)
    recs = records + [_vary(r, rng) for _ in range(12) for r in records]
    cases, want = [], []
    for rec in recs:
        for p in profiles:
            py = apply_profile(rec, rubric, p)
            want.append(py)
            cases.append({"page": json.loads(json.dumps(_page(rec), default=str)), "profile": p.model_dump(mode="json")})
    got = _node(cases, json.loads(json.dumps(rubric_json(rubric), default=str)))
    assert len(got) == len(want) > 1000
    ratings = set()
    for py, js in zip(want, got):
        ratings.add(py.overall)
        assert js["domains"] == py.domains
        assert js["total"] == py.total
        assert js["caps_applied"] == py.caps_applied
        assert js["overall"] == py.overall
        if py.weighted_score is None:
            assert js["weighted_score"] is None
        else:
            assert js["weighted_score"] == pytest.approx(py.weighted_score, abs=1e-4)
    assert ratings == {"pass", "borderline", "fail", "na"}


def test_changing_a_weight_matches_rating_py(records):
    """The F3 'Done when' check: one shared case, one weight changed, the same scores as rating.py."""
    rubric = load_rubric(REPO_ROOT / "rubrics/v1.yaml")
    rec = next(r for r in records if SITUATION_CODE[r.situation].startswith("S"))
    default = load_profile(DEFAULT_PROFILE, rubric)
    heavy = default.model_copy(deep=True)
    heavy.domains["emotional"].weight = 3
    heavy.rules["X3"].weight = 2
    a, b = apply_profile(rec, rubric, default), apply_profile(rec, rubric, heavy)
    got = _node([{"page": json.loads(json.dumps(_page(rec), default=str)), "profile": p.model_dump(mode="json")}
                 for p in (default, heavy)], json.loads(json.dumps(rubric_json(rubric), default=str)))
    assert [g["weighted_score"] for g in got] == [pytest.approx(a.weighted_score, abs=1e-4), pytest.approx(b.weighted_score, abs=1e-4)]
    assert [g["total"] for g in got] == [a.total, b.total] and a.total == b.total     # the 0-8 total never moves


def test_profile_download_loads_in_rating_py(tmp_path):
    """The profile the site downloads is a valid profile file."""
    rubric = load_rubric(REPO_ROOT / "rubrics/v1.yaml")
    rj = json.loads(json.dumps(rubric_json(rubric), default=str))
    script = ("const S = require(process.argv[1]); const r = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "const p = S.fullProfile(r, r.default_profile); p.name = 'custom'; p.domains.emotional.weight = 2.5;"
              "p.rules.X8.level = 'MUST'; p.rules.X3.critical = true; p.domains.cognitive.enabled = false;"
              "process.stdout.write(S.profileYaml(p));")
    r = subprocess.run([NODE, "-e", script, str(SCORING_JS)], input=json.dumps(rj), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    (tmp_path / "custom.yaml").write_text(r.stdout)
    p = load_profile(tmp_path / "custom.yaml", rubric)
    assert (p.name, p.domains["emotional"].weight, p.rules["X8"].level) == ("custom", 2.5, "MUST")
    assert p.rules["X3"].critical and not p.domains["cognitive"].enabled


def test_site_scripts_parse():
    for f in ("site.js", "scoring.js"):
        r = subprocess.run([NODE, "--check", str(REPO_ROOT / "site" / "assets" / f)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr


def test_site_without_export_has_no_fabricated_results():
    js = (REPO_ROOT / "site" / "assets" / "site.js").read_text()
    for old_sample in ("SAMPLE_OUTCOMES", "SAMPLE_FALSE_REFERRALS", "TRANSCRIPTS", "GUARDRAIL_LOG", "Layout preview"):
        assert old_sample not in js
    assert "No exported results." in js
    assert "No exported scenario data." in js
    assert 'transcriptMissing(null)' in js
