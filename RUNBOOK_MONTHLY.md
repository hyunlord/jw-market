# RUNBOOK — JW Market 월간 파이프라인 (정본)

> 이 문서만 보고 다음 달 실행이 가능해야 한다. 코드는 repo(develop)에, 실행은 GenOS(GCP)에서, 절차는 이 문서에 있다.
> 마지막 갱신: 2026-07-17 · 기준 커밋: codex/pipeline-finalize-20260716 (STAGE 3 산출 `stage3_genos.md`에 이미지 digest 기록)

## 0. 개요 — 무엇이 어떻게 도는가

월간 체인(단일 진입점):

```
cache → forecast → strength → shortlong → events → elements
python -m pipeline.orchestrator run --mode full
```

| 단계 | 정본 빌더 | 산출 테이블 | 증분 방식 |
|---|---|---|---|
| cache | `pipeline/scripts/etl/build_cache_deep_analysis_general.py` | cache_deep_analysis_general | new_brands(--brand 스코프) |
| forecast | `pipeline/scripts/etl/ops_forecast_builder.py` | deep_forecast_block/horizon staging | mart epoch 단위(full-only below epoch — 시장 단위 로드 구조) |
| strength | `pipeline/scripts/agent3/run_source.py` | agent3_brand_strength_source | 빌더 내장 input_hash(신규/변경만 LLM) |
| shortlong | `pipeline/scripts/ai_analysis/agent2_regen_orchestrator.py` | staging(승격 별도) | 빌더 내장(스테이징 재실행 안전) |
| events | `pipeline/scripts/etl/cache_refresh/cache_deep_analysis_events_update.py` | cache_deep_analysis(events payload) | full-only(원자 스왑 단위) |
| elements | `pipeline/scripts/etl/cache_brand_elements.py` | cache_brand_elements | new_brands(--brand 스코프) |

- 오케스트레이터는 빌더를 subprocess로 호출만 한다(계산 로직·게이트는 전부 빌더 내장 유지).
- 상태(checkpoint)는 mart epoch(`ops_forecast_store.mart_source_epoch` = 6개 mart 테이블 지문) 키의 JSON 파일. 같은 epoch 재실행 = no-op(멱등).
- 관측: JSON 1줄 1이벤트 로그(stdout + `--log-file`).

## 0-a. 트리거 지도 (이벤트 드리븐 아키텍처, 2026-07-17)

> **★ 과도기 주의(PL 범위 확정)**: 정본 아키텍처는 jw market **증분 훅 시스템**(JW_Input_Detection_Contract_v2 — jw-data-input → MinIO manifest 감지 → webhook → G3 검증 → `pipeline.orchestrator --mode incremental` Job → Σ게이트). 아래 센서·kick·poll 3종은 훅 착지 시 **대체·삭제 예정**이며 **resume 금지**(suspend 유지, 과도기 수동 예비용).

| 파이프라인 | 1차 트리거(이벤트) | 안전망(시계) | 판단 주체 |
|---|---|---|---|
| 시장 데이터 체인(오케스트레이터) | **ETL 증분 적재 성공 → kick**(`pipeline/etl/kick.py`, `JW_ETL_KICK_ORCHESTRATOR=1`일 때만·성공 경로만) | `jw-pipeline-orchestrator-poll-daily` 매일 01:00 KST(같은 epoch면 수초 no-op) | 오케스트레이터 epoch/coverage 감지 |
| 브랜드 활동(topic+row-topic) | **CSD 파일 MinIO 도착 → 센서 감지 → 구조 검증 통과 → run Job**(`jw-csd-sensor` 10분 폴링) | 센서 자체가 폴링(주기 단축형 안전망) | 검증 게이트(csd_core 계약) + ingest 내장 게이트 |
| 크롤 tier1/tier2 | (시계 유지 — 의도) 매일 · tier2는 brand_key 해시 7분할 요일 로테이션 | — | 크롤 완료→스코어링 체인 내장 |

원칙: **kick·트리거는 "깨우기"만** — 실행 여부는 각 파이프라인의 감지·검증이 판단(잘못 깨워도 no-op). 적재 실패·검증 실패 시 kick/실행 없음(fail-closed). 전부 멱등(중복 kick=동일 Job명 no-op, 중복 감지=(key,etag) 마커 no-op).

**장애 시 수동 kick 절차**:
```bash
# 시장 데이터 체인 수동 깨우기(판단은 오케스트레이터가 함 — 안전)
kubectl -n llmops create job --from=cronjob/jw-pipeline-orchestrator-poll-daily orch-manual-$(date +%Y%m%d%H%M)
# 브랜드 활동 수동 실행(검증 게이트를 우회하므로 CSD 파일 구조를 먼저 육안 확인할 것)
kubectl -n llmops create job --from=cronjob/jw-brand-activity-run ba-manual-$(date +%Y%m%d%H%M)
# 센서 1회 수동 감지(dry-run: 감지·검증 결과만 출력, write 0)
python pipeline/scripts/etl/brand_activity/minio_csd_sensor.py --dry-run
```

## 1. 월간 정기 실행 (mart 갱신 후)

1) **mart 갱신 감지는 자동** — ETL 적재 성공 kick(1차) 또는 매일 폴링(안전망)이 오케스트레이터를 깨우고, epoch가 바뀌었을 때만 full 체인이 실행된다. 갱신 전이면 no-op.
2) 실행(GenOS CronJob): `jw-pipeline-orchestrator-poll-daily` (ns llmops, 기본 suspend). 수동 트리거:
   ```bash
   kubectl -n llmops create job --from=cronjob/jw-pipeline-orchestrator-poll-daily orch-manual-$(date +%Y%m%d)
   kubectl -n llmops logs -f job/orch-manual-$(date +%Y%m%d)
   ```
   또는 pod 셸에서 직접:
   ```bash
   python -m pipeline.orchestrator run --mode full --dry-run   # 계획 검토(write 0)
   python -m pipeline.orchestrator run --mode full             # 실행
   ```
3) **게이트**(빌더 내장, 실패 시 체인 자동 중단):
   - forecast: expected blocks 43,474 / horizons 3,000 · epoch 단일성 · verify-sample
   - strength: `--expected-workflow-rev` pin(불일치 즉시 abort) · coverage(source_units ≥ 35,521 · brands ≥ 24,789 · profile_only=0) · LLM idempotency(rev·hash 변경 시만 호출, 무변경 재실행 calls=0)
   - events: staging validate → backup → apply → post-verify
   - env assert: rev env 부재 시 즉사(코드에 baked 기본값 없음 — fail-closed)
4) **검증**(체인 완료 후):
   - 골든 4종 canonical 대조(기존 절차) · 행수 COUNT(*): strength 24,789 브랜드 / forecast block 43,474 / elements 26,411
   - `/api/brands` 골든 sha256 대조
5) **실패 시 재개**: 원인 수정 후
   ```bash
   python -m pipeline.orchestrator run --mode full --from-stage <실패단계>
   ```
   선행 단계는 state가 현재 epoch에서 completed일 때만 통과. state 유실 시 선행부터 재실행(멱등이라 안전).

## 2. 수시 — 증분 실행 (신규 브랜드 N개 추가 시)

```bash
python -m pipeline.orchestrator run --mode incremental --dry-run   # 감지 결과 검토
python -m pipeline.orchestrator run --mode incremental
```
- cache·elements: 커버리지 차집합으로 신규 brand_key 자동 감지 → 해당 브랜드만.
- strength·shortlong: 그대로 실행해도 빌더가 hash로 무변경 skip(신규만 LLM 과금).
- forecast·events: 브랜드 단위 증분 없음(정직 표기) — epoch 변경 시 전량, 그 외 no-op.
- 특정 브랜드 강제: `--brands 리바로,마운자로` (지원 단계만 스코프 실행, 미지원 단계는 skip 로그).

## 3. 수시 — 부분 재생성 (특정 계열 이상 시)

```bash
python -m pipeline.orchestrator run --stages strength --dry-run
python -m pipeline.orchestrator run --stages strength
```
- 선행 산출이 stale이면 **경고 후 중단**된다. 정말 우회해야 하면 `--force`(우회 사실이 로그·state에 기록됨).
- 증분 기준표 확인: `python -m pipeline.orchestrator stages`

## 4. mart 세대(DB) 교체 절차

1. `pipeline/scripts/utils/mart_config.py`의 `DEFAULT_MART_DB_NAME` 수정(유일한 Python 정의처).
2. `python -m pytest tests/deploy/test_mart_db_single_source.py` 실행 → **남은 고정 사본 전부 파일:줄로 열거됨**(manifest env·guard, standalone 스크립트 기본값). 열거된 위치를 같은 커밋에서 일괄 갱신.
3. manifest의 `test "$VAR" = "..."` guard는 의도된 fail-closed 이중기입 — 지우지 말 것.

## 5. 이미지 · 배포

- 백엔드(agent3 job 포함): `api/Dockerfile` (**보호 blob — 수정 금지**, `tests/test_crawl_shortlong_extraction_contract.py`가 sha256 pin). 신규 코드가 이미지에 필요하면 COPY 대상인 `pipeline/scripts/*` 하위에 두어야 한다(예: mart_config는 `pipeline/scripts/utils/`).
- 오케스트레이터: `deploy/docker/pipeline-orchestrator.Dockerfile` (pipeline 전체 + docs/crawl 포함).
- 크롤: `deploy/docker/crawl.Dockerfile` — 정본 계보(develop)에서 라이브 레이아웃(`crawl/…`, `/opt/tier2`)을 조립. **빌드 커밋을 stage3_genos.md에 기록**(과거 이미지 커밋 불명 재발 방지).
- 빌드는 GCP ops VM에서 `--platform linux/amd64`, push는 AR `asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/`.
- CronJob 등록분(전부 suspend=true):
  - `jw-pipeline-orchestrator-poll-daily` + state PVC (`deploy/k8s/orchestrator/`)
  - `jw-news-crawl-tier1-daily-canonical` / `jw-news-crawl-tier2-daily-slice-canonical` (`deploy/k8s/crawler/*-canonical.yaml`)
- **cutover는 PL 판단**: 기존 라이브 CronJob(agent3-refresh, 구 crawl tier1/2)의 digest 교체·삭제는 이 런북 범위 밖.

## 6. 비용표 (전량 기준)

| 계열 | 비용 | 근거 |
|---|---|---|
| forecast | ₩0 | 결정론(SARIMA 러너, LLM 없음) |
| elements | ₩0 | 캐시 조립만 |
| cache/events | ₩0 | DB 재계산 |
| strength (Agent3/wf316) | ≈₩7,300 | 전량 24,789 브랜드 기준 실측(무변경 재실행은 calls=0 → ₩0) |
| shortlong (Agent2) | **첫 실전 실측 기입란**: ______ | staging 생성 시 wf217 호출량 기록할 것 |

## 7. 금지 사항 (요약)

1. **BRANCH_POLICY 3건**: `codex/crawl-2tier`·`codex/short-long-lineage-bulk` develop 머지 금지(rev 5365 복원·게이트 제거됨) · `3f0db0ae` 계보 머지 금지(구 이벤트 컷오프) · 크롤 이미지를 역사 브랜치에서 빌드 금지.
2. **live 테이블 직접 write 금지** — 모든 write는 빌더의 staging→gate→swap 경로로만.
3. Agent3 rev는 manifest env로만(`AGENT3_WORKFLOW_REV` + `--expected-workflow-rev`) — 코드 기본값 없음(부재 시 즉사가 정상).
4. `api/Dockerfile`·보호 blob 수정 금지(계약 테스트 pin).
5. 운영 중 CronJob digest 임의 교체 금지(별도 PL 게이트).
6. `agent3_brand_strength` 스키마 reset(DROP) 러너 경로 반입 금지(ensure_table은 create-only).
7. mart DB명 신규 하드코딩 금지 — drift gate가 fail시킴(§4 절차로만).
8. 표준 검증 절차 준수: 골든 4종 · COUNT(*) · tracked-only pytest(※ 최상위 `tests/test_*.py` 2건 포함해 실행할 것 — `tests/**/test_*.py` glob은 이들을 누락함).
9. **elements 빌더 플래그 주의**: `cache_brand_elements.py`의 `--dry-run`과 `--pilot-fill`은 **독립 플래그** — 병용하면 pilot_fill이 그대로 live upsert된다(2026-07-17 실증). 검토만 하려면 `--dry-run` 단독으로.
10. **forecast epoch 스킴**: live `deep_forecast_block/horizon`의 `source_epoch`는 DB명 문자열 계열, ops 빌더 게이트는 sha256 지문 — `epoch_is_current`는 live에 대해 False가 정상(행수는 43,474/3,000 정확). 오케스트레이터 신선도는 state 파일 기준이므로 동작에는 영향 없고, ops 빌더 첫 full 실행은 staging 재빌드로 진행된다.
