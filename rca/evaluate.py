#!/usr/bin/env python3
"""Score a diagnoser against ground truth across all trials.

  python3 -m rca.evaluate --diagnoser rules
  python3 -m rca.evaluate --diagnoser rules --trials trials --show-wrong

Metrics per trial:
  correct          predicted component == true component
  evidence_valid   every quote occurs verbatim in the window logs or a tool result (strict)
  evidence_grounded every quote occurs in what the diagnoser was shown (prompt, logs, tool
                   results), ignoring whitespace; "..." splits a quote into parts.
                   Both are vacuously true if nothing is quoted.
  latency_s        wall-clock time the diagnoser took
Writes results/<diagnoser>-<utc>.jsonl and prints a per-fault table.
"""
import argparse, glob, importlib, json, math, os, re, subprocess, sys, time
from collections import defaultdict

from rca import llm as L
from rca.incident import load_incident, load_truth


def evidence_valid(inc, evidence, tool_calls=()):
    """Every quote must occur verbatim in the window logs or in a tool result the diagnoser received."""
    texts = [inc.text(r) for r in inc.window] + [c.get("result", "") for c in tool_calls]
    return all(any(e and e in t for t in texts) for e in evidence)


def _squash(s):
    return " ".join(s.split())


def evidence_grounded(inc, evidence, tool_calls=()):
    """Was every quote really in front of the diagnoser? Tolerates formatting, not invention."""
    shown = _squash("\n".join([L.build_prompt(inc)] + [inc.text(r) for r in inc.window]
                              + [c.get("result", "") for c in tool_calls]))
    for quote in evidence:
        parts = [_squash(p) for p in re.split(r"\.\.\.|…", quote) if p.strip()]
        if not parts or not all(p in shown for p in parts):
            return False
    return True


def wilson(k, n, z=1.96):
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def log_mlflow(a, out, rows, metrics):
    import mlflow
    mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "sqlite:///mlflow.db"))
    mlflow.set_experiment("rca-bench")
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        sha = "unknown"
    with mlflow.start_run(run_name=f"{a.diagnoser}-{os.path.basename(a.trials.rstrip('/'))}"):
        mlflow.log_params({"diagnoser": a.diagnoser, "trials": a.trials, "n_trials": len(rows), "git_sha": sha,
                           "model": os.environ.get("RCA_MODEL", ""), "think": os.environ.get("RCA_THINK", ""),
                           "num_ctx": L.NUM_CTX})
        mlflow.log_metrics(metrics)
        mlflow.log_artifact(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--diagnoser", default="rules", help="module in rca/ exposing diagnose(incident)")
    ap.add_argument("--trials", default="trials")
    ap.add_argument("--out", default="results")
    ap.add_argument("--show-wrong", action="store_true")
    ap.add_argument("--mlflow", action="store_true", help="log params, metrics and the results file to MLflow")
    ap.add_argument("--resume", help="continue an earlier results .jsonl: finished trials are reused, "
                                     "crashed ones are retried, new rows are appended to the same file")
    a = ap.parse_args()

    diag = importlib.import_module(f"rca.{a.diagnoser}")
    dirs = sorted(d for d in glob.glob(os.path.join(a.trials, "*/")) if os.path.exists(os.path.join(d, "truth.json")))
    if not dirs:
        sys.exit(f"no trials with truth.json under {a.trials}/")

    # Checkpointing: every finished trial is appended to the results file immediately,
    # so a crash or sleep loses at most the trial in progress.
    done = {}
    if a.resume:
        out = a.resume
        with open(out) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue                      # half-written last line from a crash
                if r.get("pred_component") != "error":   # crashed trials get retried
                    done[r["trial_id"]] = r
        print(f"resuming {out}: {len(done)} trials already done", file=sys.stderr)
    else:
        os.makedirs(a.out, exist_ok=True)
        out = os.path.join(a.out, f"{a.diagnoser}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl")
    ckpt = open(out, "a")

    rows = []
    for i, d in enumerate(dirs, 1):
        truth = load_truth(d)
        if truth.get("trial_id") in done:
            rows.append(done[truth["trial_id"]])
            continue
        if "inject_error" in truth or "inject_ts" not in truth:
            print(f"skip {truth.get('trial_id', d)}: invalid trial", file=sys.stderr)
            continue
        inc = load_incident(d)
        t0 = time.time()
        try:
            pred = diag.diagnose(inc)
        except Exception as e:
            pred = {"component": "error", "root_cause": f"diagnoser crashed: {e}", "evidence": []}
        lat = time.time() - t0
        rows.append({
            "trial_id": truth["trial_id"], "fault": truth["name"], "category": truth["category"],
            "true_component": truth["component"], "pred_component": pred.get("component"),
            "correct": pred.get("component") == truth["component"],
            "evidence_valid": evidence_valid(inc, pred.get("evidence", []), pred.get("tool_calls", [])),
            "evidence_grounded": evidence_grounded(inc, pred.get("evidence", []), pred.get("tool_calls", [])),
            "n_quotes": len(pred.get("evidence", [])),
            "n_tool_calls": pred.get("n_tool_calls"),
            "latency_s": round(lat, 3), "window_records": len(inc.window),
            "pred": pred, "true_root_cause": truth["root_cause"],
        })
        ckpt.write(json.dumps(rows[-1]) + "\n"); ckpt.flush()
        r = rows[-1]
        print(f"[{i}/{len(dirs)}] {r['fault']:16} -> {str(r['pred_component']):10} "
              f"{'OK ' if r['correct'] else 'BAD'} {r['latency_s']:7.1f}s", file=sys.stderr, flush=True)
    ckpt.close()

    if a.resume:   # rewrite the file once, de-duplicated: one row per trial (retried crashes replaced)
        with open(out, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    by = defaultdict(list)
    for r in rows:
        by[r["fault"]].append(r)
    print(f"\n{'fault':16} {'true':8} {'n':>3} {'acc':>5}  {'exact':>5}  {'grnd':>5}  predicted")
    for fault in sorted(by):
        rs = by[fault]
        acc = sum(r["correct"] for r in rs) / len(rs)
        ev = sum(r["evidence_valid"] for r in rs) / len(rs)
        gr = sum(r.get("evidence_grounded", False) for r in rs) / len(rs)
        preds = defaultdict(int)
        for r in rs:
            preds[r["pred_component"]] += 1
        print(f"{fault:16} {rs[0]['true_component']:8} {len(rs):>3} {acc:>5.0%}  {ev:>5.0%}  {gr:>5.0%}  {dict(preds)}")
    n = len(rows)
    acc = sum(r["correct"] for r in rows) / n
    faults = [r for r in rows if r["fault"] != "none"]
    nones = [r for r in rows if r["fault"] == "none"]
    print(f"\noverall accuracy {acc:.0%} over {n} trials"
          + (f" | fault trials {sum(r['correct'] for r in faults)/len(faults):.0%}" if faults else "")
          + (f" | false alarms on 'none': {sum(not r['correct'] for r in nones)}/{len(nones)}" if nones else ""))
    tc = [r["n_tool_calls"] for r in rows if r.get("n_tool_calls") is not None]
    if tc:
        print(f"tool calls per incident: avg {sum(tc)/len(tc):.1f}, max {max(tc)}")
    lo, hi = wilson(sum(r["correct"] for r in rows), n)
    exact = sum(r["evidence_valid"] for r in rows) / n
    grounded = sum(r.get("evidence_grounded", False) for r in rows) / n
    print(f"95% CI {lo:.0%}-{hi:.0%} | exact quotes {exact:.0%} | grounded quotes {grounded:.0%} | "
          f"trials quoting nothing: {sum(not r.get('n_quotes') for r in rows)}/{n}")
    print(f"results: {out}")
    if a.mlflow:
        metrics = {"accuracy": acc, "ci_low": lo, "ci_high": hi, "evidence_exact": exact, "evidence_grounded": grounded,
                   "latency_mean_s": sum(r["latency_s"] for r in rows) / n}
        if faults:
            metrics["fault_accuracy"] = sum(r["correct"] for r in faults) / len(faults)
        if nones:
            metrics["false_alarm_rate"] = sum(not r["correct"] for r in nones) / len(nones)
        if tc:
            metrics["tool_calls_mean"] = sum(tc) / len(tc)
        log_mlflow(a, out, rows, metrics)
    if a.show_wrong:
        for r in rows:
            if not r["correct"]:
                print(f"\n✗ {r['trial_id']}  true={r['true_component']} pred={r['pred_component']} "
                      f"rule={r['pred'].get('rule')}\n  evidence={r['pred'].get('evidence')}")


if __name__ == "__main__":
    main()
