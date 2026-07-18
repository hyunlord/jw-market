# ingest_hook — 증분 적재 훅 (webhook → G3 → incremental Job)

jw-data-input 사이트의 "제출 확정" webhook 을 받아 구조검증(G3) 후 incremental
적재를 k8s Job 으로 실행한다. 근거 계약: JW_Input_Detection_Contract_v2
(필드 정본은 `contract.py` — 계약 문서와 대사 시 이 파일만 수정).

## 경계 (STOP 조건의 코드화)

* **서빙 무접촉** — jw-market-backend-api 에는 어떤 엔드포인트도 추가하지 않는다.
  트리거 서비스는 별도 Deployment (같은 orchestrator 이미지, uvicorn factory).
* **G3 우회 불가** — `job_runner.py` 가 G3 → exact 적재 → refresh → Σ게이트 순서를
  코드로 강제한다. G3 실패 = 적재 0, refresh 실패 = Σ/complete 0.
* **미활성 배포** — `deploy/k8s/ingest-hook/` 의 리소스는 replicas 0 / suspend
  상태로만 repo 에 존재한다. 활성화(적용·스케일업·resume + mysql `ingest_ledger`
  DDL 적용)는 사이트 confirm 구현 + 격리 검증 완료 후 PL 게이트.

## 구성

| 모듈 | 역할 |
|---|---|
| `contract.py` | manifest v2 파싱·검증 (계약 코드 정본) |
| `category_map.py` | 카테고리 → G3 기대 스키마 / 적재 argv / refresh argv (fail-closed) |
| `g3.py` | 파일 존재·sha256 / 스키마 / 기간 정합 / 행수 sanity / dedup 위임 명기 |
| `ledger.py` | ingest_ledger (mysql/sqlite 이중 dialect) — 멱등 락 + 상태 정본 |
| `app.py` | 트리거 서비스 (POST /ingest/webhook, GET /ingest/status, POST /ingest/reconcile) |
| `job_launcher.py` | batch/v1 Job 렌더+제출 (SA 토큰, transport 주입 가능) |
| `job_runner.py` | Job 내부 실행 순서 강제 (rehearsal 모드 = 격리 검증) |
| `sigma_gate.py` | Σ부분=전체 게이트 (staging 대상) |
| `sweep.py` | 유실 감시 CronJob 본체 (정상 시 no-op) |

## 멱등·직렬화

식별자 = `(epoch, category, manifest_sha)`. 동일 webhook 재수신은
queued/running/complete 인 동안 no-op; failed 만 재큐. 같은 category 는 Job 1개
직렬(FIFO), 타 category 병렬. 상태 회신은 `GET /ingest/status` (D-4).

## 환경 변수

| 변수 | 의미 |
|---|---|
| `INGEST_INPUT_ROOT` | 제출 파일·manifest 루트 (필수, 기본값 없음) |
| `INGEST_LEDGER_SQLITE` | 설정 시 sqlite ledger (리허설/테스트) — 미설정 시 mart DB(MARIADB_*) |
| `INGEST_JOB_IMAGE` | Job 이미지 (기본 = 운영 orchestrator digest pin) |
| `INGEST_JOB_NAMESPACE` | 기본 `llmops` |
| `INGEST_REHEARSAL_ROOT` | 설정 시 job_runner 격리 모드 (sqlite staging, orchestrator 미호출) |
| `INGEST_UBIST_TARGET_DIR` | UBIST 실증분이 append될 기존 full parquet 루트 (실 load 필수) |

## 격리 리허설 (운영 무접촉 E2E)

```bash
export INGEST_LEDGER_SQLITE=/tmp/ingest_rehearsal/ledger.db
python -m pipeline.scripts.ingest_hook.job_runner \
  --manifest /tmp/ingest_rehearsal/bucket/ubist/manifest.json \
  --input-root /tmp/ingest_rehearsal/bucket \
  --rehearsal-root /tmp/ingest_rehearsal/staging
```

게이트 증적·실행 방법: `tests/ingest_hook/` (G-1 E2E, G-2 G3 거부, G-3 멱등,
G-4 sweep). 전체: `python -m pytest tests/ingest_hook -q`.

## R-2 full-then-incremental 격리 실증

`rehearse-incremental`은 full 입력에서 제출 epoch의 canonical UBIST parquet
sidecar를 holdout한 뒤 격리 DB/cache를 full 재생성한다. 이어 별도 제출 원본
디렉터리의 XLSX를 manifest SHA256/period로 검증하고 G3와 실 UBIST incremental
loader로 append한다. 이후 canonical refresh, Σ게이트, R-1 full 산출물 exact
digest census를 순서대로 수행하며 운영 schema를 publish하지 않는다.

```bash
python -m pipeline.orchestrator rehearse-incremental \
  --full-input-manifest /work/inputs/input_manifest.json \
  --submission-manifest /config/ubist-2026-05.json \
  --submission-source-dir /work/submissions/ubist-2026-05 \
  --target-db jw_mart_rehearsal_r2_example \
  --cache-db jw_mart_s6_rehearsal_r2_example \
  --source-db jw_mart_d2_stage_20260630_r2 \
  --reference-db jw_mart_rehearsal_r1_example \
  --reference-cache-db jw_mart_s6_rehearsal_r1_example \
  --work-dir /work/r2 \
  --comparison-output /work/evidence/r2_compare.json
```

## 활성화 절차 (PL 게이트 — 이 repo 커밋만으로는 아무것도 돌지 않음)

1. 계약 정본과 `contract.py`/`category_map.py` 필드 대사 (D_design F-1)
2. mart DB 에 `ingest_ledger` DDL 적용 (ledger.py `_DDL_MYSQL`)
3. `kubectl apply -f deploy/k8s/ingest-hook/` 후 Deployment replicas 1,
   sweep CronJob `suspend: false`
4. 사이트에 webhook URL(`/ingest/webhook`)·상태 URL(`/ingest/status`) 공유
