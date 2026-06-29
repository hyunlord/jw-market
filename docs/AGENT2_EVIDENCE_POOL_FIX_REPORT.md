# Agent2 Evidence Pool Fix Report

## Verdict

- Root cause: final publish assembler `stage3a7_create_and_insert_ai_analysis.py` rebuilt `ai_analysis_json` from `zeta_analysis_outputs` using only `stage/title/body/bullets`, so stage-level `raw_response.evidence` was discarded and top-level `evidence_pool` was never written.
- Secondary gap: `metric_validator.py` validated numeric/view/prediction evidence rules, but did not gate `evidence_pool` presence or richness.
- Fix: preserve stage evidence, build a top-level `evidence_pool` from model stage evidence plus bundle-backed event/metric evidence, and add validator evidence_pool parity checks.
- Swap: not performed. Production cache tables were not written.

## Baseline

Source: rollback quality compare artifact `new_vs_old_quality_compare_20260629_153453.json`.

| Dataset | Avg evidence total | Min | Max | Evidence shape |
| --- | ---: | ---: | ---: | --- |
| Existing restored production | 11.56 | 8 | 17 | Top-level `evidence_pool` present |
| Rolled-back failed new table | 0 | 0 | 0 | `evidence_pool` absent |

The failed new narratives were not short on body length; the gap was evidence shape/richness.

## Broken Point

Evidence existed before final publish:

- Latest local `zeta_analysis_outputs.raw_response` rows contained stage evidence arrays.
- Five-brand probe showed raw stage keys included `body`, `bullets`, `evidence`, `title`.
- 25-brand latest run had model-emitted stage evidence counts from 3 to 8 before deterministic supplementing.

Evidence was dropped at final publish:

- `load_parsed_output()` did not read `raw_response`.
- `build_ai_analysis()` did not include top-level `evidence_pool`.

## Changes

| File | Change |
| --- | --- |
| `crawl/agent2/phase_zeta_runner/evidence_pool.py` | New shared builder for evidence_pool; preserves stage evidence, supplements only from bundle-backed events/metrics/forecast facts. |
| `crawl/agent2/stage3a7_create_and_insert_ai_analysis.py` | Loads `input_bundle`, reads `raw_response.evidence`, writes top-level `evidence_pool`. |
| `crawl/agent2/phase_zeta_runner/metric_validator.py` | Adds `evidence_pool_missing`, `evidence_pool_too_sparse`, and incomplete-item gates. Existing production minimum 8 is the fail threshold. |
| `crawl/agent2/phase_zeta_runner/prompt_builder.py` | Adds explicit stage `evidence` instruction using only bundle facts already used in the stage. |
| `crawl/agent2/tests/test_metric_validator.py` | Adds sparse/pass evidence_pool gate coverage. |
| `crawl/agent2/tests/test_stage3a7_evidence_pool.py` | Adds final payload evidence_pool preservation coverage. |

Validator threshold:

- Minimum pass threshold: 8 complete evidence items, matching the existing production minimum.
- Publish target: 12 evidence items, aligned with existing production average 11.56.

## 25 Brand Parity

Local latest 25 completed runs were reassembled through the patched publish path without DB write.

| Metric | Value |
| --- | ---: |
| Brands checked | 25 |
| Min evidence_pool count | 12 |
| Avg evidence_pool count | 12 |
| Max evidence_pool count | 12 |
| Verdict | PASS |

Details are in `/tmp/agent2_evidence_pool_fix_20260629_155732/local_25_evidence_parity_after_fix.json`.

## Verification

- `PYTHONPATH=crawl/agent2 pytest -q crawl/agent2/tests/test_metric_validator.py crawl/agent2/tests/test_stage3a7_evidence_pool.py crawl/agent2/tests/test_prompt_builder_guardrails.py crawl/agent2/tests/test_post_llm_repair.py`
  - Result: 31 passed.
- `python3 -m py_compile crawl/agent2/phase_zeta_runner/evidence_pool.py crawl/agent2/phase_zeta_runner/metric_validator.py crawl/agent2/stage3a7_create_and_insert_ai_analysis.py`
  - Result: passed.
- `DB_ROOT_PASSWORD=<redacted> JW_MART_PASSWORD=<redacted> PYTHONPATH=crawl/agent2 pytest -q crawl/agent2/tests`
  - Result: 116 passed, 1 skipped, 3 failed.
  - Remaining failures are pre-existing `위너프A+` catalog alias tests, unrelated to evidence_pool changes.

## Safety

- Production swap/write: 0.
- `cache_deep_analysis` events table: untouched.
- `cache_deep_analysis_ai_analysis`: no write in this task.
- The deterministic evidence supplement does not invent facts; it copies source events and numeric metrics already present in the bundle.
- Secret scan target: audit/report/code artifacts only, with connection secrets redacted.

## Next Step

Run the next PL-gated 25 brand regeneration/swap only after this commit is reviewed. The new gate should block another empty evidence_pool table before swap.
