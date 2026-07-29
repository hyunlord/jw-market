# ingest_hook — 증분 적재 훅 (webhook → G3 → incremental Job)

jw-data-input 사이트의 "제출 확정" webhook 을 받아 구조검증(G3) 후 incremental
적재를 k8s Job 으로 실행한다. 근거 계약: JW_Input_Detection_Contract_v2
(필드 정본은 `contract.py` — 계약 문서와 대사 시 이 파일만 수정).

## 경계 (STOP 조건의 코드화)

* **서빙 무접촉** — jw-market-backend-api 에는 어떤 엔드포인트도 추가하지 않는다.
  트리거 서비스는 별도 Deployment (같은 orchestrator 이미지, uvicorn factory).
* **G3/POST-GATE 우회 불가** — `job_runner.py` 가 G3 → 적재 → POST-GATE →
  refresh 순서를 코드로 강제한다. G3 실패는 `failed`, POST-GATE 실패는
  `gate_failed`이며 promotion preflight가 테이블 변경 전에 거부한다.
* **D-3a 파일럿 무장** — `deploy/k8s/ingest-hook/` 의 기본 trigger 는 replicas 1,
  `INGEST_LOAD_STAGING_ROOT=/tmp/ingest-load-staging` 상태다. sweep 은
  `suspend: true` 이며 별도 PL 결정 전 resume 하지 않는다.
* **실적재 잠금** — `INGEST_LOAD_TARGET_ROOT` 는 manifest 에 두지 않는다.
  설정하면 serving parquet refresh 가 활성화되므로 별도 PL 게이트가 필요하다.
* **3모드 배타** — `INGEST_LOAD_STAGING_ROOT`, `INGEST_LOAD_SHADOW_ROOT`,
  `INGEST_LOAD_TARGET_ROOT` 중 정확히 하나만 설정한다. shadow 는 RWX의 별도
  corpus, `jw_mart_ingest_shadow_*` DB, 별도 SQLite ledger만 사용하며 serving
  cache refresh를 호출하지 않는다. production 승인 변수는 shadow에서 무시된다.

## 구성

| 모듈 | 역할 |
|---|---|
| `contract.py` | manifest v2 파싱·검증 (계약 코드 정본) |
| `category_map.py` | 카테고리 → G3 기대 스키마 / 적재 argv / refresh argv (fail-closed) |
| `g3.py` | 파일 존재·sha256 / 스키마 / 기간 정합 / 행수 sanity / dedup 위임 명기 |
| `ledger.py` | ingest_ledger (mysql/sqlite 이중 dialect) — 멱등 락 + 상태 정본 |
| `app.py` | 트리거 서비스 (webhook, queue/status 조회, terminal drain, reconcile) |
| `job_launcher.py` | batch/v1 Job 렌더+제출 (SA 토큰, transport 주입 가능) |
| `job_runner.py` | Job 내부 실행 순서 강제 (rehearsal 모드 = 격리 검증) |
| `sigma_gate.py` | Σ부분=전체 게이트 (staging 대상) |
| `post_gate.py` | Σ·row coverage·비대상 source fingerprint JSON 판정 |
| `ledger_fingerprint.py` | CronJob과 동일한 MariaDB 대상의 read-only identity fingerprint 활성화 게이트 |
| `load_verify.py` | ★M-2 게이트: 업로드 epoch 이 로더 출력에 실제 적재됐나(조용한 실패 차단) |
| `category_table_load.py` | NSA/CSD/Keyword/MI Master canonical loader를 격리 `jw_ingest_*` DB에 연결 |
| `row_count_verifier.py` | append/upsert와 전체교체를 구분해 before/after/loaded 증거 검증 |
| `sweep.py` | 유실 감시 CronJob 본체 (정상 시 no-op) |

## Terminal reconciler ledger preflight

Run the fingerprint command only in a container cloned from the live
`jw-ingest-sweep-daily` Job template. The command requires the explicit
`MARIADB_*` environment and rejects either SQLite ledger variable, so running
it in the shadow hook Pod fails closed.

Capture is read-only and never authorizes activation:

```bash
python -m pipeline.scripts.ingest_hook.ledger_fingerprint --report-only
```

After the PL records the reported host, database, and fingerprint, the final
pre-activation check must compare all three:

```bash
python -m pipeline.scripts.ingest_hook.ledger_fingerprint \
  --expected-host "$EXPECTED_MARIADB_HOST" \
  --expected-database "$EXPECTED_MARIADB_DATABASE" \
  --expected-fingerprint "$EXPECTED_LEDGER_FINGERPRINT"
```

Only output with `activation_allowed=true` is an activation gate success.
`--report-only` deliberately emits `activation_allowed=false` even though the
read-only command exits successfully.

## 멱등·직렬화

식별자 = `(epoch, category, manifest_sha)`. 동일 webhook 재수신은
queued/running/complete 인 동안 no-op; failed 만 재큐. 같은 category 는 Job 1개
직렬(FIFO), 타 category 병렬. 상태 회신은 `GET /ingest/status` (D-4).

`GET /ingest/queue`는 portal local store와 무관하게 ledger의 running/queued
항목만 결정적 순서로 반환한다. 응답은 `{"items": [...]}`이며 각 항목은
`epoch`, `category`, `manifest_sha`, `status`, `reason`, `job_name`, `run_id`,
`uploaded_by`, `received_at`, `started_at`, `finished_at`,
`blocked_by_category`, `requires_reconcile`만 포함한다. 기존
`GET /ingest/status` 응답에는 마지막 두 boolean만 additive로 추가된다.

Job terminal signal은 `/ingest/terminal`로 돌아와 ledger에서 슬롯 해제를
확인한 뒤 다음 queued 항목을 승격한다. callback 배포 전에 이미 놓친 terminal
signal은 서비스 startup의 1회 idle-category drain으로 회복한다. 두 경로 모두
ledger atomic reservation을 사용하므로 여러 hook replica가 경쟁해도 category별
running은 최대 1개다.

## 환경 변수

| 변수 | 의미 |
|---|---|
| `INGEST_INPUT_ROOT` | 제출 파일·manifest 루트 (필수, 기본값 없음) |
| `INGEST_LEDGER_SQLITE` | 설정 시 sqlite ledger (리허설/테스트) — 미설정 시 mart DB(MARIADB_*) |
| `INGEST_JOB_IMAGE` | Job 이미지 (기본 = 운영 orchestrator digest pin) |
| `INGEST_JOB_NAMESPACE` | 기본 `llmops` |
| `INGEST_QUEUE_DRAIN_WEBHOOK_URL` | Job terminal signal을 받을 hook 내부 `/ingest/terminal` URL |
| `INGEST_QUEUE_DRAIN_WEBHOOK_ATTEMPTS` | terminal callback 시도 횟수 (3~5, 기본 3) |
| `INGEST_REHEARSAL_ROOT` | 설정 시 job_runner 격리 모드 (sqlite staging, orchestrator 미호출) |
| `INGEST_LOAD_STAGING_ROOT` | ★J5 실 로더 격리 출력 루트 (설정 시 mart refresh skip = staging-verify) |
| `INGEST_LOAD_STAGING_DB` | table loader 격리 스키마(필수, `jw_ingest_*`만 허용). 배포 manifest에는 활성화 승인 전 미설정 |
| `INGEST_LOAD_SHADOW_ROOT` | UBIST full-path shadow corpus. `/market-output`의 자식이어야 하며 staging/target과 배타 |
| `INGEST_SHADOW_LEDGER_SQLITE` | shadow 전용 ledger 경로. 운영 ingest ledger와 분리되며 trigger와 Job이 같은 RWX 파일 사용 |
| `INGEST_SHADOW_TARGET_DB` | 격리 publish DB. `jw_mart_ingest_shadow_` prefix 필수 |
| `INGEST_SHADOW_BUILD_PREFIX` | 격리 S4 build DB prefix. `jw_mart_ingest_shadow_` prefix 필수 |
| `INGEST_SHADOW_SEED_ROOT` | 첫 shadow corpus를 seed할 read-only UBIST corpus root |
| `INGEST_SHADOW_CATALOG_ROOT` | shadow root 아래에 격리 seed한 S4 catalog root |
| `INGEST_SHADOW_CRASH_AT` | shadow-only deterministic recovery injection point. production에서는 fail-closed |
| `INGEST_LOAD_TARGET_ROOT` | J5 프로덕션 출력 루트 (D-3; refresh 실행). staging 미설정 시 필수 |

## 적재 모드

| 모드 | load | mart build/gates | publish | refresh/ledger |
|---|---|---|---|---|
| staging | 격리 임시 root | skip | skip | 운영 mart/cache 무접촉, 운영 ledger |
| shadow | RWX shadow corpus | 격리 S4 + Sigma + post-gate | shadow DB 2테이블만 | shadow DB readback + shadow SQLite ledger |
| production | RWX serving corpus | 격리 build + gates | serving DB | post-mart orchestrator + 운영 ledger |

Shadow 배포 계약은
`deploy/k8s/ingest-hook/reference/ingest-trigger-shadow-overlay.yaml`과
`ingest-job-shadow-overlay.yaml`에 기록한다. 두 파일 모두 참고용이며 직접 apply하지 않는다.

## 카테고리 table loader 경계

| 카테고리 | canonical 적재 경로 | 격리 table | 방식 |
|---|---|---|---|
| `iqvia_nsa` | `pipeline.etl.io.iqvia_loader.load_source` | `iqvia_nsa_quarterly_raw` | append + source sheet resume |
| `iqvia_csd_channel` | `brand_activity.raw_db.load_sources` | `raw_csd_channel_dynamics` → `csd_channel_dynamics_stage` | raw append, stage 전체교체 |
| `iqvia_csd_keyword` | `brand_activity.raw_db.load_sources` | `raw_keyword_events` → `km_keyword_event_stage` | raw append, stage 전체교체 |
| `mi_master` | `brand_activity.master_market_group_load.load` | `stg_master_market_definition`, `stg_master_mapping_table` | 두 stage table 전체교체 |

CSD/Keyword stage는 raw 전체의 최근 36개월을 `TRUNCATE + INSERT`로 재구축한다.
따라서 primary 행수 증거는 raw append를 사용하고, stage는 `replace` 증거로 별도
기록한다. 이 전체교체 경로와 suspend된 기존 CronJob을 동시에 활성화하면 충돌할
수 있으므로 production 활성화 전 상호배제와 topic assignment 재실행을 별도 승인한다.

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
2. mart DB 에 `ingest_ledger` 및 append-only
   `ingest_status_transition` DDL 적용
   (`ledger.py`의 `_DDL_MYSQL`, `reference/ingest-status-transition.sql`)
3. D-3a 격리 검증 완료 후에만 production output root 와 sweep resume 여부 결정
4. 사이트에 webhook URL(`/ingest/webhook`)·상태 URL(`/ingest/status`) 공유

이미지 배포 및 source-to-digest 추적 규약은
`docs/runbooks/immutable_image_references.md` 를 따른다.
