# Agent2 Prediction Insight Fix Report

## Verdict

- **25/25 PASS** on wf217 rev **5032** dry regeneration with local staging only.
- Prediction insight was restored: current splitter score **3.72 insight sentences/brand** vs old production artifact **3.40**, and vs previous numeric-list dry output **2.32**.
- Numeric-listing gate works both ways: old production artifact **0 fail**, previous numeric-list dry output **11 fail**, rev5032 restored output **0 fail**.
- Evidence parity is preserved: rev5032 restored output has **12 evidence_pool items for all 25 brands**.
- **No production swap was performed.** This run only updated prompt configuration, regenerated local staging output, and produced PL review HTML.

## Prompt Change

`crawl/agent2/phase_zeta_runner/prompt_builder.py:81` was changed from numeric-only prediction guidance to:

- keep 1/3/5 year forecast_simulation values and 95% CI,
- add directionality, CI-width/uncertainty, market/prescription implications,
- keep prediction as future-outlook interpretation, not cause/recommendation,
- prohibit event-like assumptions unless the bundle includes a source event,
- require at least 3 interpretation sentences separate from raw number/CI listing.

Remote prompt sync:

| Item | Value |
| --- | --- |
| Previous rev/deploy | 5029 / 1081 |
| New rev/deploy | **5032 / 1087** |
| Flowise flow id | `1772a47e-ba6f-49da-a6ea-676ba96bb10a` |
| New prompt sha | `ab8c1d7d3113796d0ad813bdf9171267ad4953d7cd03823bef92b2e4d91ce8ea` |
| GenOS/Flowise normalized sha | `1af6e1bc5f0a41c33ab5ebdffee70ae0f63fb961ea92fe38151c2d1211e43036` |
| Remote backup | `/tmp/wf217_prediction_insight_min3_prompt_patch_remote_20260629_084540` |

## Validator Gate

`crawl/agent2/phase_zeta_runner/metric_validator.py` now counts prediction-stage insight sentences and numeric-list sentences. It fails prediction output with `prediction_insight_too_sparse` when it is mostly forecast/CI listing without enough interpretation.

The gate is intentionally conservative:

- old production artifact passes,
- prior numeric-list dry output fails 11 brands,
- rev5032 regenerated output passes all 25 brands.

Additional guardrail support kept the existing evidence policy intact:

- compact tagged forecast evidence can match bundle forecast/market values,
- event evidence title wrappers such as `뉴스 '...'` are canonicalized before matching,
- unsupported event/evidence creation is still blocked.

## Repair Layer

`crawl/agent2/phase_zeta_runner/post_llm_repair.py` remains format-only:

- repairs missing negative sign on compact-tagged percent only when the bundle has the corresponding signed trend metric,
- adds prediction numeric evidence only from forecast/market compact tags already present in the bundle,
- does not fabricate news/event evidence or alter forecast values.

## 25 Brand Dry Regeneration

Local staging was reset after backup, then 25 brands were regenerated with wf217 rev 5032. The first full run produced 24/25; `악템라` had a one-off raw response parse failure, then passed on a single-brand retry. Final local staging state:

| Metric | Value |
| --- | ---: |
| `zeta_analysis_runs` status `ok` | 25 |
| distinct brands | 25 |
| `zeta_analysis_outputs` rows | 100 |
| distinct run ids | 25 |
| validated outputs | 100 |

Dry assembly:

- audit dir: `/tmp/agent2_prediction_insight_fix_20260629_172710/dry_assembly_rev5032_final`
- zip: `/tmp/agent2_prediction_insight_fix_20260629_172710/dry_assembly_rev5032_final.zip`
- result: **25 brands with complete 4 stages**, no insert/apply.

## Prediction Insight Metrics

Source artifacts:

- old production comparison artifact: `/tmp/agent2_narrative_compare_20260629_162000/old_payloads_from_prod_select.json`
- previous numeric-list dry artifact: `/tmp/agent2_narrative_compare_20260629_162000/new_payloads_dry_assembly.json`
- restored rev5032 payloads: `/tmp/agent2_prediction_insight_fix_20260629_172710/prediction_insight_compare_rev5032/new_payloads_rev5032_final.json`

| Set | Brands | Avg insight sentences | Avg numeric-list sentences | Avg numeric tokens | Avg prediction chars | Avg evidence_pool | Gate fails |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| old_prod | 25 | 3.40 | 2.88 | 19.84 | 474.20 | 5.44 | 0 |
| numeric_dry_before_fix | 25 | 2.32 | 4.56 | 47.08 | 754.80 | 12.00 | 11 |
| rev5032_insight_fix | 25 | **3.72** | 2.96 | 30.52 | 637.36 | **12.00** | **0** |

The earlier evidence parity audit used a different evidence-counting basis. For this HTML comparison, the old artifact's top-level `evidence_pool` is reported directly; rev5032 keeps 12 top-level evidence items for every brand.

## PL HTML

Static side-by-side HTML was generated:

`/tmp/agent2_prediction_insight_fix_20260629_172710/prediction_insight_compare_rev5032/JW_Agent2_Prediction_Insight_Compare_20260629.html`

Contents:

- 25 brands,
- old vs new prediction stage side by side,
- insight sentence badges,
- evidence_pool count badges,
- stage evidence details,
- no external dependencies.

## Verification

Tests:

```text
PYTHONPATH=crawl/agent2 pytest -q \
  crawl/agent2/tests/test_metric_validator.py \
  crawl/agent2/tests/test_prompt_builder_guardrails.py \
  crawl/agent2/tests/test_prompt_builder.py \
  crawl/agent2/tests/test_post_llm_repair.py \
  crawl/agent2/tests/test_stage3a7_evidence_pool.py

40 passed in 0.05s
```

Operational safety:

- production `cache_deep_analysis_ai_analysis` was not swapped,
- production `cache_deep_analysis` events table was not touched,
- no production mart load was run,
- local staging/dry assembly only.

## Next Gate

PL should review the HTML comparison. If quality is accepted, the production swap should be requested separately using the already verified staging output and the established blue-green rollback procedure.
