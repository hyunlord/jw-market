# Agent2 View Label Scope Fix Commit Report

## Summary

`market_metric_missing_view_label`가 bare year(`2025`)와 CI literal(`95%`)을 market metric으로 오인하던 문제를 수정했다. view-label 요구 대상은 실제 market metric field로 한정했고, 진짜 metric의 label 누락은 계속 차단한다.

운영 cache/swap 및 `cache_deep_analysis_ai_analysis` 접근은 수행하지 않았다.

## Changes

- `crawl/agent2/phase_zeta_runner/metric_validator.py`
  - bare year(`19xx`/`20xx`)는 view-label 대상에서 제외.
  - CI confidence literal(`95%`, `95% 신뢰구간`)은 view-label 대상에서 제외.
  - label 요구 대상을 market metric path로 제한:
    - `raw_value`, `value`, growth/qoq/yoy/ms/rank 계열
    - `market_size.history`
    - KPI extras and forecast `horizon_*y.base`
  - period metadata and CI bounds (`latest_period`, `period`, `ci_lower_95`, `ci_upper_95`)는 제외.
- `crawl/agent2/tests/test_metric_validator.py`
  - bare year가 period metadata와 매칭되어도 view-label fail을 만들지 않는 테스트 추가.
  - CI confidence literal이 view-label fail을 만들지 않는 테스트 추가.
  - 기존 true metric missing-label fail 테스트와 compact tag pass 테스트 유지.

기존 누적 Agent2 수정도 같은 commit scope에 포함된다:

- `prompt_builder.py`: full bundle JSON 동봉, body 6~9, 1/3/5년 horizon, CI-tag 지시.
- `agent2_regen_orchestrator.py`: formatter contract guard.
- 관련 prompt builder/orchestrator/validator tests.

## Bidirectional Test Gate

Command:

```bash
PYTHONPATH=crawl/agent2 pytest -q crawl/agent2/tests/test_prompt_builder.py crawl/agent2/tests/test_metric_validator.py crawl/agent2/tests/test_agent2_regen_orchestrator.py
```

Result:

```text
26 passed in 0.07s
```

Bidirectional verdict:

- PASS: bare year `2025` no longer emits `market_metric_missing_view_label`.
- PASS: CI literal `95%` / `95% 신뢰구간` no longer emits `market_metric_missing_view_label`.
- PASS: true market metric missing view label still fails.
- PASS: compact production tag such as `ML·UBIST·매출·기간` still passes.

Py compile:

```bash
python3 -m py_compile crawl/agent2/phase_zeta_runner/prompt_builder.py crawl/agent2/phase_zeta_runner/metric_validator.py crawl/agent2/agent2_regen_orchestrator.py
```

Result: PASS.

## Hemlibra Smoke Gate

Workflow:

- wf217 direct `/run/v2`
- question builder payload with full bundle JSON
- rev/deploy: `4990` / `1063`
- brand: 헴리브라

Smoke result:

| Gate | Result |
| --- | --- |
| HTTP | 200 |
| `post_format=ok` | PASS |
| schema | PASS |
| body sentence range | PASS: phenomenon 6, cause 6, prediction 8, recommendation 8 |
| period tag | PASS: 34 period tags |
| malformed 3-part tag | PASS: 0 |
| horizon | PASS: 1y/3y/5y all present |
| CI standalone without tag | PASS: 0 |
| prediction evidence | PASS |
| forbidden `news_id`/star/3+ decimals/certainty phrase | PASS: all 0 |
| validator | PASS: `valid=true`, `unmatched_count=0`, `warnings_count=0` |

Raw smoke SHA256:

```text
8d81e65796b52041384d1c7d65b42b46b0fe8b809610dc3f6c4b00ed0961ddf1
```

## Git / Safety

- Branch: `crawl/relocation-20260628`
- Remote base before commit: `b61f5bf38c04ba1c3388c0bed8b60c7275378edc`
- Main before commit: `99a308b4c42c823870ea52868c0c8f9e1f1facb5`
- Force push: not used.
- Operating cache/swap: not touched.
- `cache_deep_analysis_ai_analysis`: not accessed.
- Secret scan on commit target: `NO_MATCH`.

Final commit SHA and remote SHA are recorded in the audit sidecar and final handoff.
