#!/usr/bin/env bash
# Frozen v3 run: collect once, evaluate once, one job at a time so latencies are comparable.
cd ~/rca-bench || exit 1
python3 scripts/run_experiment.py --faults all --repeats 5 --seed 1000 --out trials_v3
python3 scripts/run_experiment.py --faults none --repeats 7 --seed 2000 --out trials_v3
export RCA_MODEL=qwen3:8b RCA_THINK=1 RCA_TIMEOUT=600
for d in rules llm agent; do
  echo "=== $d ==="
  .venv/bin/python -m rca.evaluate --diagnoser "$d" --trials trials_v3 --mlflow --show-wrong
done
for d in llm agent; do
  echo "=== judge $d ==="
  .venv/bin/python -m rca.judge "$(ls -t results/$d-*.jsonl | head -1)"
done
echo "=== all done: $(date) ==="
