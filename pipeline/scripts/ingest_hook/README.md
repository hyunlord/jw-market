# ingest_hook — 증분 적재 훅 (webhook → G3 → incremental Job)

jw-data-input 사이트의 "제출 확정" webhook 을 받아 구조검증(G3) 후 incremental
적재를 k8s Job 으로 실행한다. 근거 계약: JW_Input_Detection_Contract_v2
(필드 정본은 `contract.py` — 계약 문서와 대사 시 이 파일만 수정).

## 경계 (STOP 조건의 코드화)

* **서빙 무접촉** — jw-market-backend-api 에는 어떤 엔드포인트도 추가하지 않는다.
  트리거 서비스는 별도 Deployment (같은 orchestrator 이미지, uvicorn factory).
* **G3 우회 불가** — `job_runner.py` 가 G3 → 적재 → Σ게이트 → refresh 순서를
  코드로 강제한다. G3 실패 = 적재 0, ledger `failed`.
* **D-3a 파일럿 무장** — `deploy/k8s/ingest-hook/` 의 trigger 는 replicas 1,
  `INGEST_LOAD_STAGING_ROOT=/tmp/ingest-load-staging` 상태다. sweep 은
  `suspend: true` 이며 별도 PL 결정 전 resume 하지 않는다.
* **실적재 잠금** — `INGEST_LOAD_TARGET_ROOT` 는 manifest 에 두지 않는다.
  설정하면 serving parquet refresh 가 활성화되므로 별도 PL 게이트가 필요하다.

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
| `load_verify.py` | ★M-2 게이트: 업로드 epoch 이 로더 출력에 실제 적재됐나(조용한 실패 차단) |
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
| `INGEST_LOAD_STAGING_ROOT` | ★J5 실 로더 격리 출력 루트 (설정 시 mart refresh skip = staging-verify) |
| `INGEST_LOAD_TARGET_ROOT` | J5 프로덕션 출력 루트 (D-3; refresh 실행). staging 미설정 시 필수 |

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

## 파일럿 이후 활성화 절차 (PL 게이트)

1. 계약 정본과 `contract.py`/`category_map.py` 필드 대사 (D_design F-1)
2. mart DB 에 `ingest_ledger` DDL 적용 (ledger.py `_DDL_MYSQL`)
3. D-3a 격리 검증 완료 후에만 production output root 와 sweep resume 여부 결정
4. 사이트에 webhook URL(`/ingest/webhook`)·상태 URL(`/ingest/status`) 공유

이미지 배포 및 source-to-digest 추적 규약은
`docs/runbooks/immutable_image_references.md` 를 따른다.
