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
   - agent with tools — *in progress*
6. **Evaluator** (`rca/evaluate.py`): component accuracy, false alarms on no-fault
   trials, and whether quoted evidence actually exists in the logs (hallucination check).

## Status
Dev set (36 trials) evaluated; see `CHANGELOG-eval.md` for every change made after
looking at dev results. **Final numbers will come only from a fresh, uninspected test run.**

## Run
```bash
# app checkout expected at ../RealEstate_Blockchain (or set TARGET_DIR)
python3 scripts/faults.py list
python3 scripts/run_experiment.py --faults all --repeats 3
python3 -m rca.evaluate --diagnoser rules
RCA_MODEL=qwen3:8b RCA_THINK=1 python3 -m rca.evaluate --diagnoser llm
```
