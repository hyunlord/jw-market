# Agent2 25 Brand Fix + Swap Report

## Verdict
PASS. 25 brand regeneration completed with 25/25 validated, code was committed/pushed, and production `jw_mart.cache_deep_analysis_ai_analysis` was blue-green swapped successfully.

## Stage 0 - 4 Offending Analysis
The previous 25-brand run failed 4 brands (`가드메트`, `뉴트로진`, `시그마트`, `타발리스`). Saved diagnostics showed formatter/validator format defects rather than bundle or corpus absence:

| Brand | Stored offending class | Classification | Fix path |
| --- | --- | --- | --- |
| 가드메트 | `dual_source_missing` on dual-source brand while validation passed | format/coverage contract | prompt requires both UBIST and IQVIA when both are present |
| 뉴트로진 | Korean large-unit values and decimal unit formatting | format repair | deterministic Korean-unit normalization and decimal unit cleanup |
| 시그마트 | Korean large-unit values in currency/CI text | format repair | deterministic Korean-unit normalization |
| 타발리스 | forecast tags/periods and short prediction body | format/prompt | forecast compact-tag repair and prediction body guardrail |

Follow-up full run exposed one additional 25-brand blind spot, `가드렛`, with `dual_source_missing(has_iqvia=false, has_ubist=true)`. Its bundle contained both UBIST and IQVIA views, so prompt coverage was strengthened rather than weakening the formatter gate.

## Stage 1 - Fix Summary
Changed files:

- `crawl/agent2/phase_zeta_runner/post_llm_repair.py`
- `crawl/agent2/phase_zeta_runner/metric_validator.py`
- `crawl/agent2/phase_zeta_runner/prompt_builder.py`
- `crawl/agent2/tests/test_post_llm_repair.py`
- `crawl/agent2/tests/test_prompt_builder_guardrails.py`

The repair layer remains format-only: it normalizes Korean large-unit numerals, removes decimal `원/개` suffixes only when bundle-backed, repairs forecast compact tags from bundle forecast paths, and preserves quoted news titles plus class names such as `DPP-4 억제제`/`SGLT-2 억제제`.

## Stage 2 - 25 Brand Gate
Final run: `/tmp/agent2_25brand_fix_swap_20260629_145002/regen25_full_after_dual_source_prompt_fix`

- Manifest: `/tmp/agent2_25brand_fix_swap_20260629_145002/regen25_full_after_dual_source_prompt_fix/run_manifest.json`
- Total: 25
- Validated: 25
- Failed: 0 `[]`
- Local staging rows: `zeta_analysis_runs=25`, `zeta_analysis_outputs=100`
- Payload SHA256: `3861f63b1a30c334ed8565b9e9da43dba14169f6cb06cfa2431b96665684c334`

## Tests
`python3 -m pytest crawl/agent2/tests/test_post_llm_repair.py crawl/agent2/tests/test_metric_validator.py crawl/agent2/tests/test_prompt_builder_guardrails.py crawl/agent2/tests/test_agent2_regen_orchestrator.py`

Result: 35 passed.

## Git
Code commit: `2c028be05f47e8bba07df1c2bb4217e1f324740a`
Remote `jw-private/crawl/relocation-20260628`: `2c028be05f47e8bba07df1c2bb4217e1f324740a`
Remote match: `true`

```text
2c028be Stabilize Agent2 narrative gates for all brands
 crawl/agent2/phase_zeta_runner/metric_validator.py |   6 +
 crawl/agent2/phase_zeta_runner/post_llm_repair.py  | 195 +++++++++++++++++++++
 crawl/agent2/phase_zeta_runner/prompt_builder.py   |   4 +-
 crawl/agent2/tests/test_post_llm_repair.py         | 181 +++++++++++++++++++
 .../agent2/tests/test_prompt_builder_guardrails.py |   5 +-
 5 files changed, 389 insertions(+), 2 deletions(-)
```

## Stage 4 - Production Swap
Production write scope: `jw_mart.cache_deep_analysis_ai_analysis` only.

- App DB identity: `llmops@%`
- Existing live rows before swap: `25`
- New table: `cache_deep_analysis_ai_analysis_new_20260629_153453`
- Backup table retained: `cache_deep_analysis_ai_analysis_prod_bak_20260629_153453`
- Failed table name reserved: `cache_deep_analysis_ai_analysis_failed_20260629_153453`
- Swap steps: `created_new_table, inserted_new_rows, atomic_rename_swap`
- Live rows after swap: `25`
- Backup rows: `25`
- `cache_deep_analysis` row count before/after: `3449` / `3449`
- Existing `cache_deep_analysis` events/graph table: untouched.

Off-DB backup:

```text
ea8939c5a4a91a32ceb772ee074390df0791a5c9eb0d0ae7841a369383457426  /tmp/agent2_25brand_fix_swap_20260629_145002/prod_swap/cache_deep_analysis_ai_analysis_prod_bak_20260629_153453.sql
```

Rollback SQL:

```sql
RENAME TABLE cache_deep_analysis_ai_analysis TO cache_deep_analysis_ai_analysis_failed_20260629_153453, cache_deep_analysis_ai_analysis_prod_bak_20260629_153453 TO cache_deep_analysis_ai_analysis;
```

## Post-Swap Read Verification
DB/API sample brands: 제이클, 리바로젯, 악템라, 헴리브라.

- Live row count: `25`
- Backup row count: `25`
- `cache_deep_analysis` row count: `3449`
- API samples returned HTTP 200 with all 4 stages: `True`

API sample summary:

```json
[
  {
    "brand": "제이클",
    "status": 200,
    "has_ai": true,
    "stages": [
      "phenomenon",
      "cause",
      "prediction",
      "recommendation"
    ],
    "phenomenon_title": "제이클의 시장 진입 및 초기 점유율 확보 현황"
  },
  {
    "brand": "리바로젯",
    "status": 200,
    "has_ai": true,
    "stages": [
      "phenomenon",
      "cause",
      "prediction",
      "recommendation"
    ],
    "phenomenon_title": "리바로젯의 시장 내 위치와 성장 흐름"
  },
  {
    "brand": "악템라",
    "status": 200,
    "has_ai": true,
    "stages": [
      "phenomenon",
      "cause",
      "prediction",
      "recommendation"
    ],
    "phenomenon_title": "악템라의 시장 내 위치와 최근 매출 흐름"
  },
  {
    "brand": "헴리브라",
    "status": 200,
    "has_ai": true,
    "stages": [
      "phenomenon",
      "cause",
      "prediction",
      "recommendation"
    ],
    "phenomenon_title": "헴리브라의 시장 지배력 확대와 성장세"
  }
]
```

## Safety Confirmation
- Validator/formatter gates were not weakened.
- Repair did not synthesize content evidence.
- Production mart tables were not reloaded.
- `cache_deep_analysis` was not modified.
- Backup table was not deleted.
- No rollback was required.
- Secret scan: NO_MATCH, except non-secret variable names such as `TOKEN_RE` in regex code.
