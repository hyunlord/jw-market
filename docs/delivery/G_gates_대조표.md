# G 게이트 대조표 — 문서·실체 정합 검증 (G-1 ~ G-5)

| 항목 | 값 |
|---|---|
| 기준 develop SHA | `761b4def` → **`7ca98403`** (재실측 라운드 전진; 워크트리 HEAD `24f14d0e`=문서 산출 커밋) |
| 사이트 정본 SHA | `8ca9d987` (feat/market-ingest-v21, `/tmp/site-head`) · 배포 이미지 `v0.6.0-8ca9d98` |
| 검증일 | 2026-07-17 (G-3 재판정: `k8s_hpa.txt`) → **재실측 라운드(09:54~) 재검증 반영** (§재실측 라운드) |
| 근거 디렉토리 | `scratchpad/evidence/` (BASELINE.md, db_schema_dump.txt, api_endpoints.md, api_captures.md, k8s_*.txt, backend_deploy_env.txt, dataportal_env.txt, site_repo.txt) |
| 검증 대상 | `docs/delivery/DOC-1 ~ DOC-5` (6파일: 4a/4b 포함) |
| 검증 방식 | 적대적 전수 대조(샘플링은 명시 항목만). 문서 무수정 원칙(불일치는 보고만). |

> **push 시점 각주.** develop은 본 대조표 확정 직후 `0a8e9080`까지 10커밋 추가 전진했다(전부 dynamic-market 지연 최적화 — 커밋 메시지 기준 응답 계약 불변의 성능 리팩터 + 테스트, `pipeline/scripts/api/` 내부). 본 문서 세트의 검증 기준은 `7ca98403` 스냅샷이며, API 계약·캡처는 해당 시점 실측이다. 이후 전진분은 검증 범위 밖이다.

## 종합 판정

| 게이트 | 대상 | 검증 항목 수 | 불일치 | 판정 |
|---|---|---|---|---|
| G-1 | API 명세 (DOC-3) | 20 라우트 + 3 mock 별칭 + 정적마운트 + 4 ingest EP + 14 라인인용 + 5 응답표본 | 0 | **PASS** |
| G-2 | DB 스키마 (DOC-2) | 43 오브젝트(42 테이블+1 뷰), 41 컬럼표·컬럼명 전수·타입 전수 | 0 | **PASS** |
| G-3 | 아키텍처 (DOC-1·DOC-5) | 16 CronJob·15+ 리소스명·gen·APP_VERSION·HPA·replica | 0 | **PASS** |
| G-4 | 사용설명서 (DOC-4a·4b) | 10 file:line 표본 + 활성/적용예정 구분 | 0 | **PASS** |
| G-5 | 문서 머리 (6파일) | SHA·gen·생성일·버전 | 2 (도메인상 생략) | **PASS(주석)** |

---

## G-1 · API 명세 대조 (DOC-3 ↔ 실코드) — PASS

**방법론.** `grep -rnE '@router\.(get|post)\(' routes/*.py` + 멀티라인 데코레이터에서 경로 문자열 추출(`sed` 블록 스캔) → DOC-3 §3 전 EP와 1:1 대조. `main.py:80-102`(라우터 등록·serve_frontend·정적마운트), `ingest_hook/app.py`(별도 서비스 4 EP) 포함. 응답 예시는 `api_captures.md`에서 표본 5건을 값 grep으로 역추적.

### (a) 실코드 → 문서 (누락 0)
코드에 존재하는 전 라우트가 DOC-3에 문서화됨:

| 실코드 경로 | 정의 | DOC-3 |
|---|---|---|
| `GET /` (+prefix 2종) | main.py:95,100-102 | §3.1 ✓ |
| `GET /api/health` | health.py:13 | §3.2 ✓ |
| `GET /api/market-status` | market_status.py:13 | §3.3 ✓ |
| `GET /api/brands` | brands.py:130 | §3.4 ✓ |
| `GET /api/cause/{brand_name}` | cause.py:120 | §3.5 ✓ |
| `GET /api/deep-analysis/{brand_name}` | deep_analysis.py:1367 | §3.6 ✓ |
| `POST /api/dynamic-market` | dynamic_market.py:64 | §3.7 ✓ |
| `GET /api/dynamic-market/filter-options` | dynamic_market.py:412 | §3.8 ✓ |
| `GET /api/dynamic-market/brand-option-check` | dynamic_market.py:475 | §3.9 ✓ |
| `GET /api/market-filter/atc-options` | market_filter.py:16 | §3.10 ✓ |
| `GET /api/market-scope/options` | market_scope.py:34 | §3.11 ✓ |
| `POST /api/market-scope/resolve` | market_scope.py:56 | §3.12 ✓ |
| `POST /api/market-scope/cause` | market_scope.py:69 | §3.13 ✓ |
| `GET /api/brand-activity/csd-presence` | brand_activity.py:69 | §3.14 ✓ |
| `GET /api/brand-activity/topics` | brand_activity.py:96 | §3.15 ✓ |
| `GET /api/brand-activity/topics/{scope_id}` | brand_activity.py:105 | §3.16 ✓ |
| `POST /api/brand-activity/topics` (+mock 117) | brand_activity.py:121 | §3.17/17b ✓ |
| `POST /api/brand-activity/csd-timeseries` (+mock 175) | brand_activity.py:179 | §3.18/18b ✓ |
| `POST /api/brand-activity/csd-activity-series` | brand_activity.py:236 | §3.19 ✓ |
| `POST /api/brand-activity/interest-rx-matrix` (+mock 274) | brand_activity.py:278 | §3.20/20b ✓ |
| 정적마운트 `/static` (+prefix) | main.py:90-92 | §3.21 ✓ |
| ingest_hook 4 EP (`/healthz`, `/ingest/webhook`, `/ingest/status`, `/ingest/reconcile`) | app.py:89,93,97,114 | §5 ✓ |

### (b) 문서 → 실코드 (유령 0)
DOC-3의 전 EP가 실코드에 실재. mock 별칭 3종(117/175/274)·정적마운트도 코드 확인.

### (c) 라인 인용 정확도 (표본 14건, 오차 0)
`VALID_VIEWS`(query_params.py:6), `_normalize_deep_view`(deep_analysis.py:1340), view_kind Literal(deep_analysis.py:1391), DynamicMarketRequest `extra="forbid"`(dynamic_market.py:232/237), MarketFilterView Literal(market_filter.py:8), MarketFilter/BrandActivityTopicsRequest(brand_activity.py:163/213/264), `_view_family`/`_reject_general`(market_scope.py:141-157), CsdActivitySeriesRequest(brand_activity_csd_activity_contract.py:58, `extra="ignore"`), CONTRACT_VERSION="v2"(contract.py:22) — 전부 실측 일치. (파일 위치 주의: `brand_activity_csd_activity_contract.py`는 `models/` 아래가 아니라 `api/` 루트에 있으며 DOC-3의 bare 경로 표기가 정확.)

### (d) 응답 예시 ↔ 실캡처 (표본 5/5 일치)
| 표본 | DOC-3 값 | api_captures.md |
|---|---|---|
| health | markets_loaded 25, version ad782bc0…, 107B | L31,33 ✓ |
| brands 검색(리바로) | 479B, x-total-matches 1 | L187-188 ✓ |
| cause ML | 2,256,903B, market_yoy_recent_pct 4.86 | L221,337 ✓ |
| market-scope/options | 1,086B, group:livalo_family, GROUP_01_20260716 | L2007-2048 ✓ |
| brand-activity/topics POST | 1,936B, etc_pct 100.0, no_topic_scope | L3491-3548 ✓ |

---

## G-2 · DB 스키마 대조 (DOC-2 ↔ db_schema_dump.txt) — PASS

**방법론.** Python 스크립트(`scratchpad/g2_compare.py`, `g2_types.py`)로 자동 대조.
- 덤프: 정규식으로 `CREATE TABLE ... ENGINE` 블록 파싱 → 테이블별 컬럼명·타입·NULL 추출(97 블록).
- 문서: `#### <table>` 헤딩 + `| 컬럼 |` 표 파싱. DOC-2 압축 표기 확장 처리 — 범위(`target_iqvia_1..3`)·슬래시목록(`a / b / c`). 슬래시목록은 "전체명 나열"과 "접두 stem 공유" 두 해석이 공존(예: `analyze_class / molecule / …`=접두공유, `nhi_type / ox_gx / fish_oil`=전체명)하므로 후보집합 방식으로 양쪽 허용, 실 DB에 존재하는 후보로 판정.

| 검증 항목 | 결과 |
|---|---|
| 오브젝트 수 (mart / brand_activity) | 91 / 7 = 98 — DOC-2 §0 표와 일치 ✓ |
| 정본 42 테이블 + 1 뷰 = 43 헤딩 | 일치 ✓ |
| 컬럼표 41개 테이블 컬럼명 (전수) | MISSING 0 / PHANTOM 0 ✓ |
| 컬럼 타입 (전수) | 실질 불일치 0 ✓ |
| VIEW(row_topic_assignment_share_view) | 덤프에 SHOW CREATE TABLE이 CREATE 미반환·행수 1,639 일치. DOC-2가 "테이블 아닌 VIEW"로 정확히 명기 ✓ |
| cache_market_status (컬럼표 없이 산문) | DB 컬럼 = cache_brands와 동일(query_key/response_json/payload_size/updated_at/build_sha/input_manifest_json) — 산문 기술 정확 ✓ |

**타입 대조 주석.** 자동 대조가 11건을 플래그했으나 전부 **DOC-2가 덤프보다 더 정밀**한 경우(파서 한계)로, 실 덤프에서 정확성 확인:
- `state enum('building','ready','failed')` (L771), `derivation enum('llm_direct','cross_match','manual')` (L1106), `payload longtext … COLLATE utf8mb4_bin CHECK(json_valid)` (L1492), `char(64) ascii_bin` 등 — 문서가 enum 값 리스트·collation·json 체크까지 기재. 기저 타입 전부 일치, 문서 표기가 오히려 상세. **문서 결함 아님.**

---

## G-3 · 아키텍처 리소스명 대조 (DOC-1·DOC-5 ↔ k8s_*.txt) — PASS

**방법론.** 4개 evidence(k8s_llmops.txt, k8s_cron_svc.txt, k8s_portal_vmhome.txt, **k8s_hpa.txt**)에서 `kind/name` 토큰을 ground-truth 집합으로 수집 → DOC-1·DOC-5의 백틱 리소스명 후보를 접두 매칭으로 대조. CronJob은 SUSPEND 컬럼까지 행 단위 대조. HPA·replica는 `k8s_hpa.txt`(2026-07-17 09:49 UTC 실측) 기준.

### PASS 항목 (오기 0)
| 검증 항목 | 결과 |
|---|---|
| CronJob 16행 (이름 + SUSPEND) | DOC-5 표 ↔ 클러스터 16/16 일치. 제외한 2건(`credit-monthly-renew`, `mcp-health-checker`)은 플랫폼 잡(비-jw, 범위 밖) — 정당 ✓ |
| Deployment/Svc/STS 실명 | `jw-market-backend-api`(+service/test/reference-cycle0119), `galera-mariadb-galera`(sts), `llmops-mariadb-service`, `jw-data-portal`(+worker/service), `llmops-gitea`, `llmops-minio-service`, 레거시 `jw-market-api` — 전부 evidence 실재 ✓ |
| nodeSelector `knp-jw-agn-dev-genos-api-01`, project `prj-jw-agn-stg-ai` | evidence 일치 ✓ |
| generation 302, APP_VERSION ad782bc0…, release annotation f139… | backend_deploy_env.txt observedGeneration 302 일치 ✓ |
| HPA `jw-market-backend-api-hpa` (2~8, memory 60% 타깃), replica 8 | `k8s_hpa.txt` 실측 일치 — 아래 재판정 참조 ✓ |
| ingest hook 리소스(`jw-ingest-hook` Deploy replicas 0, `jw-ingest-sweep-daily` CronJob) | 클러스터 미배포 — DOC-1 L213이 "클러스터 어디에도 배포되어 있지 않다"로 명기, 매니페스트 선언값임을 §2.8 표에서 교차참조. 내부 일관 ✓ (경미: "배포 형태" 라벨·"현재 suspend" 문구는 매니페스트 선언 상태를 서술하는 것으로, 미배포 사실과 혼동 소지 있으나 각 절이 미활성으로 명시) |

### 재판정 — 초판 FAIL 2건 해소 (evidence `k8s_hpa.txt` 추가)

초판(evidence 3파일)에서는 HPA·replica가 근거 부재로 FAIL 처리했으나, **`k8s_hpa.txt`(2026-07-17 09:49 UTC 실측)** 추가로 두 건 모두 해소:

```
jw-market-backend-api-hpa  Deployment/jw-market-backend-api  memory: 82%/60%  MIN 2  MAX 8  REPLICAS 8  7d
jw-market-backend-api      8/8  8  8  52d
```

**[G-3-1 해소] 백엔드 replica 8 — 실측 일치.**
- DOC-1 L6 "HPA … 캡처 시점 8", DOC-5 L6 "현재 8 replicas" ↔ `k8s_hpa.txt` REPLICAS **8**, deploy **8/8**. 일치 ✓.
- 스케일아웃 타임라인: 초기 캡처 `k8s_llmops.txt`는 **5/5**였으나, HPA 타깃 대비 memory **82% > 60%**로 09:49 UTC 시점 **8**로 스케일아웃(min 2/max 8 범위 내). 5→8은 동일 배포의 시점차이며 문서의 "2~8 변동·캡처 시점 8" 서술과 정합. (BASELINE.md도 동일 갱신됨.)

**[G-3-2 해소] HPA 리소스명·파라미터 — 실측 확인.**
- DOC-1 L6 "HPA `jw-market-backend-api-hpa` 2~8, memory 60% 타깃", DOC-5 L6 "HPA min 2 / max 8" ↔ `k8s_hpa.txt`의 이름 `jw-market-backend-api-hpa`, REFERENCE `Deployment/jw-market-backend-api`, MIN 2 / MAX 8, TARGET memory 60% 전부 실측 일치 ✓.

→ **G-3 오기 0. 최종 PASS.** (초판 FAIL은 evidence 미포함 캡처 한계였고, 문서 서술 자체는 실측과 일치했음.)

---

## G-4 · 사용설명서 대조 (DOC-4a·4b ↔ 실코드) — PASS

**방법론.** DOC-4a(백엔드 `pipeline/scripts/api/`)·DOC-4b(사이트 `/tmp/site-head/web/src/`) file:line 인용 표본 10건을 `sed`로 실 라인 열람해 내용 일치 확인. DOC-4b의 [현행 활성]/[적용 예정] 구분을 실 코드·배포 env로 검증.

### 표본 10건 (전부 라인 내용 일치)
| # | 인용 | 확인 내용 |
|---|---|---|
| 4a-1 | models/dynamic_market.py:237-244 | `view` 필드(general/strategic_ml/strategic_cd) ✓ |
| 4a-2 | general_analysis_levels.py:54-70 | `GENERAL_LEVEL_SPECS`(판매사/성분/…) ✓ |
| 4a-3 | market_growth.py:18-36 | `compound_period_growth_pct(...)` ✓ |
| 4a-4 | dynamic_market/cause_ranking.py:88-91 | `select_top_competitors(... top_n=5)` ✓ |
| 4a-5 | deep_analysis_vocabulary.py:6-9 | `STRENGTH_VIEW_KIND_BY_FORMAL_VIEW` 매핑 ✓ |
| 4b-1 | app/page.tsx:37-39 | session email 미존재 시 `redirect("/login")` ✓ |
| 4b-2 | UploadPage.tsx:1085-1090 | "Step 1 · 카테고리 선택" ✓ |
| 4b-3 | config/agents.ts:143-150 | `rndCategories`(논문/pdf·zip) ✓ |
| 4b-4 | lib/market-ingestion.ts:23-27 | `EPOCH_PATTERNS`(month/quarter/week 정규식) ✓ |
| 4b-5 | lib/ingest-hook-client.ts:15-17 | `triggerIngest`, `INGEST_HOOK_TRIGGER_URL` 미설정 시 mock waiting ✓ |

### 활성/적용예정 구분 (정확)
- DOC-4b L14/L116/L165-167: R&D 업로드(NFS)=[현행 활성], 시장 MinIO 인입=[적용 예정].
- 근거 검증: `confirm/route.ts:25-27`이 storage 비-S3 시 500 "시장분석 MinIO 설정이 필요합니다." 반환(실코드 확인), `lib/storage.ts:907-912` provider = `agent…provider ?? STORAGE_PROVIDER ?? "local"`(실코드 확인), 배포 env `STORAGE_PROVIDER=local`(dataportal_env.txt). → 시장 인입 미활성 서술이 코드·env와 정합. **미구현 기능을 활성으로 서술한 사례 0.**

---

## G-5 · 문서 머리 대조 (6파일) — PASS(주석)

| 문서 | 기준 SHA 761b4def | 운영 gen 302 | 생성일 2026-07-17 | 버전 v1.0 |
|---|---|---|---|---|
| DOC-1 | ✓ | ✓ | ✓ | ✓ |
| DOC-2 | ✓ | — (DB 문서·gen 미기재) | ✓ | ✓ |
| DOC-3 | ✓ | ✓ | ✓ | ✓ |
| DOC-4a | ✓ | ✓ | ✓ | ✓ |
| DOC-4b | — (사이트 문서·SHA 8ca9d987 기재) | — (사이트 문서) | ✓ | ✓ |
| DOC-5 | ✓ | ✓ | ✓ | ✓ |

**주석.** SHA·생성일·버전은 6/6 명기. `gen 302`는 백엔드 스코프 4문서(DOC-1/3/4a/5)에 명기, DOC-2(DB 스키마)·DOC-4b(사이트)에는 부재 — 두 문서는 각자 도메인 기준(DOC-2=DB명, DOC-4b=사이트 SHA 8ca9d987 + `jw-data-portal:v0.5.2`)을 명기하므로 도메인상 정당한 생략으로 판단. 엄격 해석 시 "6문서 전부 gen 명기"는 미충족이나 하드 결함 아님.

---

## 방법론·산출물 요약

- 자동 대조 스크립트: `scratchpad/g2_compare.py`(컬럼명), `scratchpad/g2_types.py`(타입).
- API 라우트 추출: `grep -rnE '@router\.(get|post)\('` + 멀티라인 경로 스캔.
- CronJob·리소스 대조: k8s evidence kind/name 토큰 집합 vs 문서 백틱명 접두 매칭.
- 표본 인용 검증: `sed -n` 라인 열람(G-1 라인 14건, G-4 file:line 10건, 응답 5건).
- **문서 무수정 원칙 준수** — DOC-1~5는 수정하지 않았으며 불일치는 본 대조표에만 기록.

---

## 재실측 라운드 (2026-07-17 09:54~ UTC) 재검증

**배경(기준 상태 변경).** 초판 검증(SHA `761b4def`) 이후 기준이 전진했다. 근거: `BASELINE.md §09:54 갱신`, 신규 evidence `k8s_ingest_active.txt`(ingest+crawl digest), `dataportal_env_v060.txt`. 문서 5종이 이에 맞춰 갱신됨.

핵심 변화: ① 기준 SHA `761b4def`→`7ca98403`(12커밋, API·etl 무변경) ② ingest hook 활성화(deploy `jw-ingest-hook` 1/1·svc :8080·CronJob `jw-ingest-sweep-daily` suspend=False·`ingest_ledger` 3행 생성, **리허설 격리 모드**) ③ 사이트 `v0.6.0-8ca9d98` 재배포(MinIO·hook env 장착, market 인입 활성) ④ crawl cutover 실행 완료(라이브 digest `64bb2b9f`=repo manifest 일치).

**SHA 전진 무변경 실측(재검증 근거).** `git diff --stat 761b4def 7ca98403 -- pipeline/scripts/api/ pipeline/etl/` = **공집합**(변경 0). 전진분은 `pipeline/scripts/ingest_hook/`·`deploy/k8s/{crawler,ingest-hook}/`·`RUNBOOK_MONTHLY.md`·`tests/{crawler,ingest_hook}`에 한정. → 초판 G-1(API §2~4)·G-2(mart/etl 41테이블)·G-4(DOC-4a) 검증은 **그대로 유효**하며, 인입 훅·crawl·사이트 v0.6.0 관련 절만 재검증.

### 재검증 결과 (게이트별)

| 게이트 | 재검증 대상 | 결과 |
|---|---|---|
| G-1 | DOC-3 §5 ingest 절(배포 상태·실캡처·app.py 라인) | **PASS** |
| G-2 | DOC-2 §4 `ingest_ledger` 컬럼표 ↔ DDL | **PASS** |
| G-3 | ingest hook·crawl cutover 신규 리소스명 | **PASS** |
| G-4 | DOC-4b v0.6.0 활성/리허설 구분 | **PASS** |
| G-5 | 6문서 머리 SHA `7ca98403` 통일 | **PASS** |
| (e) 구서술 잔존 | "미배포·전부 미활성·적용 예정" 잔존 0 | **1건 불일치** |

**[G-1 PASS]** DOC-3 §5 → "배포됨·리허설 격리 모드"로 개정. §5.1 실캡처 `GET /healthz`→`200 {"ok":true}`, `GET /ingest/status?…probe`→`404 {"detail":"unknown submission identity"}`가 `api_captures.md`(L4277-4289) 실캡처와 일치. §5 라우트 표 app.py 라인 `99/103/107/124`가 rebase 후 실코드(`ingest_hook/app.py`)와 정확 일치(초판 89/93/97/114에서 S3 분기 추가로 시프트). webhook 오류에 "MinIO 모드 key 부재→404"(s3_input) 추가분도 코드 근거.

**[G-2 PASS]** DOC-2 §4 `ingest_ledger` 컬럼표(14컬럼+UNIQUE `uq_ledger_identity`+KEY `idx_ledger_category_status`)가 코드 정본 `ledger.py:_DDL_MYSQL`과 **컬럼명·타입·NULL·키 전수 일치**(id bigint PK AI / epoch·category varchar(32) / manifest_sha char(64) / manifest_path varchar(512) / uploaded_by varchar(128) / status varchar(16) / reason·row_counts text / job_name varchar(128) / run_id varchar(64) / received_at·started_at·finished_at datetime). §0 총계 갱신(정본 37/합계 43) 정합. *주의: 문서는 "운영 DB 재실측 DDL 기준"으로 표기하나, evidence 번들에 라이브 `SHOW CREATE ingest_ledger`는 미수록 — 테이블이 `CREATE TABLE IF NOT EXISTS _DDL_MYSQL`로 생성되었고 컬럼표가 그 코드계약과 1:1이므로 실질 일치. 완전 엄밀화 시 라이브 SHOW CREATE 캡처 첨부 권장(경미).*

**[G-3 PASS]** 신규 리소스 실명 `k8s_ingest_active.txt`와 대조 전부 일치: deploy `jw-ingest-hook`(1/1)·svc `jw-ingest-hook`(:8080)·CronJob `jw-ingest-sweep-daily`(`30 19 * * *`, suspend=False)·이미지 `jw-pipeline-orchestrator@sha256:fea29685…`. DOC-1 §2.8·§1 다이어그램·§목록(L214/248), DOC-5 §3·CronJob표 L73 모두 반영. crawl cutover: DOC-1 L171/178·DOC-5 L79/80이 라이브 digest `jw-market-crawl@sha256:64bb2b9f…`=repo manifest 일치·suspend:false·canonical 2종 강등(suspend:true, 삭제 후보)로 실측과 정합. (DOC-1 L218이 repo 기본값 `replicas:0`/`suspend:true` vs 라이브 활성 오버라이드를 명시 구분 — 정확.)

**[G-4 PASS]** DOC-4b 머리 `v0.6.0-8ca9d98`, MARKET을 **[활성 · 리허설 격리]**로 개정. 에이전트 단위 스토리지 라우팅 서술이 실코드 근거: `config/agents.ts:274` `market: { storage: { provider: "s3", bucketEnv: "MINIO_MARKET_BUCKET" } }` 실재 → `STORAGE_PROVIDER=local`이어도 시장은 S3 경로(confirm 라우트 S3 검사 통과). MinIO 4개 env·`INGEST_HOOK_TRIGGER/STATUS_URL`이 `dataportal_env_v060.txt`에 실재 → "실동작+훅 실호출" 판정 정합. 백엔드 훅 리허설 격리도 명기.

**[G-5 PASS]** 백엔드 5문서(DOC-1/2/3/4a/5) 머리 SHA `7ca98403` 통일 확인. DOC-4b는 사이트 문서로 정본 SHA `8ca9d987`+배포 `v0.6.0-8ca9d98` 명기(도메인 기준). 생성일 2026-07-17·v1.0 유지.

### 불일치 (1건 → 해소됨) — 구서술 잔존

**[재-1] DOC-5 §1 개요(L21·L23) "현재 미활성" 잔존. → 해소됨(2026-07-17): DOC-5 §1을 "배포·기동됨(리허설 격리 모드), 실적재 미전환"으로 정정 완료, §3·CronJob표와 정합 확인.**
- L21: "증분 훅(3절)이 이 사이트의 제출 확정을 받는 설계지만 **현재 미활성**."
- L23: "(B) 증분 적재 훅(3절, **미활성**). 현재 라이브로 도는 것은 (A) 및 그 부속 CronJob들뿐이다."
- **실체:** ingest hook는 배포·기동됨(deploy 1/1·sweep suspend=False, 리허설 격리 모드). DOC-5 **자기 §3**(L88-90 "배포·기동됐다")·§2-3 CronJob표(L73 sweep=라이브)·DOC-1/2/3와 **내부 모순**. §1 개요만 초판(미배포) 프레이밍이 잔존.
- → 정확 표현은 "배포·기동됨(리허설 격리 모드), 실 mart 적재는 미전환". 나머지 "예비/미활성"(suspend=True CronJob 5종)·"강등"(canonical 2종)은 실측과 일치하므로 정당.

> 조치 결과: 위 권고대로 DOC-5 §1 L21/L23 정정 완료(팀 리드 직접 수정, "배포·기동됨(리허설 격리)·실적재 미전환"). **최종: G-1~G-5 전부 PASS, 미해소 불일치 0건.**
