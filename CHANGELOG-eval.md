# Evaluation change log

Dev set: trials/ run 20261104T173951Z (36 trials). Every change below was made
after looking at dev-set results. Final numbers come ONLY from a fresh test run
that is not inspected before evaluation.

| # | Date | Change | Why | Found on |
|---|------|--------|-----|----------|
| 1 | 2026-11-04 | llm: replaced per-service log-volume table with "services completely silent" | Volume swings with random traffic; caused a false alarm on a no-fault trial. Restores parity with rules (which only use silence) | dev trial 001-none (pre-run check) |
| 2 | 2026-11-04 | rules: rpc_unreachable also matches `UnknownHostException: chain` | Stopped container loses its DNS name; third form of "chain down" (after timeout, refused). Rules scored 0% on chain_stop | chain_stop x3 |
| 3 | 2026-11-04 | incident: `restarted()` signal from startup markers; rules + llm both get it | wrong_contract and chain_reset have identical error lines; only the restarted service differs ("what changed?") | wrong_contract / chain_reset |
| 4 | 2026-11-04 | rules: chain_restarted_state_lost, backend_restarted_config_change | Use change #3 | same |

## Known metric limitations
- evidence_valid checks a quote EXISTS in the window, not that it is RELEVANT
  (llama3.2 quoted real but unrelated Hibernate startup warnings). Relevance -> LLM judge (Phase 5).
- n=3 per fault at temperature 0: repeats are near-identical; effective sample ~12 fault types.

## Dev results log
| version | rules | qwen3:8b (no think) | llama3.2 | note |
|---|---|---|---|---|
| v1 | 67% | 58% | 81% (3/3 false alarms) | before changes 2-4 |
| v2 | 83% | 50% | 75% (3/3 false alarms) | after 2-4. One added prompt line moved qwen3 -8pts (backend_crash -> wallet 3/3): LLM prompt fragility |

Rules improved using the dev set; LLMs were not tuned -> dev comparison is NOT fair. Final claims only from a fresh test run.
| v2 + think | 83% | 69% (RCA_THINK=1) | 75% | qwen3 reasoning mode on: +19pts vs no-think; fixes symptom-vs-cause (db_bad_password 0->100%), 0 false alarms; ~35s/trial vs ~2s. Evidence validity NOT improved (paraphrases instead of quoting). Chosen as agent model. |

## v2 evaluation (2026-11-08)
Trial set: trials_v2, run 20261105T090413Z (36 trials: 12 faults x 3). qwen3:8b, think on, temperature 0.

| diagnoser | correct | 95% Wilson CI | false alarms on none | exact-quote evidence |
|---|---|---|---|---|
| rules | 25/36 (69%) | 53-82% | 0/3 | see results file |
| llm | 22/36 (61%) | 45-75% | 0/3 | 18/36 |
| agent | 28/36 (78%) | 62-88% | 0/3 | 10/36 |

Agent averaged 0.9 tool calls per incident (max 4). Rules fell from 83% on the dev set to 69% here.

Limitations found after reading these results (trials_v2 is now inspected; no tuning on it):
- The sample cannot separate the diagnosers: CIs overlap; agent vs llm per fault type is 4 better, 1 worse, 7 tied (sign test p=0.38).
- Agent differed from llm in more than tools: num_ctx 16384 vs 8192, and a prompt hint to look beyond the symptoms.
- Silence signal: inject_ts is taken before the kill, so one log line in that gap hides a killed backend ("silent now: none").
- evidence_valid is too strict to read as a hallucination rate: models quote the rendered prompt, elide with "...", and one bad quote fails the trial.
- llm and agent ran concurrently on one GPU, so latencies are not comparable.

## v3 evaluation (2026-11-08, code frozen at tag v3-frozen)
| diagnoser | correct | acc | 95% CI | false alarms (none) |
|---|---|---|---|---|
| rules | 55/66 | 83% | 73-90 | 0/12 |
| agent | 53/66 | 80% | 69-88 | 3/12 |
| llm   | 43/66 | 65% | 53-76 | 6/12 |

Limitations: intervals overlap, no winner claimed. All three fail db_lock (silent logs).
Agent used 0.9 tool calls per incident on average. One agent trial timed out (600 s).
LLM quoted log lines absent from the data on 6/12 fault-free runs (grep found none).
LLM judge disagrees with the evaluator on correct diagnoses; judge numbers not used.
Code was developed on an earlier 36-incident set; this 66-incident set was collected after freezing.
