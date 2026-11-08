import csv, json, os, subprocess, sys
from datetime import datetime, timedelta, timezone

from rca import judge, llm, rules
from rca.evaluate import evidence_grounded, evidence_valid, wilson
from rca.incident import Incident, load_incident

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def stamp(s):
    return (T0 + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def rec(s, service="backend", msg="GET /x 200", level="INFO"):
    return {"ts": stamp(s), "service": service, "level": level, "msg": msg}


def incident(window, baseline=None):
    return Incident("t", T0, T0 + timedelta(seconds=45), window=window,
                    baseline=[rec(-10)] if baseline is None else baseline)


def test_line_just_after_inject_does_not_hide_a_dead_service():
    assert incident([rec(1)]).silent() == ["backend"]


def test_service_still_logging_is_not_silent():
    assert incident([rec(1), rec(30)]).silent() == []


def test_service_absent_from_baseline_is_not_silent():
    assert incident([], baseline=[]).silent() == []


def test_rules_call_a_dead_backend():
    assert rules.diagnose(incident([rec(1)]))["component"] == "backend"


def test_prompt_reports_the_dead_backend():
    assert "completely silent now: backend" in llm.build_prompt(incident([rec(1)]))


def error_incident():
    return incident([rec(2, msg="DESYNC: finalizeSale tx 0xabc buyer is missing (null)", level="ERROR"), rec(30)])


def test_rendered_prefix_and_elision_are_grounded_but_not_exact():
    quote = ["[backend] ERROR (x1) DESYNC: finalizeSale tx ... buyer is missing (null)"]
    assert not evidence_valid(error_incident(), quote)
    assert evidence_grounded(error_incident(), quote)


def test_invented_and_junk_quotes_are_not_grounded():
    assert not evidence_grounded(error_incident(), ["connection refused by postgres"])
    assert not evidence_grounded(error_incident(), ['contract_code_bytes": 0]}</think></think>{'])


def test_one_bad_quote_fails_the_trial():
    assert not evidence_grounded(error_incident(), ["buyer is missing", "made up"])


def test_tool_output_counts_as_shown():
    calls = [{"result": '"sessions_waiting_on_lock": 2'}]
    assert evidence_grounded(incident([rec(30)]), ['sessions_waiting_on_lock": 2'], calls)


def test_wilson_matches_reported_interval():
    lo, hi = wilson(28, 36)
    assert round(lo, 3) == 0.619 and round(hi, 3) == 0.883


def make_trial(root):
    d = root / "t1-backend_crash"
    d.mkdir()
    (d / "truth.json").write_text(json.dumps({
        "trial_id": "t1", "name": "backend_crash", "category": "resource", "component": "backend",
        "root_cause": "killed", "inject_ts": stamp(0), "revert_ts": stamp(45)}))
    (d / "logs.jsonl").write_text("\n".join(json.dumps(r) for r in [rec(-10), rec(1)]) + "\n")
    return d


def test_load_incident_splits_baseline_and_window(tmp_path):
    inc = load_incident(str(make_trial(tmp_path)))
    assert len(inc.baseline) == 1 and len(inc.window) == 1


def test_evaluator_end_to_end_with_rules(tmp_path):
    trials = tmp_path / "trials"
    trials.mkdir()
    make_trial(trials)
    out = tmp_path / "res"
    p = subprocess.run([sys.executable, "-m", "rca.evaluate", "--diagnoser", "rules", "--trials", str(trials), "--out", str(out)],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": "."})
    assert p.returncode == 0, p.stderr
    assert "overall accuracy 100% over 1 trials" in p.stdout
    row = json.loads(next(out.iterdir()).read_text().splitlines()[0])
    assert row["correct"] and row["evidence_grounded"]


def test_judge_prompt_hides_the_diagnoser():
    row = {"true_root_cause": "killed", "pred": {"root_cause": "down", "evidence": ["x"], "model": "SECRET", "rule": "SECRET"}}
    assert "SECRET" not in judge.build_prompt(row)


def test_judge_agreement_counts_only_labelled_rows(tmp_path):
    path = tmp_path / "j.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, ["judge", "human"]); w.writeheader()
        w.writerows([{"judge": "match", "human": "match"}, {"judge": "wrong", "human": "partial"}, {"judge": "match", "human": ""}])
    assert judge.agreement(str(path)) == (1, 2)
