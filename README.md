# rca-bench

A ground-truth benchmark for **LLM-based root-cause analysis** of production incidents.

I break a real running system on purpose — 12 fault types, each with a known cause —
record everything it logs, and measure whether different "diagnosers" can name the
broken component from the logs alone.

## How it works
1. **Target system**: [RealEstate_Blockchain](https://github.com/saishyamdontha/RealEstate_Blockchain)
   (Spring Boot + PostgreSQL + Anvil/Solidity + React) running in Docker.
2. **Load** (`scripts/load.py`): simulated users browse and run full escrow sales.
3. **Fault injection** (`scripts/faults.py`): breaks exactly one thing and records the
   ground truth (component, category, root cause, timestamps).
4. **Runner** (`scripts/run_experiment.py`): reset -> load -> inject -> hold -> revert,
   repeated per fault; logs collected as structured JSONL (`scripts/collect_logs.py`).
5. **Diagnosers** (`rca/`): see only the incident's logs, never the answer key.
   - `rules` — hand-written regex baseline
   - `llm` — single LLM call over error lines (local Ollama)
   - `agent` — same input as `llm`, plus read-only tools over system-state snapshots recorded during the trial
6. **Evaluator** (`rca/evaluate.py`): component accuracy, false alarms on no-fault
   trials, and whether quoted evidence actually exists in the logs (hallucination check).

## Status
Second trial set (36 trials, 12 fault types x 3), qwen3:8b with reasoning on:

| Diagnoser | Correct | 95% CI | False alarms |
|---|---|---|---|
| Tool-using agent | 28/36 (78%) | 62-88% | 0/3 |
| Regex rules | 25/36 (69%) | 53-82% | 0/3 |
| Single LLM call | 22/36 (61%) | 45-75% | 0/3 |

**These numbers do not separate the three diagnosers**: the sample is small and the agent
also had a larger context window. Known problems and the planned fixes are in
`CHANGELOG-eval.md`; a larger, frozen `trials_v3` run is next.

## Run
```bash
# app checkout expected at ../RealEstate_Blockchain (or set TARGET_DIR)
python3 scripts/faults.py list
python3 scripts/run_experiment.py --faults all --repeats 3
python3 -m rca.evaluate --diagnoser rules
RCA_MODEL=qwen3:8b RCA_THINK=1 python3 -m rca.evaluate --diagnoser llm
```

## History
Developed inside RealEstate_Blockchain before being split out; earlier history is in
commits `db2272f`..`7aa04d4` of that repo (fault injection, runner, diagnosers, dev-set changes).
