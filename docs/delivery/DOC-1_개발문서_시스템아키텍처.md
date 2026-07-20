# DOC-1 개발 문서 — 시스템 아키텍처

| 항목 | 값 |
|---|---|
| 기준 코드(백엔드/파이프라인) | 원격 `develop` live HEAD (`git fetch jw-private develop && git rev-parse jw-private/develop`) |
| 운영 백엔드 | GKE ns `llmops`, Deployment `jw-market-backend-api`; generation·imageID·APP_VERSION은 아래 live query로 확인 |
| 사이트 코드(jw-data-input) | Gitea `jw-market/jw-data-input.git` 활성 브랜치의 원격 HEAD |
| 운영 사이트 | Deployment `jw-data-portal` + `jw-data-portal-worker`; imageID는 live query로 확인 |
| 갱신일 | 2026-07-20 |
| 문서 버전 | v2.0 |

> **좌표 규칙.** 이 문서에 남은 과거 SHA·digest·행수는 당시 evidence를 설명하는 역사값이며 현재 운영 좌표가 아니다. 운영 작업 전 [배포·승격·롤백 런북](RUNBOOK_배포_승격_롤백.md)의 live query로 코드 SHA, Deployment generation, imageID와 APP_VERSION을 다시 고정한다.

```bash
git fetch jw-private develop && git rev-parse jw-private/develop
kubectl -n llmops get deploy jw-market-backend-api jw-market-backend-api-test -o json
kubectl -n llmops get pods -l app=jw-market-backend-api -o json
kubectl -n llmops get deploy jw-data-portal jw-data-portal-worker -o json
```

---

## 1. 전체 구성도

시스템은 네 개의 흐름으로 나뉜다. ①은 사용자 질의 응답(서빙), ②는 데이터 인입(업로드), ③은 뉴스 크롤·스코어링, ④는 코드 정본 보관이다.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ ① 서빙 경로 (사용자 → 답변)                                                     │
│                                                                              │
│  [사용자 브라우저]                                                             │
│       │  HTTPS                                                                │
│       ▼                                                                       │
│  [포탈 portal-front / portal-back]  ← ns=portal, SI 소관                       │
│       │  ClusterIP HTTP                                                       │
│       ▼                                                                       │
│  jw-market-backend-api-service (ClusterIP :80 → :8000)                        │
│       │                                                                       │
│       ▼                                                                       │
│  deployment jw-market-backend-api (HPA 2~8, mem 60%; 캡처 8, uvicorn workers=1) │
│       │  PyMySQL                                                              │
│       ▼                                                                       │
│  MariaDB Galera  ── DB: jw_mart_d2_stage_20260630_r2 (전 차원 공통)            │
│                     DB: jw_brand_activity_stage (브랜드활동)                    │
│                                                                              │
│  캐시 워밍: CronJob dynamic-market-cache-warm (매시 07/37분) → dynamic_market_* │
├─────────────────────────────────────────────────────────────────────────────┤
│ ② 인입 경로 (데이터 업로드 → mart)                                              │
│                                                                              │
│  [사용자] → jw-data-portal (Next.js) → jw-data-portal-worker                    │
│                    │                                                          │
│    (현행 운영: R&D 등 일반 업로드)  ▼                                           │
│         NFS /nfs-root/autoIngestion  ← UPLOAD_BASE_PATH, STORAGE_PROVIDER=local│
│                                                                              │
│    (시장 인입: 배선 완료·클러스터 기동, 리허설 격리 모드)                        │
│         → MinIO(S3 서명 URL, minio.llmops:9000) → manifest PUT → webhook       │
│              → jw-ingest-hook (Deployment 1/1, svc :8080) → G3 검증            │
│                   → incremental Job → orchestrator → mart                     │
│              ※ INGEST_REHEARSAL_ROOT 설정 = job_runner 격리(sqlite staging)    │
│              ※ 안전망: CronJob jw-ingest-sweep-daily (04:30 KST, active)        │
├─────────────────────────────────────────────────────────────────────────────┤
│ ③ 크롤 경로 (뉴스 → 이벤트 스코어)                                              │
│                                                                              │
│  CronJob jw-news-crawl-tier1-daily (18:10) → news_raw                         │
│  CronJob jw-news-crawl-tier2-daily-slice (18:40, brand_key 해시 7분할 요일)     │
│       → corpus_loader → news_raw/events_raw → tier2 LLM 스코어(wf337)          │
│  CronJob jw-agent3-refresh-daily (21:00 UTC=06:00 KST) → agent3 brand strength │
├─────────────────────────────────────────────────────────────────────────────┤
│ ④ 코드 정본 / 백업                                                             │
│                                                                              │
│  Gitea (llmops-gitea) org jw-market: jw-data-input.git, jw-market.git         │
│  CronJob jw-gitea-dump-daily (19:40) → dump 백업                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

핵심 경계 두 가지:
- **서빙 무접촉 원칙.** 인입·크롤 파이프라인은 `jw-market-backend-api`에 엔드포인트를 추가하지 않는다. 트리거 서비스(`jw-ingest-hook`)는 별도 Deployment다(`pipeline/scripts/ingest_hook/README.md` §경계).
- **포탈(portal-front/portal-back)은 SI 소관.** ns `portal`에 위치하며(evidence/k8s_portal_vmhome.txt), 우리 소관은 ns `llmops` 전부다(BASELINE §인프라).

---

## 2. 컴포넌트별 상세

### 2.1 jw-market-backend-api — 시장분석 조회 API (서빙 핵심)

| 항목 | 값 | 근거 |
|---|---|---|
| 역할 | 시장/브랜드/원인/심층분석/브랜드활동 조회 API. mart를 읽어 화면 카드·시계열·필터를 응답 | `pipeline/scripts/api/main.py` |
| 기술 스택 | FastAPI 0.135.1 · uvicorn · Pydantic 2 · PyMySQL 1.1 · numpy/pandas/statsmodels · PyYAML | `pipeline/scripts/api/requirements.txt` |
| repo 위치 | `pipeline/scripts/api/` (routes·handlers·composers·dynamic_market·models) | — |
| 배포 형태 | Deployment + HPA `jw-market-backend-api-hpa`; 현재 replica·generation·node 배치는 live query로 확인 | `kubectl -n llmops get deploy,hpa,pod -o wide` |
| 진입점 | `uvicorn pipeline.scripts.api.main:app --workers 1` | `api/Dockerfile` CMD |
| 컨테이너 | Python 3.11-slim, 비-root UID/GID 3000, `/app` workdir, `EXPOSE 8000` | `api/Dockerfile` |
| 서비스 | `jw-market-backend-api-service` (ClusterIP :80→:8000), test용 `jw-market-backend-api-test`(+service) | BASELINE §인프라 |

**미들웨어·구성**(`pipeline/scripts/api/main.py`):
- CORS: `localhost:8013`/`8888` 허용, methods `GET/POST/OPTIONS`.
- GZip: 1KB 이상 압축(compresslevel 1).
- `root_path = EXTERNAL_PATH_PREFIX`(`/jw-market-backend-api`) — FastAPI 문서/프록시용 프리픽스이며, 실경로는 프리픽스 없이 `/api/...`로도 유효하다(evidence/api_captures.md 주석).
- `/` 및 `/static`은 `docs/reference`의 목업 HTML(`jw_market_hardcoded_mockup_v3_4.html`)을 서빙(이미지 안에서는 `/app/static`).

**엔드포인트 인벤토리**(라우터 등록 순: `main.py:80-88`, 실호출 검증 evidence/api_captures.md):

| 라우터 파일 | 메서드·경로 | 용도 |
|---|---|---|
| `routes/health.py` | `GET /api/health` | 상태·로드 브랜드/시장 수·버전 |
| `routes/brands.py` | `GET /api/brands` | 브랜드 목록/검색(`?q=`) |
| `routes/market_status.py` | `GET /api/market-status` | 브랜드 카드(front/back/extended) |
| `routes/cause.py` | `GET /api/cause/{brand}` | 원인 분석(view=market_landscape·competitive_dynamics) |
| `routes/deep_analysis.py` | `GET /api/deep-analysis/{brand}` | 심층분석(view=general·view_kind=strategic_ml) |
| `routes/dynamic_market.py` | `POST /api/dynamic-market`, `GET /api/dynamic-market/filter-options`, `GET .../brand-option-check` | 동적 시장 조회·필터 옵션 |
| `routes/market_filter.py` | `GET /api/market-filter/atc-options` | ATC 드롭다운 옵션 |
| `routes/market_scope.py` | `GET /api/market-scope/options`, `POST .../resolve`, `POST .../cause` | 시장 범위 해석(`include_in_schema=False`) |
| `routes/brand_activity.py` | `GET /api/brand-activity/topics`, `GET .../topics/{scope_id}`, `GET .../csd-presence`, `POST .../topics`, `POST .../csd-timeseries`, `POST .../csd-activity-series`, `POST .../interest-rx-matrix` | 브랜드 활동(토픽·CSD·관심도) |

**데이터 접근**(`pipeline/scripts/api/config.py`): DB 접속은 env 우선·로컬 폴백 구조. 운영 env(evidence/backend_deploy_env.txt)는 전 차원(`DB_NAME`/`BRIDGE_DB_NAME`/`GENERAL_DIMENSION_DB_NAME`/`STRATEGIC_DIMENSION_DB_NAME`/`AGENT3_DB`)이 모두 `jw_mart_d2_stage_20260630_r2`를 가리키고, 브랜드활동만 `jw_brand_activity_stage`(config 기본값)다. `DYNAMIC_MAX_BRAND_ROWS=3000` 게이트가 동적 시장 조회 행수를 제한한다.

### 2.2 동적 시장 캐시 워머 (CronJob)

| 항목 | 값 | 근거 |
|---|---|---|
| 이름 | `dynamic-market-cache-warm`(+`-test2`) | evidence/k8s_cron_svc.txt |
| 스케줄 | `7,37 * * * *` (매시 07·37분) | `deploy/k8s/jw-market/dynamic-market-cache-warm-prod-cronjob.yaml` |
| 실행 | `python -m pipeline.scripts.api.dynamic_market.cache_maintenance` → `... warm_cache` | 동 manifest |
| 이미지 | manifest placeholder `JW_MARKET_API_IMAGE`(배포 시 envsubst 치환) → 치환 실이미지 = `jw-market-backend-api@sha256:8e2501cd…`(2026-07-18 in-mesh kubectl 실측; backend API 이미지 계열, cronjob이 `cache_maintenance` 모듈 실행). 단 live backend deploy(`@sha256:aec14a90…`)와 digest 드리프트 상태 | 동 manifest · `evidence/openq_resolution_20260718.md` Q-7 |
| DB | `galera-mariadb-galera` secret 사용, `jw_mart_d2_stage_20260630_r2` | 동 manifest |

역할: 동적 시장 조회의 무거운 집계를 미리 계산해 `dynamic_market_*` 캐시에 적재(cold latency 완화). SUSPEND=False로 상시 가동.

### 2.3 파이프라인 오케스트레이터 — 월간 데이터 체인

| 항목 | 값 | 근거 |
|---|---|---|
| 역할 | mart 갱신 후 6단계 체인(cache→forecast→strength→shortlong→events→elements)을 subprocess로 순차 호출 | `RUNBOOK_MONTHLY.md` §0 |
| 진입점 | `python -m pipeline.orchestrator run --mode full` | RUNBOOK §1 |
| repo 위치 | `pipeline/orchestrator/` (cli·executor·planner·stages·state·probe) | — |
| 배포 형태 | CronJob `jw-pipeline-orchestrator-poll-daily` (매일 01:00 KST=`0 16 * * *`), **SUSPEND=True**, state PVC 1Gi | `deploy/k8s/orchestrator/pipeline-orchestrator-poll-cronjob.yaml` |
| 이미지 | `jw-pipeline-orchestrator@sha256:6bffbc53...` (digest pin) | 동 manifest |
| 리소스 | req cpu 1/mem 2Gi, limit cpu 2/mem 6Gi | 동 manifest |

**단계별 정본 빌더**(RUNBOOK §0 표):

| 단계 | 빌더 | 산출 | 증분 |
|---|---|---|---|
| cache | `pipeline/scripts/etl/build_cache_deep_analysis_general.py` | cache_deep_analysis_general | 신규 브랜드 스코프 |
| forecast | `pipeline/scripts/etl/ops_forecast_builder.py` | deep_forecast_block/horizon | mart epoch 단위 |
| strength | `pipeline/scripts/agent3/run_source.py` | agent3_brand_strength_source | input_hash(신규/변경만 LLM) |
| shortlong | `pipeline/scripts/ai_analysis/agent2_regen_orchestrator.py` (904줄판이 정본) | staging | hash 무변경 skip |
| events | `pipeline/scripts/etl/cache_refresh/cache_deep_analysis_events_update.py` | cache_deep_analysis(events) | full-only(원자 스왑) |
| elements | `pipeline/scripts/etl/cache_brand_elements.py` | cache_brand_elements | 신규 브랜드 스코프 |

**상태·멱등성**: 체크포인트는 mart epoch(`ops_forecast_store.mart_source_epoch` = 6개 mart 테이블 지문) 키의 JSON 파일. 같은 epoch 재실행 = no-op. Fail-closed 이중기입 pin(manifest args): `MARIADB_DATABASE`·`AGENT3_DB_NAME`이 `jw_mart_d2_stage_20260630_r2`와 일치하지 않거나 `AGENT3_WORKFLOW_REV`/`AGENT3_EXPECTED_WORKFLOW_REV`(=`5692`)가 비면 즉시 abort(RUNBOOK §1.3, manifest `args`).

**트리거(이벤트 드리븐, 과도기)**: 1차 트리거는 ETL 증분 적재 성공 kick(`pipeline/etl/kick.py`, `JW_ETL_KICK_ORCHESTRATOR=1`일 때만), 안전망은 위 daily poll(RUNBOOK §0-a). 다만 이 3종 트리거(센서·kick·poll)는 인입 훅 시스템 착지 시 대체·삭제 예정이며 현재는 전부 SUSPEND로 과도기 예비 상태다.

### 2.4 Agent3 — 브랜드 강도(strength) 갱신

| 항목 | 값 | 근거 |
|---|---|---|
| 배포 형태 | CronJob `jw-agent3-refresh-daily`, `0 21 * * *`(06:00 KST), **SUSPEND=False** | `deploy/k8s/agent3/agent3-refresh-cronjob.yaml`, evidence/k8s_cron_svc.txt |
| 이미지 | `jw-market-backend-api@sha256:1824db4d...` (백엔드 이미지 재사용) | 동 manifest |
| Job manifest | `agent3-full-job.yaml`, `agent3-market-full-job.yaml`(전량/시장 단위) | `deploy/k8s/agent3/` |
| 비용 | 전량 24,789 브랜드 ≈ ₩7,300 (무변경 재실행 calls=0 → ₩0) | RUNBOOK §6 |

LLM 멱등성: rev·hash 변경 시에만 워크플로 호출(RUNBOOK §1.3). rev는 manifest env로만 주입되고 코드 기본값이 없어, env 부재 시 즉사가 정상 동작이다(RUNBOOK §7.3).

### 2.5 크롤 tier1/tier2 — 뉴스 인입·스코어링

| 항목 | 값 | 근거 |
|---|---|---|
| tier1 CronJob | `jw-news-crawl-tier1-daily`, `10 18 * * *`, SUSPEND=False | evidence/k8s_cron_svc.txt |
| tier2 CronJob | `jw-news-crawl-tier2-daily-slice`, `40 18 * * *`, SUSPEND=False (brand_key 해시 7분할 요일 로테이션) | evidence/k8s_cron_svc.txt, RUNBOOK §0-a |
| canonical(강등) | `*-daily-canonical`, `*-daily-slice-canonical` 각각 SUSPEND=True — cutover 완료로 강등(demoted)·삭제 후보 | evidence/k8s_cron_svc.txt, 상류 `30763e9c` |
| 이미지(라이브) | `jw-market-crawl@sha256:64bb2b9f...` — 라이브 CronJob과 repo manifest 동일 digest 실측 | evidence/k8s_ingest_active.txt, `deploy/k8s/crawler/crawl-tier{1,2}-cronjob.yaml` |
| repo 위치 | `crawl/` (tier2 prompts·ops), 빌드 소스는 `pipeline/scripts/crawler/*`·`agent_2/*` | `deploy/docker/crawl.Dockerfile` |

**tier2 잡 파이프라인**(canonical manifest `args`): ① news_raw URL 프리시드 → ② `crawl/crawler/crawl_2tier.py`(tier2 크롤·스코어) → ③ 중복 게이트(batch 내 중복 abort + news_raw 기존 news_id 차집합) → ④ `corpus_loader_v2.py`로 적재 → ⑤ `/opt/tier2/tier2_full_scoring_runner.py sync-events-raw` + `append-live`(대상 `tier2_llm_v2_rev5671`, wf337 호출, 일 60콜/₩203.40 상한).

**tier2 cutover 유의점**(RUNBOOK §5): canonical CronJob은 ConfigMap `tier2-llm-runner-rev5671`을 마운트하는데, 이 CM이 이미지 내 runner(`/opt/tier2`)를 가린다. canonical 이미지로 전환하려면 CM 마운트 제거가 필수다(그렇지 않으면 구판 CM이 계속 실행). cutover 자체는 PL 판단 범위.

**cutover 진행 상태(2026-07-17 실측)**: cutover는 **실행 완료**다. 라이브 CronJob(`jw-news-crawl-tier1-daily`·`jw-news-crawl-tier2-daily-slice`)의 이미지 digest가 repo manifest(`deploy/k8s/crawler/crawl-tier{1,2}-cronjob.yaml`)와 동일한 `jw-market-crawl@sha256:64bb2b9f...`로 실측 일치하고 둘 다 `suspend: false`다(evidence/k8s_ingest_active.txt digest 대조). 상류 `30763e9c`는 이 실행 사실을 repo에 기록하고 canonical 사본 2종을 강등(demote)한 커밋이며, canonical 2종은 `suspend: true` 유지 상태의 삭제 후보다(resume 시 이중 기동 위험 — DOC-5 §crawl 참조).

### 2.6 브랜드 활동 (topic / CSD)

| CronJob | 스케줄 | SUSPEND | 근거 |
|---|---|---|---|
| brand-activity-topic-monthly | `0 19 4 * *` | False | evidence/k8s_cron_svc.txt |
| brand-activity-row-topic-monthly | `0 22 4 * *` | False | evidence/k8s_cron_svc.txt |
| jw-csd-sensor | `*/10 * * * *` | True | evidence/k8s_cron_svc.txt |
| jw-brand-activity-run | `0 0 30 2 *` | True | evidence/k8s_cron_svc.txt |

DB `jw_brand_activity_stage`(7테이블)를 씀. API 서빙은 `routes/brand_activity.py`(§2.1). CSD 센서는 MinIO CSD 파일 도착 감지형이나 현재 SUSPEND(RUNBOOK §0-a).

### 2.7 jw-data-portal — 데이터 입력 사이트

| 항목 | 값 | 근거 |
|---|---|---|
| 역할 | UBIST/IQVIA 등 원천 파일 업로드·제출·대시보드 | 사이트 repo `web/src/app/(portal)/` |
| 기술 스택 | Next.js 14.2.35 · React 18 · next-auth 4 · Tailwind 3 · AWS SDK S3(MinIO)·GCS·Firestore · exceljs·adm-zip·uppy | `web/package.json` |
| 배포 형태 | Deployment `jw-data-portal` + worker `jw-data-portal-worker`, svc `jw-data-portal-service`(:80); 현재 imageID는 live query | `kubectl -n llmops get deploy,pod -o json` |
| 라우팅 | Istio VirtualService `jw-data-portal-virtualservice`, `llmops-gateway`, prefix `/jw-data-portal/`, timeout 120s | `web/k8s-manifests/jw-data-portal-vs.yaml` |
| 컨테이너 | node:20-bookworm-slim, 멀티스테이지(builder→runner), `next build`+`build:worker`, `PORT=8080` | `web/Dockerfile` |

**현행 운영 인입 방식(R&D 등 일반 업로드)**: `STORAGE_PROVIDER=local`, `UPLOAD_BASE_PATH=/nfs-root/autoIngestion`(유지). 업로드 파일을 NFS에 두고 worker(`upload-worker`, poll 30s/lease 600s)가 처리. Weaviate dedup 활성(`WEAVIATE_DEDUP_ENABLED=true`).

**시장 인입 방식**: MinIO S3 서명 URL 업로드 → manifest(계약 v2, `web/src/lib/market-ingestion.ts`) PUT → webhook(`ingest-hook-client.ts`가 `INGEST_HOOK_TRIGGER_URL`로 POST). `MINIO_*`·`INGEST_HOOK_*` 키는 Deployment의 secretRef 존재 여부를 live query로 확인한다. 일반 업로드와 시장 인입의 실제 storage provider도 현재 env/config를 함께 확인한다.

**사이트 API 라우트**(`web/src/app/api/`): `market/submissions`(목록/`confirm`/`retry`), `upload`(`signed-url`/`complete`/`jobs`), `uploads/stats`, `files/[...path]`, `categories`, `admin/*`, `auth/[...nextauth]`.

### 2.8 인입 훅(ingest_hook) — webhook → G3 → incremental Job (클러스터 기동·리허설 격리 모드)

| 항목 | 값 | 근거 |
|---|---|---|
| 역할 | 사이트 "제출 확정" webhook 수신 → 구조검증(G3) → incremental 적재 Job 실행 | `pipeline/scripts/ingest_hook/README.md` |
| repo 위치 | `pipeline/scripts/ingest_hook/` (app·contract·g3·ledger·job_launcher·job_runner·sigma_gate·sweep·**s3_input·sigma_market**) | — |
| 엔드포인트 | `POST /ingest/webhook`, `GET /ingest/status`, `POST /ingest/reconcile` | ingest_hook README §구성 |
| 배포 형태(클러스터 실측) | Deployment `jw-ingest-hook` **1/1**, svc `jw-ingest-hook`(:8080), CronJob `jw-ingest-sweep-daily`(`30 19 * * *`=04:30 KST, **SUSPEND=False**) | evidence/k8s_ingest_active.txt |
| 이미지 | `jw-pipeline-orchestrator@sha256:fea29685...` (orchestrator 이미지 재사용, 신규 빌드 없음) | evidence/k8s_ingest_active.txt |
| 계약 | `contract.py`(manifest v2.1: 주간 epoch·uploaded_by), `ledger.py`(ingest_ledger) — **운영 DB에 `ingest_ledger` 생성됨(행수 3, AUTO_INCREMENT=8)** | README, BASELINE §09:54 |

> repo manifest(`deploy/k8s/ingest-hook/ingest-trigger-deployment.yaml`)의 기본값은 `replicas: 0`, sweep은 `suspend: true`로 "등록 후 미기동" 상태이나, **라이브 클러스터에는 PL 게이트를 통과해 활성 오버라이드가 적용**되어 Deployment 1/1·sweep active로 기동 중이다(evidence/k8s_ingest_active.txt). 이미지도 repo pin(`6bffbc53`)보다 최신인 `fea29685`가 실행된다.

**★ 현재 동작 모드 = 리허설 격리(운영 무접촉 E2E)**: 라이브 트리거 서비스 env에 `INGEST_REHEARSAL_ROOT=/tmp/ingest-rehearsal`이 설정되어 있다(evidence/k8s_ingest_active.txt). `config.py` 계약상 이 변수가 설정되면 `job_runner`가 격리 모드로 동작해 orchestrator를 호출하지 않고 sqlite staging에서 G3→적재→Σ게이트 순서만 검증한다(RUNBOOK 격리 리허설 절차). 즉 훅은 배선·기동은 되었으나 실 mart를 변경하지 않는 리허설 단계다. 실적재 전환(REHEARSAL_ROOT 해제)은 남은 PL 게이트다.

**관련 코드(원격 `develop` live HEAD)**:
- `s3_input.py` — MinIO 제출 세트를 파일시스템 마운트가 아닌 **S3 API로 읽는 stdlib-only(SigV4) 리더**(GET/LIST, path-style). 의존성 프리(오케스트레이터 이미지에 boto3 없음). write 연산 부재 = 제출 버킷 불변. `INGEST_S3_BUCKET` 설정 시 활성, 미설정 시 로컬 루트 폴백(`config.open_input_source()`).
- `sigma_market.py` — 실적재용 Σ(부분)=전체 핀: `Σ mart_general_brand_metric.metric_history[period].raw_value == mart_general_market_metric.market_size_series[period]`를 (source, atc4_code, period) 단위로 대사. 2026-07-17 라이브 대사 결과 ubist 364/364·iqvia_nsa 538/538·worst rel 0.000000%(코드 docstring). 로드한 기간을 시장이 하나도 안 담고 있으면 실패로 처리(skip 아님).

### 2.9 Gitea — 코드 정본 / 백업

| 항목 | 값 | 근거 |
|---|---|---|
| 배포 | `llmops-gitea-deployment`, svc `llmops-gitea-service`(:3000/:22), `gitea/gitea:1.25.2` | evidence/k8s_llmops.txt, BASELINE |
| org/repo | `jw-market` org: `jw-data-input.git`, `jw-market.git` | BASELINE §인프라 |
| 백업 | CronJob `jw-gitea-dump-daily`, `40 19 * * *`, SUSPEND=False | evidence/k8s_cron_svc.txt |

---

## 3. 인프라

| 리소스 | 실명 | 근거 |
|---|---|---|
| 클러스터 | GKE, ns `llmops`(우리 소관 전부) / `portal`(SI) / cicd 등 | BASELINE, evidence/k8s_*.txt |
| MariaDB | Galera StatefulSet `galera-mariadb-galera`(3/3), svc `llmops-mariadb-service`·`galera-mariadb-galera`(:3306) | BASELINE §인프라 |
| DB(운영) | `jw_mart_d2_stage_20260630_r2`(전 차원), `jw_brand_activity_stage`(브랜드활동) | evidence/backend_deploy_env.txt |
| DB user | `llmops` (비밀번호는 secret `galera-mariadb-galera`, mart writer는 secret `jw-mart-d2-writer`) | evidence, orchestrator manifest |
| MinIO | svc `minio`(:9000)/`minio-console`(:9090), ExternalName `llmops-minio-service` | BASELINE §인프라 |
| NFS | `/nfs-root/autoIngestion`(업로드), `nfs-client-provisioner` | evidence/dataportal_env.txt, k8s_llmops.txt |
| Weaviate | svc `llmops-weaviate-service:8080` (dedup) | evidence/dataportal_env.txt |
| Gitea | `llmops-gitea-service`(:3000/:22) | BASELINE |
| 인입 훅 | Deployment `jw-ingest-hook`(1/1), svc `jw-ingest-hook`(:8080), CronJob `jw-ingest-sweep-daily`(active) | evidence/k8s_ingest_active.txt |
| 레지스트리(AR) | `asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01` | BASELINE §인프라 |
| nodeSelector | backend: `knp-jw-agn-dev-genos-api-01`; 크롤: `genos: enabled` | evidence, crawl manifest |

**DB 스키마 규모**(evidence/db_schema_dump.txt): `jw_mart_d2_stage_20260630_r2` 91테이블 + `jw_brand_activity_stage` 7테이블(SHOW CREATE 전수 + 행수). ground truth 테이블은 `catalog_ml_market`·`catalog_cd_market`·`catalog_strategic_brand`. `_bak_*`/`_backup_*`/`_stage_*`/`_old_*` 등 접미사 테이블은 백업/작업용(정본 아님)이며, `cache_cause`·`cache_deep_analysis` 계열은 "제거 예정" 대상으로 분류된다(BASELINE §DB 스키마). 인입 훅 활성화에 맞춰 **`ingest_ledger` 테이블이 운영 DB에 생성**되었다(행수 3, AUTO_INCREMENT=8; 초기 문서의 "미생성" 서술은 폐기, BASELINE §09:54).

---

## 4. 코드 구조

### 4.1 백엔드/파이프라인 repo (GitHub 원격 `develop`)

```
api/Dockerfile                     # 백엔드 API 이미지(보호 blob — 수정 금지, 계약 테스트 pin)
RUNBOOK_MONTHLY.md                 # 월간 파이프라인 실행 정본
BRANCH_POLICY.md                   # 머지 금지 브랜치·정본 지정
chat/                              # jw-chat-agent-poc, wf301-vdb-bridge(별도 서비스)
crawl/                             # 크롤 런타임 레이아웃(tier2 prompts, ops 백필 스크립트)
data/                              # JW 주요 약품 수동 매핑
deploy/
  docker/                          # crawl.Dockerfile, pipeline-orchestrator.Dockerfile
  k8s/
    agent3/  brand-activity/  brand-elements/  cache-refresh/
    crawler/  ingest-hook/  iqvia-sidecar/  jw-market/  orchestrator/
docs/
  reference/                       # 서빙 목업 HTML(static) — static 노출 금지 경로
  crawl/  design/  runbooks/  research/  delivery/(본 문서)
pipeline/
  etl/
    config/  engine/  lib/  stages/
    io/
      cache/  catalog/  enrich/  mart/(general_*·strategic_*·molecule_*·momentum 등 40+ 모듈)
      db.py  manifest.py  iqvia_loader.py  ubist_loader.py
    kick.py  run.py  entrypoint.sh
  orchestrator/                    # cli·executor·planner·stages·state·probe
  scripts/
    api/                           # ★서빙 API (routes·handlers·composers·dynamic_market·models·validators)
    agent3/  agent_2/  ai_analysis/  analysis/  crawler/  forecast/
    ingest_hook/                   # 인입 훅(클러스터 기동·리허설 모드). app·contract·g3·
                                   #   ledger·job_launcher·job_runner·sigma_gate·sweep
                                   #   ·s3_input(MinIO SigV4 리더)·sigma_market(Σ부분=전체 핀)
    etl/  gates/  serving/  deploy/  utils/  news_cutover/
tests/                             # agent3·api·crawler·etl·forecast·gates·ingest_hook·orchestrator·serving
```

이미지별 COPY 범위:
- `api/Dockerfile`: `pipeline/scripts/{api,agent3,ai_analysis/bundle_builder,analysis,deploy,etl,forecast,utils}` + `pipeline/etl` + `docs/reference`(→`/app/static`). 명시 COPY 리스트(보호 blob).
- `deploy/docker/pipeline-orchestrator.Dockerfile`: pipeline 패키지 전체 + docs/crawl. AGENT3_WORKFLOW_REV baked 없음(fail-closed).
- `deploy/docker/crawl.Dockerfile`: `crawl/crawler`←`scripts/crawler`, `crawl/agent1`←`scripts/agent_2`, `/opt/tier2`←`scripts/crawler/tier2_*` 재조립.

### 4.2 사이트 repo (Gitea 원격 활성 브랜치의 `web/`)

```
web/
  Dockerfile  cloudbuild.yaml  deploy.sh  next.config.mjs  tailwind.config.ts
  package.json                       # name=web, version 0.4.2
  k8s-manifests/                     # jw-data-portal.yaml, -worker.yaml, -vs.yaml
  scripts/                           # migrate-to-firestore.ts, upload-worker.ts
  src/
    middleware.ts
    app/
      (portal)/                      # admin/  dashboard/(jobs/[jobId])  market/  rnd/
      api/                           # market/submissions/{confirm,retry}, upload/{signed-url,complete,jobs},
                                     #   uploads/stats, files/[...path], categories, admin/*, auth/[...nextauth]
      login/  unauthorized/  error/
    components/  config/  types/
    lib/                             # market-ingestion.ts, ingest-hook-client.ts, storage.ts,
                                     #   upload-*.ts, weaviate-dedup.ts, genos-client.ts, auth-*.ts 등
```

---

## 5. 빌드·배포 절차

### 5.1 공통 원칙(RUNBOOK §5)

- 빌드는 **GCP ops VM에서 `docker build --platform linux/amd64`**, push는 AR `asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/`.
- 백엔드 이미지의 `api/Dockerfile`은 보호 blob(수정 금지, `tests/test_crawl_shortlong_extraction_contract.py`가 sha256 pin). 신규 코드는 COPY 대상인 `pipeline/scripts/*` 하위에 두어야 이미지에 포함된다.
- 크롤 이미지 빌드 커밋은 `stage3_genos.md`에 기록(과거 이미지 커밋 불명 재발 방지).

### 5.2 백엔드 API 배포 흐름

현행 절차는 amd64 이미지 1회 빌드·AR push → immutable digest 확인 → test2 검증 → 같은 digest를 운영에 승격 → 전체 pod imageID·strict log 검증이다. 명령, CAS 좌표 보존, 이미지 롤백과 데이터 롤백의 구분은 [배포·승격·롤백 런북](RUNBOOK_배포_승격_롤백.md)을 따른다.

### 5.3 mart DB 세대 교체(RUNBOOK §4)

1. `pipeline/scripts/utils/mart_config.py`의 `DEFAULT_MART_DB_NAME` 수정(유일한 Python 정의처).
2. `pytest tests/deploy/test_mart_db_single_source.py` → 남은 고정 사본이 파일:줄로 전부 열거됨. 같은 커밋에서 일괄 갱신.
3. manifest의 `test "$VAR" = "..."` guard는 의도된 fail-closed 이중기입(삭제 금지).

### 5.4 사이트(jw-data-portal) 배포

- `web/deploy.sh`: `docker build --platform linux/amd64` → AR push → `kubectl apply -f k8s-manifests/{jw-data-portal,jw-data-portal-worker,jw-data-portal-vs}.yaml`. AR는 stg 프로젝트(`prj-jw-agn-stg-ai`), 배포는 dev 클러스터(`kcl-jw-agn-dev-genos`)로 권한 분리. 컨텍스트 불일치 시 확인 프롬프트.
- `web/cloudbuild.yaml`: Cloud Build로 `$SHORT_SHA`·`_VERSION` 두 태그 빌드·push.
- 사이트 배포 스크립트는 별도 Gitea `jw-data-input` 저장소 소관이다. README나 스크립트 기본 tag를 현재 운영값으로 간주하지 말고 `kubectl -n llmops get deploy jw-data-portal jw-data-portal-worker -o json`의 image와 각 pod `imageID`를 대조한다.

### 5.5 머지 금지(BRANCH_POLICY.md, RUNBOOK §7)

- `codex/crawl-2tier`·`codex/short-long-lineage-bulk` develop 머지 금지(rev 5365 복원·게이트 제거).
- `3f0db0ae` 계보 머지 금지(구 이벤트 컷오프 43/49/51/54/55).
- 크롤 이미지를 역사 브랜치에서 빌드 금지.
- Agent2 정본은 `pipeline/scripts/ai_analysis/agent2_regen_orchestrator.py`(904줄판); `ops/` 스냅샷은 폐기 세대(provenance 보존용).

---

## 확인 결과 · 잔여 항목

2026-07-18 jw market 실측(근거: `evidence/openq_resolution_20260718.md`). ✅=해소, ⏳=PL/데이터 대기(→ [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md)).

1. 운영 좌표는 문서의 과거 캡처가 아니라 live query로 확인한다.
2. backend와 `dynamic-market-cache-warm`의 imageID가 다르면 캐시 계산 경로 drift로 분류해 별도 게이트한다.
3. 사이트 코드는 Gitea 원격 HEAD, 실행체는 Deployment/pod imageID로 각각 확인한다.
4. 비용·실적재 전환처럼 승인에 의존하는 항목은 [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md)에 남기고 문서가 임의 확정하지 않는다.
