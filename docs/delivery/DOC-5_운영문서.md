# DOC-5. JW Market 운영 문서 (관리자용)

| 항목 | 값 |
|---|---|
| 기준 소스(develop) | `7ca98403` (worktree `/tmp/jwm-develop-docs`; 761b4def→7ca98403 전진 12커밋은 전부 ingest_hook·deploy/k8s/{ingest-hook,crawler}·RUNBOOK·tests 영역 — API·DB·시장분석 서술은 무변경) |
| 운영 backend | GKE ns `llmops`, deployment `jw-market-backend-api` (HPA min 2 / max 8, 현재 8 replicas), generation **302** |
| 운영 이미지 APP_VERSION | `ad782bc064ba03a45eaa4f1e301dbd75b8bf9a9e` (release annotation `jw-market/release=f139-brand-activity-general-scope`) |
| 생성일 / 버전 | 2026-07-17 / v1.0 |

> 이 문서는 **관리자 운영**용이다. 자격증명 값은 일절 기재하지 않는다(계정명·secret 리소스명·보관 위치만 기술). 실체 확인이 불가한 절차는 `[확인 필요]`로 명시한다.
> 인프라 실명·CronJob 실측 표의 정본은 `docs/delivery`와 동일 근거(BASELINE 실측, 2026-07-17)이며, 이 문서의 CronJob 표는 그 실측을 그대로 옮긴 것이다.
> 월간 운영 정본 `RUNBOOK_MONTHLY.md`도 이번 전진에서 §5(crawl cutover 절)가 갱신됐다(8줄) — 3절·2-3절에 반영.

---

## 1. 시스템 개요 — 무엇이 어디서 도는가

- **서빙(backend)**: `jw-market-backend-api` (ns `llmops`, HPA min 2 / max 8, ClusterIP svc `jw-market-backend-api-service` :80 → :8000). 사용자 요청은 여기로만 들어온다. 데이터 적재/배치는 서빙과 **프로세스·Pod·엔드포인트를 공유하지 않는다**(설계 원칙).
- **데이터 파이프라인(배치)**: ns `llmops`의 CronJob·Job 군. mart DB `jw_mart_d2_stage_20260630_r2`(MariaDB Galera `galera-mariadb-galera`)에 적재한다.
- **월간 post-mart 체인**: `cache → forecast → strength → shortlong → events → elements`. 단일 진입점 `python -m pipeline.orchestrator run --mode full`. 이 명령은 raw source를 mart로 재적재하지 않으며, mart 갱신 뒤 후처리만 수행한다. 상세 절차는 `RUNBOOK_MONTHLY.md`가 정본.
- **사이트(입력)**: `jw-data-portal`(입력·업로드, `v0.6.0-8ca9d98`), 별도 Deployment. 증분 훅(3절)이 이 사이트의 제출 확정(webhook)을 받는다 — **배포·기동됨(리허설 격리 모드), 실적재 미전환**.

운영자가 알아야 할 두 축: **(A) 정기 전체 재적재(월간/분기, 2절)**와 **(B) 증분 적재 훅(3절, 배포·기동됨 — 리허설 격리 모드로 실 mart 미반영)**. 라이브로 도는 것은 (A)와 그 부속 CronJob들, 그리고 (B)의 트리거 서비스(`jw-ingest-hook`)·sweep CronJob이다. (B)의 실적재 전환(리허설 env 해제)은 PL 게이트로 남아 있다(3-5절).

---

## 2. 월간 / 분기 정기 운영

### 2-1. 월간 정기 실행 (mart 갱신 후)

절차 요약(정본: `RUNBOOK_MONTHLY.md` §1):

1. **mart 갱신 감지는 자동 설계** — ETL 적재 성공 kick(1차) 또는 매일 폴링(안전망)이 오케스트레이터를 깨우고, mart epoch가 바뀐 경우에만 full 체인이 실행된다. 갱신 전이면 no-op.
   - 단, 오케스트레이터 poll CronJob은 현재 **suspend=True**(2-3절 표) — 라이브 자동 실행이 아니라 수동 트리거 기준으로 운용한다.
2. **수동 실행**(GenOS/GCP pod에서):
   ```bash
   # CronJob 템플릿에서 1회성 Job 생성
   kubectl -n llmops create job --from=cronjob/jw-pipeline-orchestrator-poll-daily orch-manual-$(date +%Y%m%d)
   kubectl -n llmops logs -f job/orch-manual-$(date +%Y%m%d)
   # 또는 pod 셸에서 직접
   python -m pipeline.orchestrator run --mode full --dry-run   # 계획 검토(write 0)
   python -m pipeline.orchestrator run --mode full             # 실행
   ```
3. **게이트**(빌더 내장, 실패 시 체인 자동 중단):
   - forecast: expected blocks 43,474 / horizons 3,000 · epoch 단일성 · verify-sample
   - strength: `--expected-workflow-rev` pin(불일치 즉시 abort) · coverage(source_units ≥ 35,521 · brands ≥ 24,789) · LLM 멱등(rev·hash 무변경 재실행 = calls 0)
   - events: staging validate → backup → apply → post-verify
   - env assert: rev env 부재 시 즉사(코드에 baked 기본값 없음 — fail-closed)
4. **검증**(체인 완료 후): 골든 4종 canonical 대조 · 행수 `COUNT(*)`(strength 24,789 브랜드 / forecast block 43,474 / elements 26,411) · `/api/brands` 골든 sha256 대조.
5. **실패 시 재개**: 원인 수정 후 `python -m pipeline.orchestrator run --mode full --from-stage <실패단계>`. 선행 단계는 현재 epoch에서 completed 상태일 때만 통과(멱등이라 처음부터 재실행해도 안전).

### 2-2. 수시 실행

- **증분(신규 브랜드 N개)**: `python -m pipeline.orchestrator run --mode incremental --dry-run` → 확인 후 `--dry-run` 없이. cache·elements는 커버리지 차집합으로 신규 brand_key만, strength·shortlong은 hash로 무변경 skip. forecast·events는 브랜드 단위 증분 없음(epoch 변경 시 전량).
- **부분 재생성(특정 계열)**: `python -m pipeline.orchestrator run --stages strength`. 선행 산출이 stale이면 경고 후 중단하며, 우회는 `--force`(로그·state에 기록됨).
- **mart 세대(DB) 교체**: `pipeline/scripts/utils/mart_config.py`의 `DEFAULT_MART_DB_NAME` 수정 → `pytest tests/deploy/test_mart_db_single_source.py`가 남은 고정 사본을 파일:줄로 열거 → 같은 커밋에서 일괄 갱신. manifest의 `test "$VAR" = "..."` guard는 의도된 fail-closed 이중기입이므로 삭제 금지.

### 2-2-1. raw source → mart 전체 재현 리허설 (운영 무접촉)

`run --mode full`과 전체 재적재를 혼동하지 않는다. 전자는 post-mart 체인이고, raw source부터의 R-1 재현은 별도 진입점이다.

```bash
python -m pipeline.orchestrator rehearse-full \
  --input-manifest /path/to/r1-inputs.json \
  --target-db jw_mart_rehearsal_<run-id> \
  --cache-db jw_mart_s6_rehearsal_<run-id> \
  --source-db jw_mart_d2_stage_20260630_r2 \
  --work-dir /tmp/r1-<run-id> \
  --dry-run
```

- input manifest v1은 `ubist_source_dir`, `iqvia_source_dir`, `mi_master`를 모두 명시한다. 누락·빈 디렉터리·지원하지 않는 파일 형식은 DB 연결 전에 실패한다.
- mart schema는 `jw_mart_rehearsal_`, cache schema는 `jw_mart_s6_rehearsal_` 접두만 허용한다. 두 schema는 분리되며 publish/RENAME 단계가 없다.
- `--dry-run`은 명령 계획만 JSON으로 출력하고 work directory와 DB를 만들지 않는다.
- dry-run 이후 실제 실행은 격리 DB write다. 운영 mart와의 전수 대조 및 R-1 게이트가 통과하기 전에는 "전체 재적재 재현 완료"로 보고하지 않는다.

### 2-3. 상시 CronJob 운영 표 (ns `llmops`, 실측 2026-07-17)

> cron 스케줄은 별도 표기가 없으면 **UTC**(예: `0 16 * * *` = 01:00 KST). `SUSPEND=True`는 등록만 되고 자동 실행되지 않는 상태다.

| CronJob | 스케줄 | SUSPEND | 상태 / 성격 |
|---|---|---|---|
| `jw-cache-refresh-daily` | `0 20 * * *` (05:00 KST) | False | 라이브. cache_deep_analysis events staging→backup→apply→post-verify→drop |
| `jw-agent3-refresh-daily` | `0 21 * * *` (06:00 KST) | False | 라이브. 브랜드 strength(wf316), rev pin 5692 |
| `brand-activity-topic-monthly` | `0 19 4 * *` | False | 라이브. 브랜드 활동 topic |
| `brand-activity-row-topic-monthly` | `0 22 4 * *` | False | 라이브. 브랜드 활동 row-topic |
| `dynamic-market-cache-warm` | `7,37 * * * *` | False | 라이브. 동적 시장 캐시 워밍 |
| `dynamic-market-cache-warm-test2` | `7,37 * * * *` | False | 라이브(test2 계열) |
| `jw-news-crawl-tier1-daily` | `10 18 * * *` | False | 라이브. 뉴스 크롤 tier1 |
| `jw-news-crawl-tier2-daily-slice` | `40 18 * * *` | False | 라이브. 뉴스 크롤 tier2(brand_key 해시 7분할 요일 로테이션) |
| `jw-gitea-dump-daily` | `40 19 * * *` (04:40 KST) | False | 라이브. Gitea 백업(5절) |
| `jw-ingest-sweep-daily` | `30 19 * * *` (04:30 KST) | False | 라이브. 증분 훅 유실 감시(3-4절). ★현재 리허설 격리 모드 |
| `jw-pipeline-orchestrator-poll-daily` | `0 16 * * *` (01:00 KST) | **True** | 예비/미활성. 월간 체인 안전망 poll. DO-NOT-RESUME(훅 착지 시 대체 예정) |
| `jw-csd-sensor` | `*/10 * * * *` | **True** | 예비/미활성. CSD 파일 도착 센서. DO-NOT-RESUME |
| `jw-brand-activity-run` | `0 0 30 2 *` | **True** | 예비/미활성. 브랜드 활동 수동 실행 템플릿 |
| `iqvia-general-sidecar-quarterly` | `0 3 5 1,4,7,10 *` (KST) | **True** | 예비/미활성. 분기 IQVIA 일반차원 사이드카 |
| `jw-news-crawl-retention-daily` | `0 19 * * *` | **True** | 예비/미활성. 크롤 보존 정리 |
| `jw-news-crawl-tier1-daily-canonical` | `10 18 * * *` | **True** | **강등(demoted)**. cutover 실행 완료(상류 `30763e9c`)로 라이브 tier1이 재설계판으로 전환됨 → 이 `-canonical` 사본은 **삭제 후보**. resume 금지(이중 기동 위험) |
| `jw-news-crawl-tier2-daily-slice-canonical` | `40 18 * * *` | **True** | **강등(demoted)**. cutover 완료. tier2 category 결손은 `refresh-live-categories` 호출 부재가 근인이었고 라이브 tier2 body에 refresh 스텝 추가로 해소됨(RUNBOOK §5). 이 사본은 **삭제 후보**·resume 금지 |

부속 리소스(참고): 오케스트레이터 poll은 state PVC `jw-pipeline-orchestrator-state`(1Gi)를 사용하고, csd-sensor는 `jw-csd-sensor-state`(100Mi)를 쓴다. 두 CronJob 모두 suspend 중이라 실제 디스크는 프로비저닝되지 않는다.

> **★ suspend=True 항목 취급**: (a) **예비/미활성**(poll·csd-sensor·brand-activity-run·iqvia-sidecar·crawl-retention) — 훅/재설계 착지 시 대체 예정, 무단 resume 금지. (b) **강등(삭제 후보)**(crawl `-canonical` 2종) — cutover가 이미 실행돼 라이브가 재설계판으로 전환됨. resume하면 라이브와 이중 기동·역행. 실제 삭제는 훅/재설계 안정화 후 PL 판단. 어느 경우든 무단 resume·digest 교체 금지.

---

## 3. 증분(훅) 운영 체계 — 활성화(리허설 모드)

> **★ 현재 상태(2026-07-17 재실측)**: 훅 체계가 클러스터에 **배포·기동됐다** — Deployment `jw-ingest-hook`(1/1, 이미지 `jw-pipeline-orchestrator@sha256:fea29685…`), Service `jw-ingest-hook`(:8080), CronJob `jw-ingest-sweep-daily`(`30 19 * * *` UTC, suspend=False), mart DB에 `ingest_ledger` 테이블 생성됨(현재 3행). 사이트도 `v0.6.0-8ca9d98`로 재배포되어 MinIO·훅 트리거/상태 URL env가 붙었다.
> **단 완전 운영 전환 전 단계다**: 트리거 서비스·sweep에 **`INGEST_REHEARSAL_ROOT=/tmp/ingest-rehearsal`가 설정돼 있어 `job_runner`가 격리(리허설) 모드로 동작한다**(`config.py` 계약). 즉 제출을 받으면 CSV를 sqlite staging에 적재해 Σ게이트까지 검증하되 **오케스트레이터/mart 실적재(refresh)는 호출하지 않는다**(운영 무접촉 E2E). 리허설 env 제거 + 실적재 경로 승인(refresh 활성)은 **PL 게이트(D-3)**로 남아 있다.

### 3-1. 데이터 흐름

```
jw-data-input(사이트) "제출 확정"
   └─ webhook →  트리거 서비스(jw-ingest-hook Deployment, orchestrator 이미지, uvicorn --factory)
        POST /ingest/webhook {"manifest_path": ...}
        └─ contract 파싱(manifest v2.1) → ledger.receive() 큐잉
             └─ promote(): category당 running 1개(FIFO) → k8s Job 기동
                  └─ Job 내부(job_runner): contract → G3 → load → Σ게이트 → refresh(incremental) → ledger complete
```

제출 소스는 MinIO 시장 버킷(S3 모드, `s3_input.py`)이며, 트리거 서비스 env는 `INGEST_INPUT_ROOT`/`MINIO_ENDPOINT`(=`http://minio.llmops.svc.cluster.local:9000`)로 버킷을 가리킨다.

- **트리거 서비스는 서빙과 분리된 별도 Deployment**다(`jw-market-backend-api`에 엔드포인트를 추가하지 않는다 — 설계 STOP 조건의 코드화).
- 트리거 서비스 엔드포인트(사이트 계약 표면 전부):
  - `POST /ingest/webhook` — 제출 확정 수신(manifest가 `complete=true`가 아니면 409)
  - `GET /ingest/status?epoch=&category=&manifest_sha=` — 상태 회신
  - `POST /ingest/reconcile` — 큐잉된 제출 승격(운영/sweep 헬퍼)
  - `GET /healthz`

### 3-2. manifest 계약 v2.1

- 정본은 코드 `pipeline/scripts/ingest_hook/contract.py`(계약 문서와 불일치 시 코드가 우선하되, 변경 시 문서 v2.1도 같은 라운드에 갱신).
- 필수 필드: `contract_version`(="v2"), `epoch`, `category`, `complete`(bool), `files[]`(각 `path`·`sha256`(64 hex), 선택 `rows`·`period_start/end`).
- v2.1 델타: **주간 epoch**(`2026-W27`, ISO week 01–53) 지원, 선택 감사 필드 **`uploaded_by`**(사이트 세션 이메일 — 없거나 형식이 이상해도 제출을 실패시키지 않음).

### 3-3. 멱등·직렬화·상태

- 식별자 = `(epoch, category, manifest_sha)`. 동일 webhook 재수신은 상태가 `queued`/`running`/`complete`인 동안 no-op이고, `failed`만 재큐.
- 같은 category는 Job 1개 직렬(FIFO), 다른 category는 병렬.
- ledger 상태 전이(`ledger.py`): `queued → running → complete` (성공) / `→ failed` (실패). failed는 webhook·sweep 재수신 시 다시 `queued`로 승격 가능.
- Job 내부 실행 순서(`job_runner.py`)는 코드로 강제된다: **contract → G3(구조 검증) → load → Σ 게이트 → refresh → complete**. 어느 단계든 실패하면 ledger row가 `failed`로 표기되고 non-zero 종료, **아무것도 승격되지 않는다**(G3 실패 = 적재 0).
- **Σ 게이트는 두 층**이다:
  - `sigma_gate.check_staging` — staging 적재분의 기본 정합(리허설 경로에서도 수행).
  - **`sigma_market.check_market_sigma`(신규, `sigma_market.py`)** — 실적재 경로에서 **Σ 브랜드 raw_value == market 시장 계열(`market_size_series`)**을 재대사한다. 대사 grain = `(source, measure='sales', atc4_code, period)`, 적재된 epoch의 기간으로 스코프. 허용오차 abs 0.01 / rel 0.001. 시장 계열에 해당 기간이 아예 없으면 skip이 아니라 **실패**(적재가 썼다고 주장한 기간을 mart가 못 받은 것). pin 근거(2026-07-17 라이브 인구조사): ubist 364/364·iqvia_nsa 538/538, 최악 상대오차 0.000000%.
- **리허설 모드(현재 운용)에서는** load가 CSV→sqlite staging이고 `check_staging`까지만 수행하며, `refresh`(오케스트레이터)와 `sigma_market` 실적재 대사는 **건너뛴다**(로그 `phase=refresh status=skipped reason=rehearsal`). `sigma_market` 게이트는 리허설 env 제거 후 실적재 경로에서 발동한다.

### 3-4. 유실 감시(sweep)

- `jw-ingest-sweep-daily` CronJob(**현재 라이브**, `30 19 * * *` UTC = 04:30 KST)이 제출 루트를 매일 스캔해 ledger에 없거나 failed-stale인 complete manifest를 재킥한다. 정상일에는 전부 이미 기록돼 있어 **no-op**. (현재 리허설 env가 걸려 있어 재킥도 리허설 격리 실행이다.)

### 3-5. 완전 운영 전환 절차 (남은 PL 게이트)

배포·ledger DDL·사이트 연동은 이미 완료됐고(위 현재 상태), 남은 것은 **리허설 → 실적재 전환**이다. 이 전환 전에는 어떤 제출도 mart를 건드리지 않는다.

1. 리허설 검증 증적 확인: `tests/ingest_hook/`(G-1 E2E, G-2 G3 거부, G-3 멱등, G-4 sweep) 및 라이브 `ingest_ledger` 처리 이력(현재 3행).
2. **트리거 Deployment·sweep CronJob의 `INGEST_REHEARSAL_ROOT` env 제거**(리허설 격리 해제) — 이후 `job_runner`가 실적재(`spec.load_argv`) + `refresh`(`pipeline.orchestrator --mode incremental`) + `sigma_market` 실 대사를 수행한다.
3. Σ 실 대사(D-3)가 대상으로 삼는 DB가 staging인지 live인지 확정(코드 주석상 D-3 승인 전까지 staging).
4. 전환 후 첫 실제 제출을 좁은 스코프로 감시(`GET /ingest/status`, ledger `row_counts`, Σ 게이트 로그 `worst_rel`).

계약/필드는 배포 이미지에 이미 반영돼 있으므로(`contract.py`/`category_map.py`), 전환은 코드 변경이 아니라 **env 토글 + PL 승인**이다.

---

## 4. 장애 대응

### 4-1. 정기 파이프라인(오케스트레이터) 실패

- 로그 확인: `kubectl -n llmops logs job/<job-name>`(JSON 1줄 1이벤트).
- 재개: 원인 수정 후 `python -m pipeline.orchestrator run --mode full --from-stage <실패단계>`(2-1 §5). state가 유실됐으면 선행부터 재실행(멱등).
- 게이트 위반(coverage·rev pin·행수)은 fail-closed로 체인을 중단시키므로, **게이트 메시지의 기대값과 실제값을 먼저 대조**한다(수치 불일치 = 데이터/rev 문제이지 코드 재기동 대상이 아님).

### 4-2. 재적재 원복 (staging → 승격 실패/이상 시)

- 모든 write는 빌더의 **staging → gate → swap** 경로로만 이뤄진다(live 테이블 직접 write 금지). cache-refresh는 apply 전 `cache_deep_analysis_bak_d2_prev3_<RUN_ID>`로 라이브를 백업한다.
- 이상 감지 시 원복은 백업 테이블을 원자 `RENAME`으로 되돌리는 방식(과거 F-124a 승격에서 검증된 패턴). 대상 백업 테이블명과 원복 SQL은 실행 전 보존한다.
- staging/backup 테이블은 `_bak_*`·`_stage_*`·`_backup_*` 등 패턴으로 식별하며 정본이 아니다(정본: `catalog_ml_market`/`catalog_cd_market`/`catalog_strategic_brand`).

### 4-3. 증분 훅 Job 실패 (현재 라이브 — 리허설 모드)

- 상태 확인: `GET /ingest/status?epoch=&category=&manifest_sha=` → `status=failed`·`reason` 확인. 또는 ledger 직접 조회(`ingest_ledger` 테이블, `status='failed'`).
- 재킥: 동일 manifest webhook 재수신 또는 `POST /ingest/reconcile`(failed → queued 재승격). sweep CronJob도 다음 주기에 자동 재킥한다.
- G3/Σ게이트 실패는 **적재 0**이 정상 동작이므로, 재킥 전에 manifest·원본 파일의 구조(스키마·기간·행수)를 먼저 교정한다.
- **현재는 리허설 격리 모드**이므로 실패해도 mart 영향이 없다(staging sqlite만). 완전 운영 전환(3-5절) 후에는 Σ 실 대사(`sigma_market`) 실패 = mart 미승격이 되므로, 실패 `reason`의 `MarketSigmaError`(Σ브랜드 != 시장 계열, 또는 기간 결손)는 원본 데이터/적재 스코프 문제로 다룬다.

### 4-4. backend 롤백 (이미지 digest 교체)

- backend는 digest-pin 이미지로 배포된다(운영 gen 302, 이미지 `jw-market-backend-api@sha256:d5e2…cd66`(deployment 실측); APP_VERSION `ad782bc0`).
- 롤백은 배포 매니페스트의 이미지 digest를 이전 정상 digest로 교체 → `kubectl -n llmops set image deployment/jw-market-backend-api ...` 또는 `kubectl rollout undo deployment/jw-market-backend-api`.
- 검증: `GET /api/health`의 `version` 필드가 목표 커밋(APP_VERSION)과 일치하는지 대조(§6).
- ★ `api/Dockerfile` 및 보호 blob은 수정 금지(계약 테스트가 sha256 pin). agent3 Job도 같은 backend 이미지 계보를 쓰므로 롤백 시 rev pin(`AGENT3_WORKFLOW_REV=5692`) 정합을 함께 확인한다.

---

## 5. 백업

- **Gitea 저장소 백업**: CronJob `jw-gitea-dump-daily`(라이브, `40 19 * * *` UTC = 04:40 KST). Gitea org `jw-market`(`jw-data-input.git`, `jw-market.git`)의 일일 덤프. 이 CronJob은 플랫폼 소관이며 이 repo `deploy/`에 매니페스트가 없다(클러스터 실측만 확인).
- **재적재 staging 백업 규약**: 각 빌더가 apply 전 라이브를 백업 테이블로 복제한다. 관측된 패턴 예: `cache_deep_analysis_bak_d2_prev3_<RUN_ID>`. 백업/작업용 테이블 접두 규약 = `_bak_*`·`_backup_*`·`_stage_*`·`_mig_stg_*`·`_old_*`·`__failed_*`·`_cutover_*`(정본 아님으로 분류).
- **mart DB(MariaDB Galera) 자체 백업**: `[확인 필요]` — 이 repo/실측 범위에서 DB 스냅샷·PITR·mysqldump 스케줄의 존재를 확인하지 못했다. Galera 3-노드 복제(`galera-mariadb-galera` 3/3)는 고가용이나 백업과는 별개이므로, 정기 논리 백업 정책은 플랫폼팀 확인이 필요하다.

---

## 6. 모니터링 포인트

실존이 확인된 지표만 기술한다.

- **backend 헬스**: `GET /api/health` → `{status:"ok", markets_loaded, brands_loaded, version}`. 운영 캡처(2026-07-17): `markets_loaded=25`, `brands_loaded=25`, `version=ad782bc0…`. 배포 전환 후 image tag / APP_VERSION / OpenAPI version 대조에 사용한다.
- **backend 오토스케일(HPA)**: `jw-market-backend-api-hpa`(대상 Deployment `jw-market-backend-api`) 존재 — **min 2 / max 8, memory 60% 타깃**. 재실측(2026-07-17) 시 현재 memory 85%(타깃 60% 초과) → **8/8 replicas**로 스케일아웃된 상태. 상태 확인: `kubectl -n llmops get hpa jw-market-backend-api-hpa`(TARGETS·MINPODS·MAXPODS·REPLICAS) / `kubectl -n llmops get deploy jw-market-backend-api`(READY). 지속적으로 max(8)에 붙어 있으면 max 상향 또는 메모리 타깃·요청량을 재검토한다.
- **CronJob 성공 여부**: `kubectl -n llmops get cronjob`(LAST SCHEDULE·SUSPEND) / `kubectl -n llmops get jobs`(COMPLETIONS)로 라이브 CronJob(2-3절 False 행)의 성공을 확인. 실패 Job은 `failedJobsHistoryLimit=3`으로 보존된 pod 로그를 확인.
- **파이프라인 로그**: 오케스트레이터·빌더는 JSON 1줄 1이벤트 로그(stdout + `--log-file`). 게이트 통과/중단이 이벤트로 남는다.
- **증분 훅(현재 라이브)**: 트리거 서비스 헬스 `GET /healthz`(Deployment `jw-ingest-hook` 1/1), ledger 상태 분포 `SELECT status, COUNT(*) FROM ingest_ledger GROUP BY status`, sweep CronJob `jw-ingest-sweep-daily` 성공 여부. Σ 게이트 로그(`gate=sigma … worst_rel=…`)로 정합 감시. 단 리허설 env가 걸려 있는 동안은 실적재/refresh가 스킵되므로 mart 반영은 없다.
- 그 외 대시보드/알림(Grafana·Alertmanager 등) 연동 여부: `[확인 필요]`.

---

## 7. 계정 · 권한

> 아래는 **리소스명·env 키 이름·보관 위치**만 기술한다. 자격증명 값은 이 문서에 없으며, 어떤 경우에도 문서/로그/커밋에 기재하지 않는다.

| 대상 | 식별 정보(값 아님) | 보관 위치 |
|---|---|---|
| Gitea(코드·사이트 저장소) | 계정 `llmops` / `jw-market-bot` / `jw-pl`, org `jw-market` | 운영 credential 보관처(ops VM `~/.rnd_creds.env` 등) — 값은 위치에서만 조회 |
| 사이트 인증(jw-data-portal) | env 키 `NEXTAUTH_URL`(= `https://jwai-dev.jwhealthcare.com/jw-data-portal/api/auth`), GenOS 연동 | 배포 env / k8s secret — 값 위치만 |
| MinIO(오브젝트 스토리지) | svc `minio`(9000)·`minio-console`(9090), ExternalName `llmops-minio-service`; 훅 env `MINIO_ENDPOINT`·`MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY`(secretRef), 제출 버킷 `INGEST_S3_BUCKET`(secretRef) | k8s secret(리소스명만). 증분 훅은 버킷 전용 **read-only MinIO 유저**로 읽는다(상류 `e479d087`). csd-sensor(예비)도 resume 전 scoped read-only 유저 사용 |
| MariaDB — 서빙(backend) | user `llmops`, secret `galera-mariadb-galera`(key `mariadb-password`) | k8s secret — 값 금지 |
| MariaDB — 배치(CronJob·ingest) | secret `jw-mart-d2-writer`(key `username`/`password`), mart writer 신원 | k8s secret — 값 금지 |

- backend와 배치가 **서로 다른 DB secret**을 쓴다: 서빙은 `galera-mariadb-galera`(user `llmops`), 배치(orchestrator·agent3·cache-refresh·ingest)는 `jw-mart-d2-writer`. 롤백/권한 조정 시 두 신원을 혼동하지 않는다.
- 이미지 레지스트리(AR): `asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01`. push는 GCP ops VM에서 `--platform linux/amd64`.
- GCP/노드 접근(SSH)·bastion 자격증명: 운영 credential 보관처(ops VM `~/.rnd_creds.env`)에서만 조회(값·경로 상세는 이 문서 범위 밖).

---

## 8. CronJob 소관별 상세 — [jw agent 기고 필요]

> **기고 자리.** 아래 §2 CronJob 표는 리소스 실측(이름·스케줄·suspend)만 담고 있어 "각 잡이 **무엇을 하는지**"가 부실하다. jw agent 세션이 크롤/BA/orchestrator 잡의 동작·산출물·의존을 채운다. 형식·규칙은 [README §3](README.md) 준수. 스켈레톤 본체는 [DOC-1b](DOC-1b_개발문서_크롤_BA파이프라인.md) 참조.

`[기고 필요]` 잡별로 다음을 서술: 무엇을 하는가(입력→처리→산출 테이블) · 선행/후행 의존 · 실패 시 영향 · 정상 소요·산출 규모. 근거: 잡 매니페스트·스크립트 `파일:줄`.

| CronJob | 소관 세션 | 동작 요약 | 상태 |
|---|---|---|---|
| `jw-news-crawl-tier1-daily` | jw agent | 광범위 뉴스 크롤(규칙 스코어). 입력: 뉴스 사이트 → `news_raw`/`events_raw`(processor `workflow_196_rev5674` 등). 스케줄 `10 18 * * *`, **suspend=false**. 후행: tier2·brand_activity가 소비. 실패 시 당일 뉴스 미수집(다음 날 증분 회복). 근거 `deploy/k8s/crawler/crawl-tier1-cronjob.yaml` | ✅ |
| `jw-news-crawl-tier2-daily-slice` | jw agent | 브랜드 정밀 AI 스코어(wf337). 당일 요일 슬라이스(brand_key 해시 1/7)만. sync-events-raw→append-live(60콜/₩203.40 상한)→**refresh-live-categories**(`events.category` 갱신, 2026-07-18 cutover 추가). 산출 `event_brand_scores`(71,318). 스케줄 `40 18 * * *`, **suspend=false**. 선행: tier1. 실패 시 해당 슬라이스 브랜드 스코어·카테고리 미갱신. 근거 `crawl-tier2-cronjob.yaml`, DOC-1b §1.4 | ✅ |
| `brand-activity-topic-monthly` | jw agent | 월간 토픽 생성(GenOS, `run_auto_topic.py --execute`, max 86콜). 입력: `csd_channel_dynamics_stage`·`km_keyword_event_stage` → 산출 `mart_brand_activity_topics`(11)+`_runs`. 스케줄 `0 19 4 * *`, **suspend=false**. 후행: row-topic. 실패 시 토픽 미갱신(전월분 유지). 근거 evidence §A, DOC-1b §2.4 | ✅ |
| `brand-activity-row-topic-monthly` | jw agent | 월간 row 단위 토픽 배정. 입력: 토픽+stage row → `row_topic_assignment`(172,419)/`_status`/`_share_view`. 스케줄 `0 22 4 * *`, **suspend=false**. 선행: topic-monthly. 실패 시 점유(share_view) 미갱신. 근거 evidence §A | ✅ |
| `jw-agent3-refresh-daily` | jw agent | 브랜드 강도(strength) 일일 갱신(wf316, rev env pin 5692·fail-closed). mart 프로파일 → `agent3_brand_strength`(25,153)/`_source`(35,521). input_hash+rev 매치 시 미호출(신규/변경만 과금). 스케줄 `0 21 * * *`, **suspend=false**. 실패 시 강도 미갱신. 근거 `deploy/k8s/agent3/agent3-refresh-cronjob.yaml`, DOC-1b §2.3 | ✅ |
| `jw-pipeline-orchestrator-poll-daily` | jw agent | 월간 데이터 체인(cache→forecast→strength→shortlong→events→elements)의 **안전망 폴링**. mart epoch 변경 시만 실행, 같은 epoch면 no-op(멱등). 스케줄 `0 16 * * *`, **suspend=true**. **★ 증분 훅 시스템(DOC-5 §3) 착지 시 대체·삭제 전제 — resume 금지**(단순 미가동 아님). 근거 `deploy/k8s/orchestrator/pipeline-orchestrator-poll-cronjob.yaml`, DOC-1b §3.4 | ⏳(예비) |
| `jw-cache-refresh-daily` | jw market | 서빙 캐시 재생성 (기존 §2 참조) | ✅ |
| `dynamic-market-cache-warm` | jw market | 동적 캐시 워밍 (기존 §2 참조) | ✅ |
| `iqvia-general-sidecar-quarterly` | jw market | 분기 IQVIA 사이드카 (기존 §2 참조) | ✅ |
| `jw-ingest-sweep-daily` | jw market | 증분 훅 sweep (§3 참조) | ✅ |
| `jw-gitea-dump-daily` | jw market | Gitea 백업 (§5 참조) | ✅ |

> jw market 소관 잡은 기존 §2·§3·§5에 서술됨. jw agent 소관(크롤·BA·orchestrator·agent3) 행의 `[기고 필요]`만 채우면 된다.

---

## 부록 A. 금지 사항 (요약 — `RUNBOOK_MONTHLY.md` §7)

1. **BRANCH_POLICY**: `codex/crawl-2tier`·`codex/short-long-lineage-bulk`·`3f0db0ae` 계보 develop 머지 금지. 크롤 이미지를 역사 브랜치에서 빌드 금지.
2. **live 테이블 직접 write 금지** — staging→gate→swap 경로로만.
3. Agent3 rev는 manifest env로만(`AGENT3_WORKFLOW_REV` + `--expected-workflow-rev`) — 코드 기본값 없음(부재 시 즉사가 정상).
4. `api/Dockerfile`·보호 blob 수정 금지(계약 테스트 pin).
5. 운영 중 CronJob digest 임의 교체·suspend=True 항목 무단 resume 금지(별도 PL 게이트).
6. mart DB명 신규 하드코딩 금지 — drift gate가 fail시킴(2-2 절차로만).
7. `cache_brand_elements.py`의 `--dry-run`과 `--pilot-fill`은 독립 플래그 — 병용 금지(검토는 `--dry-run` 단독).
8. 표준 검증: 골든 4종 · `COUNT(*)` · tracked-only pytest(최상위 `tests/test_*.py` 2건 포함).
