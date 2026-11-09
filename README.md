# rca-bench

A small, honest benchmark for **root-cause diagnosis of recorded incidents** in a full-stack application. It injects known faults into a running stack, records what the system looked like, and measures how well different diagnosers (regex rules, a single LLM call, a tool-using LLM agent) name the failing component.

The scope is **offline diagnosis of recorded incidents**, not live monitoring. Everything is run on a local machine with a local model (Ollama), and every number below comes from a frozen, tagged evaluation.

## Why this exists

"Let an LLM read the logs" is easy to demo and hard to trust. This repo asks a narrower question with a controlled answer:

> Given a fault we injected ourselves (so the true cause is known), which approach finds it, how often, how expensively, and how often does it blame something when nothing is wrong?

## The system under test

A blockchain real-estate application running locally:

| Component | Role |
|---|---|
| Spring Boot backend | API and business logic |
| PostgreSQL | persistence |
| Anvil (local EVM node) | blockchain |
| Solidity contract | on-chain property records |
| Wallet | account that signs and pays for transactions |

## Fault model

11 injected fault types plus a fault-free control (`none`), each repeated 5 times in the final set.

`backend_crash`, `chain_pause`, `chain_reset`, `chain_stop`, `db_bad_password`, `db_lock`, `db_pause`, `db_stop`, `escrow_desync`, `wallet_drained`, `wrong_contract`, and `none`.

Each diagnoser must answer with one of five labels: `db`, `chain`, `wallet`, `backend`, `none`.

The `none` runs matter: they measure **false alarms** (blaming a component when nothing is broken).

## What a recorded incident contains

Each trial directory holds:

- `logs.jsonl` : structured service logs during the incident
- `snapshot_before.json` / `snapshot_during.json` : container, database, chain, wallet and endpoint state
- `truth.json` : the injected fault (never shown to a diagnoser)

## Diagnosers

1. **Rules**: deterministic regex/pattern rules over the logs. No model.
2. **Single LLM call**: one prompt containing a digest of the incident (duration, silent and restarted services, de-duplicated ERROR/WARN lines, capped at 40). No tools. The model must return JSON with a label and an evidence quote copied exactly from the logs.
3. **Tool-using agent**: the same system prompt plus investigation instructions, with up to 6 tool calls. The model chooses which read-only tools to call, then answers.

Agent tools (all read-only, operating on the recorded snapshots): `get_containers`, `check_database`, `check_chain`, `check_wallet`, `check_backend_config`, `probe_endpoints`, `grep_logs`.

Model settings: qwen3:8b via Ollama, thinking on, temperature 0, context 16384 tokens. This is **one agent with a toolbox**, not a multi-agent system.

## Metrics

- Accuracy over all valid trials, with a Wilson 95% confidence interval
- False alarms on `none` trials
- Evidence quality: *exact* (quote found verbatim in the logs) versus *grounded*
- Average tool calls per trial (agent)

## Results (frozen, tag `v3-frozen`)

66 valid trials: 54 fault trials and 12 fault-free trials.

| Diagnoser | Correct | Accuracy | 95% CI (Wilson) | False alarms (none) | Notes |
|---|---|---|---|---|---|
| Rules | 55/66 | 83% | 73–90 | 0/12 | deterministic |
| Tool-using agent | 53/66 | 80% | 69–88 | 3/12 | 0.9 tool calls on average |
| Single LLM call | 43/66 | 65% | 53–76 | 6/12 | quoted log lines that do not exist on 6 fault-free runs |

**Reading these numbers**

- The confidence intervals overlap between rules and agent, so **no winner is claimed** between them. The LLM-only result is clearly lower.
- **Every diagnoser fails `db_lock`.** The logs are silent during a lock. The snapshots do show sessions waiting on a lock, but the diagnosers did not use that signal.
- On the fault-free runs, the single LLM call fabricated evidence quotes (strings verified absent from those trials by grep). Cause unknown.
- One agent trial hit a 600 s timeout.

## Limitations (read before citing any number)

- One application, one machine, one small local model. No claim of generality.
- Sample size is small; intervals are wide.
- The rules and prompts were developed on an earlier 36-incident set. The 66-incident set was collected **after the freeze**, so v3 numbers are not tuned on.
- An LLM-as-judge was tried and abandoned: its scores contradicted the programmatic evaluator.
- Faults are injected by me, so they reflect my own idea of what breaks.
- Offline, single-incident diagnosis only. Nothing here shows the approach works on a live stack.

## Reproducing

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
# rules need no model:
.venv/bin/python -m rca.evaluate --diagnoser rules --trials trials_ci
# llm / agent need Ollama with qwen3:8b running locally
```

`trials_ci/` holds 12 frozen incidents (one per fault type plus a fault-free one) used by CI. The full 66-trial set is not committed.

## Method discipline

- Evaluation is frozen by git tag (`v3-frozen`). Results are only added to `CHANGELOG-eval.md`, never edited in place.
- Failures are reported next to successes.
- Changes after a freeze are logged as new versions and tested on fresh incidents.

## Where this fits in the software lifecycle

| Phase | Role in this repo |
|---|---|
| Testing | Faults are injected into a known stack with known ground truth |
| CI | A rules-diagnoser regression gate runs on `trials_ci` for every push |
| Operations | The diagnosers are candidate tools for incident triage |
| Maintenance | A misdiagnosed incident becomes a new regression case |

## Roadmap (one change at a time, each pre-registered and tested on fresh incidents)

1. Expose DB or Actuator metrics so `db_lock` becomes detectable
2. Cost experiment: rules first with LLM fallback, and thinking off
3. Optional: retrieval of similar past incidents, against a nearest-neighbour baseline
4. Optional: a second, public dataset as a test-only check

## Related

`llm-api-sectest`: an LLM agent that plans API security checks against the same application, with an oracle baseline.
