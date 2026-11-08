#!/usr/bin/env python3
"""LLM judge for a results file: is the root cause right, and does the evidence support it?

Accuracy only checks the component name. The judge reads the root-cause text and the quotes.
It never sees which diagnoser produced them, and should be a different model family (default
llama3.2, diagnosers use qwen3). Its verdicts are only usable after checking them against a person:

  python3 -m rca.judge results/agent-X.jsonl                      # writes results/agent-X.judged.csv
  # fill the empty 'human' column (match/partial/wrong) on a sample of rows, then:
  python3 -m rca.judge results/agent-X.jsonl --agreement results/agent-X.judged.csv
"""
import argparse, csv, json, os, sys, urllib.request

MODEL = os.environ.get("RCA_JUDGE_MODEL", "llama3.2")
HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
TIMEOUT = int(os.environ.get("RCA_TIMEOUT", "600"))
VERDICTS = ["match", "partial", "wrong"]
SCHEMA = {"type": "object",
          "properties": {"root_cause_verdict": {"type": "string", "enum": VERDICTS},
                         "evidence_supports": {"type": "boolean"},
                         "reason": {"type": "string"}},
          "required": ["root_cause_verdict", "evidence_supports", "reason"]}
SYSTEM = """You grade an incident diagnosis against the known true cause.
root_cause_verdict: match = same underlying cause; partial = right area but misses the cause or names only a symptom;
wrong = different cause.
evidence_supports: true only if the quoted evidence, taken together, supports the DIAGNOSED cause. Real but unrelated lines are false.
Answer with JSON only."""


def build_prompt(row):
    quotes = "\n".join(f"- {q}" for q in row["pred"].get("evidence", [])) or "(none)"
    return f"True cause: {row['true_root_cause']}\n\nDiagnosed cause: {row['pred'].get('root_cause', '')}\n\nQuoted evidence:\n{quotes}"


def ask(row):
    payload = {"model": MODEL, "stream": False, "format": SCHEMA, "options": {"temperature": 0},
               "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": build_prompt(row)}]}
    req = urllib.request.Request(f"{HOST}/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        out = json.loads(json.load(r)["message"]["content"])
    return out["root_cause_verdict"], bool(out["evidence_supports"]), out.get("reason", "")[:300]


def judge(path):
    with open(path) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    rows = [r for r in rows if r["fault"] != "none"]            # no root cause to grade
    out_path = path.rsplit(".", 1)[0] + ".judged.csv"
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trial_id", "fault", "correct_component", "judge", "evidence_supports", "reason",
                    "true_root_cause", "pred_root_cause", "human"])
        counts, support = {v: 0 for v in VERDICTS}, 0
        for i, r in enumerate(rows, 1):
            verdict, sup, why = ask(r)
            counts[verdict] += 1; support += sup
            w.writerow([r["trial_id"], r["fault"], r["correct"], verdict, sup, why,
                        r["true_root_cause"], r["pred"].get("root_cause", ""), ""])
            f.flush()
            print(f"[{i}/{len(rows)}] {r['fault']:16} {verdict:8} support={sup}", file=sys.stderr, flush=True)
    n = len(rows)
    print(f"root cause: " + ", ".join(f"{v} {counts[v]}/{n}" for v in VERDICTS) + f" | evidence supports diagnosis: {support}/{n}")
    print(f"wrote {out_path}. Check a sample by hand before quoting these numbers.")


def agreement(csv_path):
    with open(csv_path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["human"].strip()]
    if not rows:
        sys.exit("no rows have a value in the 'human' column yet")
    bad = sorted({r["human"].strip() for r in rows} - set(VERDICTS))
    if bad:
        sys.exit(f"'human' must be one of {VERDICTS}, found {bad}")
    agree = sum(r["judge"] == r["human"].strip() for r in rows)
    print(f"judge vs human: {agree}/{len(rows)} agree ({agree/len(rows):.0%})")
    return agree, len(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--agreement", help="judged CSV with the 'human' column filled in")
    a = ap.parse_args()
    agreement(a.agreement) if a.agreement else judge(a.results)
