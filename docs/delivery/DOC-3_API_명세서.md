# DOC-3 · JW 시장분석 백엔드 API 명세서

| 항목 | 값 |
|---|---|
| 문서 버전 | v1.1 |
| 기준 코드(develop) SHA | `7ca98403` (worktree `/tmp/jwm-develop-docs`) |
| 운영 배포 | GKE `llmops` ns · deployment `jw-market-backend-api` · generation **302** |
| 실호출 캡처일 | 2026-07-17 (백엔드 API 09:17 UTC · ingest hook GET 09:59 UTC, in-mesh `kubectl exec`) |
| 생성일 | 2026-07-17 |

> **정본 선언.** 본 문서가 JW 시장분석 백엔드 API의 최신 정본 명세이며, 기존 `JW_Market_Analysis_API_Spec` 계열 문서를 **대체**한다. 본 명세의 파라미터·필드·오류 계약은 실코드와 1:1로 대조되었고, 예시 응답은 2026-07-17 운영 pod 실호출 캡처에서만 발췌했다(창작 없음).
>
> **기준 SHA 각주.** 문서 기준을 `7ca98403`(ingest hook 이미지 pin 반영본)으로 갱신했다. 단, **백엔드 API 영역(`pipeline/scripts/api/`)은 `761b4def`와 완전 무변경**이다(`git diff --stat 761b4def HEAD -- pipeline/scripts/api/` 공집합 실측). 따라서 §2~§4의 파라미터 대조와 09:17 UTC 캡처는 그대로 유효하다. 이번 개정은 §5 ingest hook 절만 갱신한다(rebase로 `ingest_hook/app.py`에 S3/MinIO 입력원 분기 추가됨 — 재확인 반영).

---

## 1. 공통 사항

### 1.1 접속 계약 (포탈 BFF 연동 관점)

- **클러스터 내부 서비스:** `jw-market-backend-api-service` (ClusterIP) — `80 → 8000` 매핑. 컨테이너 포트는 8000, uvicorn(FastAPI).
- **경로 프리픽스:** 운영 컨테이너는 `EXTERNAL_PATH_PREFIX=/jw-market-backend-api` 환경변수를 갖는다. 이 값은 FastAPI `root_path`(문서/프록시 노출용 메타)일 뿐이며, **실제 라우트 경로는 프리픽스 없이 `/api/...` 그대로 유효**하다(2026-07-17 in-mesh 실측 확인 — 모든 EP를 `http://localhost:8000/api/...`로 직접 호출해 200 수신). 포탈(BFF)이 프리픽스를 붙여 프록시하더라도 백엔드 내부 라우팅은 `/api/...` 기준이다.
  - 프리픽스가 설정된 경우에 한해 `/jw-market-backend-api` 및 `/jw-market-backend-api/`가 프론트엔드 mockup으로 추가 등록된다(`main.py:100-102`).
- **CORS:** `allow_methods=["GET","POST","OPTIONS"]`, `allow_credentials=true`, origin 허용은 로컬 개발 4개(`127.0.0.1|localhost : 8013|8888`)만 명시(`main.py:62-73`). 운영 포탈 연동은 동일 클러스터 내부 프록시 경유이므로 CORS 대상 아님.
- **압축:** 1024바이트 이상 응답에 gzip 자동 적용(`GZipMiddleware`, `main.py:74-78`). 대다수 EP 응답이 MB 단위이므로 전송은 압축된다.
- **인증:** 백엔드 라우트 자체에는 인증 미들웨어가 없다(포탈 SI 계층에서 처리). 자격값은 본 문서에 기재하지 않는다.

### 1.2 데이터 소스·지표·단위

`validators/query_params.py` 기준(cause 계열):

| source | 허용 measure | 단위 라벨(`unit_label`) |
|---|---|---|
| `UBIST` | `sales` | KRW |
| `UBIST` | `volume` | Rx |
| `IQVIA` | `sales` | KRW |
| `IQVIA` | `unit` | Unit |
| `IQVIA` | `dosage_unit` | Dosage Unit |
| `IQVIA` | `counting_unit` | Counting Unit |

source 값은 대소문자 무관(내부 `.upper()`), 공개 입력은 `ubist`/`iqvia`만 사용하고 내부 `iqvia_nsa` 값은 노출하지 않는다.

### 1.3 응답 예시 표기 규칙

각 EP의 "실응답 예시"는 캡처 파일(`api_captures.md`)의 구조 보존 요약을 그대로 인용한다. 규칙: dict는 전체 key 보존, list는 첫 원소 + `...(N개 중 1개)`, 대형 payload는 상위 구조만 발췌하고 `원본 크기`(bytes)를 병기한다. 실제 운영 응답은 발췌보다 훨씬 크다.

---

## 2. view / view_kind 값 체계

★ **중요:** view 파라미터의 허용값은 **EP마다 다르다.** 이는 코드 실측 결과이며, 전환기 계약과 신규 계약이 공존하기 때문이다. 아래는 각 EP의 실제 literal이다.

| EP | 파라미터 | 허용값(코드 실측) | 근거 |
|---|---|---|---|
| `GET /api/cause` | `view` | `market_landscape`(기본) · `competitive_dynamics` | `validators/query_params.py:6` `VALID_VIEWS` |
| `GET /api/deep-analysis` | `view`(legacy) | `general` · `strategic`(생략 시 strategic) | `routes/deep_analysis.py:1340` `_normalize_deep_view` |
| `GET /api/deep-analysis` | `view_kind`(신규) | `general` · `strategic_ml` · `strategic_cd` | `routes/deep_analysis.py:1391` `Literal[...]` |
| `POST /api/dynamic-market` | `view` | `general` · `strategic_ml` · `strategic_cd`(생략 시 filters.view_kind 추론) | `models/dynamic_market.py:237` |
| `GET /api/dynamic-market/filter-options` | `view` | `general`(기본) · `strategic` · `strategic_cd` | `routes/dynamic_market.py:425-429` |
| `GET /api/market-filter/atc-options` | `view` | `general`(기본) · `strategic` | `models/market_filter.py:8` `Literal["general","strategic"]` |
| `POST /api/brand-activity/*` | `view` | `general`(기본) · `strategic_ml` · `strategic_cd` | `models/brand_activity.py:213` |
| `GET /api/market-scope/options`, `POST resolve/cause` | `view_family` | `strategy` · `general`(→ 501 미구현) | `routes/market_scope.py:141-157` |

**의미 대응(정규화):** 개념상 뷰는 3종 — 일반뷰(general), 전략뷰-시장조망(ML), 전략뷰-경쟁구도(CD).
- 일부 EP(`filter-options`, `market-filter/atc-options`)에서 **`strategic` = 전략뷰 ML**을 뜻한다(별칭). 같은 EP들에서 CD는 `strategic_cd`.
- dynamic-market / brand-activity에서는 ML을 명시적으로 `strategic_ml`로 표기한다.
- `market-filter/atc-options`는 CD 값을 아직 받지 않고 `general`/`strategic`만 허용한다(그 외 값은 422).

**PL 원칙(브랜드 중심 시장 유도):** 전략뷰·CD 경로에서 포탈은 **브랜드명 + 뷰**만 선택하고 `market_id`는 보내지 않는다. 백엔드가 브랜드명으로 소속 시장(ml_id/cd_id)을 내부 유도한다. `filter-options`·`brand-option-check`·`brand-activity`의 `market_id`는 deprecated 호환 필드이며, `deep-analysis`/`dynamic-market`의 명시적 `market_id`도 다중 시장 소속을 콕 집을 때만 사용하는 선택 입력이다.

---

## 3. 백엔드 API 엔드포인트 (20개 라우트 + mock 별칭 + 정적 마운트)

라우터 등록: `main.py:80-88` (health, brands, market_status, cause, deep_analysis, dynamic_market, market_filter, market_scope, brand_activity). `include_in_schema=False`인 EP는 OpenAPI 문서에 비노출(내부/디버그·mock)이나 경로 자체는 유효하다.

---

### 3.1 `GET /` — 프론트엔드 mockup (내부 EP·비노출)

- **핸들러:** `serve_frontend` (`main.py:95`)
- **파라미터:** 없음
- **응답:** `FileResponse` — 하드코딩 mockup HTML `jw_market_hardcoded_mockup_v3_4.html` (`/app/static` 또는 `docs/reference`)
- **실응답:** `200`, `text/html; charset=utf-8`, 원본 493,936 bytes. 문서 타이틀 `JW 시장분석 Agent · Strategic View`.
- 프리픽스 설정 시 `/jw-market-backend-api`, `/jw-market-backend-api/`에도 동일 등록(`main.py:100-102`). 전부 비노출.

### 3.2 `GET /api/health` — 헬스체크 (META, 노출)

- **핸들러:** `health` (`routes/health.py:13`)
- **파라미터:** 없음
- **응답 구조:** `{ status, markets_loaded, brands_loaded, version }` — `version`은 `APP_VERSION`(배포 이미지 커밋). 운영 전환 후 image tag·OpenAPI version 대조용.
- **실응답 (200, 107 bytes):**
```json
{
 "status": "ok",
 "markets_loaded": 25,
 "brands_loaded": 25,
 "version": "ad782bc064ba03a45eaa4f1e301dbd75b8bf9a9e"
}
```
> `version` 값은 운영 이미지 커밋(`ad782bc0…`, release annotation `f139-brand-activity-general-scope`)이며 develop `761b4def`과 다른 latency 릴리즈 빌드다(BASELINE 참조).

### 3.3 `GET /api/market-status` — 포탈 시장 현황 카드 (노출)

- **핸들러:** `market_status` (`routes/market_status.py:13`)
- **파라미터:** 없음
- **응답 구조:** `cache_market_status.response_json` 그대로 — `{ brand_cards[], kpi_summary{UBIST, IQVIA} }`. 각 `brand_cards` 원소는 `atc_codes, atc_desc, back, back_extended, brand, company, front{sources_data}, is_jw, is_target, market_id, market_name, rank, total_brands_in_market …`.
- **오류:** 캐시 부재 시 `404 {error:"cache_not_found", cache:"cache_market_status"}`, 페이로드 타입 오류 시 `500 invalid_cache_payload`.
- **실응답 (200, 35,015 bytes):** `brand_cards` 25건, 예시 첫 카드 `brand:"라베칸"`, `market_id:"strategy_001"`. `kpi_summary.UBIST.period_recent:"2026-05"`, `kpi_summary.IQVIA.period_recent:"2026-Q1"`.

### 3.4 `GET /api/brands` — 브랜드 목록·검색 (노출)

- **핸들러:** `list_brands` (`routes/brands.py:130`)

| 파라미터 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `q` | string | 아니오 | `None` | 브랜드명 완전 일치 검색 |
| `query` | string | 아니오 | `None` | BFF 호환 검색어(=q 대체) |
| `market_id` | string | 아니오 | `None` | `strategy_NNN` 시장 필터(기본 목록에만 적용) |
| `limit` | int | 아니오 | `20` | 검색 결과 상한, 범위 `1~50`(`ge=1, le=50`) |

- **동작:** `q`/`query` 둘 다 있고 정규화 후 값이 다르면 `422 {error:"conflicting_search_query"}`. 검색어가 있으면 검색 결과(list) + 응답 헤더 `X-Has-More`/`X-Total-Matches`/`X-Result-Limit`. 검색어가 없으면 `cache_brands` 기본 목록(선택 시 `market_id` 필터).
- **실응답 — 기본 목록 (`GET /api/brands`, 200, 10,801 bytes):** 25건. 원소: `{ atc_codes, atc_desc, brand, general_sources[], is_dual_source, is_jw, is_target, market_id, market_label_kor, market_name, market_name_short, mkt_team, rank, sources[], strategic_sources[] }`.
- **실응답 — 검색 (`GET /api/brands?q=리바로&limit=5`, 200, 479 bytes, 헤더 `x-has-more:false, x-total-matches:1, x-result-limit:5`):**
```json
[
 {
  "brand": "리바로",
  "sources": ["UBIST", "...(2개 중 1개)"],
  "strategic_sources": ["UBIST"],
  "general_sources": ["UBIST", "...(2개 중 1개)"],
  "contexts": [
   {"view_kind": "general", "market_id": "C10A1", "market_name": "STATINS (HMG-COA RED)", "has_market_data": true},
   "...(3개 중 1개)"
  ],
  "is_jw_target": true
 }
]
```

### 3.5 `GET /api/cause/{brand_name}` — 운영 포탈 원인분석 조회 (노출)

- **핸들러:** `cause` (`routes/cause.py:120`)
- **path:** `brand_name` (string, 필수, URL 인코딩; 내부 `unquote`)

| query | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `view` | string | 아니오 | `market_landscape` | `market_landscape` 또는 `competitive_dynamics` |
| `source` | string | 아니오 | `UBIST` | `UBIST` 또는 `IQVIA` |
| `measure` | string | 아니오 | `sales` | source별 허용 measure(§1.2) |
| `market_id` | string | 아니오 | `None` | `strategy_006` 또는 `ml_006` 형태 허용 |

- **응답 구조:** 포탈 렌더링 계약(23섹션) dict — 최상위 `brand, brand_name, brand_key, market_id, view, source, measure, unit_label, data{ kpi, sources_data, market_size_series, market_yoy_series, hhi_series_5y, brand_ranking … }, markets`.
- **오류:** 미존재/미소속 브랜드 `404 {error:"brand_not_found", brand}`. 과부하 시 `429 {error:"dynamic_market_overloaded"}`(헤더 `Retry-After:2`). 브랜드가 존재하나 소스에 없으면 `200`에 `data:null, reason:"brand_not_in_source"`.
- **실응답 — ML(`view=market_landscape`, 200, 2,256,903 bytes):** `market_id:"strategy_006"`, `data.kpi.target_brand:"리바로"`, `market_yoy_recent_pct:4.86`.
- **실응답 — CD(`view=competitive_dynamics`, 200, 2,256,918 bytes):** 동일 구조, `view:"competitive_dynamics"`.
- **실오류 — 404 (64 bytes):**
```json
{"detail": {"error": "brand_not_found", "brand": "없는브랜드"}}
```

### 3.6 `GET /api/deep-analysis/{brand_name}` — 포탈 심층분석 조회 (노출)

- **핸들러:** `deep_analysis` (`routes/deep_analysis.py:1367`)
- **path:** `brand_name` (string, 필수, URL 인코딩)

| query | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `view` (legacy) | string | 아니오 | `None`(→`strategic`) | `general` 또는 `strategic` |
| `view_kind` (신규) | `general\|strategic_ml\|strategic_cd` | 아니오 | `None` | 신규 계약 뷰 종류 |
| `market_id` (신규) | string | 아니오 | `None` | general=ATC4, strategic_ml=ml_id, strategic_cd=cd_id |
| `source` (신규) | `ubist\|iqvia` | 아니오 | `None` | 요청 시장의 단일 소스 |

- **계약 규칙(실측):**
  - `view_kind`/`market_id`/`source` 중 하나라도 있으면 "formal 계약". formal + legacy `view` 병용 시 `422 {error:"conflicting_view_contract"}`.
  - formal인데 `view_kind` 누락 시 `422 {error:"missing_view_kind"}`.
  - `atc4` 쿼리 파라미터 전달 시 `422 {error:"unsupported_query_parameter", parameter:"atc4"}`(atc4는 백엔드 유도).
- **응답 구조:** 심층분석 payload dict — `brand, brand_name, market_id, market_name, available_combos[], data{ forecast{by_combo{…}}, events[], … }, generated_at`. formal 경로는 추가로 `view_kind, source` echo, 예측/이벤트 부재 시 `forecast_meta`/`events_meta` 상태 블록.
- **오류:** 미존재 `404 {error:"brand_not_found", brand}`.
- **실응답 — strategic(legacy 기본, 200, 571,929 bytes):** `market_id:"strategy_006"`, `data.forecast.by_combo["UBIST.sales"|"UBIST.volume"]`, 예측 모델 `HoltWinters`(prophet 미설치 fallback, `forecast_warnings:["prophet_fit_failed_fallback:ModuleNotFoundError"]`).
- **실응답 — general(`?view=general`, 200, 755,794 bytes):** `available_combos:["IQVIA.counting_unit", …6]`, `data.events[]` 47건(뉴스), `data.forecast.by_combo["IQVIA.*"]`.
- **실응답 — formal ML(`?view_kind=strategic_ml&source=ubist`, 200, 591,184 bytes):** `view_kind:"strategic_ml", source:"ubist", market_id:"ml_006"`.
- **실오류 — 404 (64 bytes):** cause와 동일 형식.

### 3.7 `POST /api/dynamic-market` — 동적 시장 원인분석 재계산 (노출)

- **핸들러:** `dynamic_market` (`routes/dynamic_market.py:64`)
- **바디:** `DynamicMarketRequest` (`models/dynamic_market.py:232`, `extra="forbid"`)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `view` | string | 아니오 | `None` | `general`·`strategic_ml`·`strategic_cd`. 생략 시 `filters.view_kind` 추론(deprecated) |
| `filters` | object | 아니오 | `{}` | `DynamicMarketFilters` |
| `filters.atc4` | list[str] | 아니오 | `[]` | 공통 ATC4 OR 범위. 일반뷰=scope, 전략뷰=내부 narrowing |
| `filters.view_kind` | string | 아니오 | `None` | deprecated legacy 힌트 |
| `filters.focus_brand_key` | string | 아니오 | `None` | 선택 브랜드명(narrowing 후 유지) |
| `filters.analysis_level` | object | 아니오 | `{}` | UBIST/IQVIA 소스별 분석레벨(§부록 A) |
| `source` | string | 아니오 | `ubist` | ubist 또는 iqvia |
| `measure` | string | 아니오 | `sales` | sales 또는 qty |
| `options.period_range` | object`{start,end}` | 아니오 | `None` | `YYYY-MM` 기간 범위 |

- **응답 구조:** `{ status:"SUCCESS", result:{…} }`. 일반뷰 result는 `analysis_level_market_status` 등 채널·세그먼트 계약, 전략뷰 result는 cause 계약(kpi/brand_ranking 등)과 동형 + `view` echo.
- **오류:** scope 부재 `400 {error:"invalid_dynamic_market_request", message:"at least one ATC4 or molecule filter is required"}`. view 오류 `422 invalid_dynamic_market_view`. scope 초과 `400 dynamic_scope_too_broad`. 과부하 `429`.
- **실응답 — 일반뷰 (`{"view":"general","filters":{"atc4":["C10A1"]},"source":"ubist","measure":"sales"}`, 200, 2,610,403 bytes):** `result.brand:"리피토"`, `result.data.analysis_level_market_status.levels:["판매사",…6]`, 채널 5종(전체/의원 IGF/주요고객 종합병원 순환기/병원/주요고객 종합병원 신경).
- **실응답 — 전략뷰 ML (`{"view":"strategic_ml","filters":{"focus_brand_key":"리바로"},…}`, 200, 2,256,951 bytes):** `result.market_id:"strategy_006", result.view:"strategic_ml"`.
- **실응답 — 전략뷰 CD (`{"view":"strategic_cd","filters":{"focus_brand_key":"리바로"},…}`, 200, 2,256,962 bytes):** `result.view:"strategic_cd"`.
- **실오류 — 400 (빈 filters, 114 bytes):**
```json
{"detail": {"error": "invalid_dynamic_market_request", "message": "at least one ATC4 or molecule filter is required"}}
```

### 3.8 `GET /api/dynamic-market/filter-options` — 동적 시장 필터 옵션 (노출)

- **핸들러:** `dynamic_market_filter_options` (`routes/dynamic_market.py:412`)

| query | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `view` | string | 아니오 | `general` | `general`·`strategic`(ML)·`strategic_cd`(CD) |
| `source` | string | 아니오 | `ubist` | ubist 또는 iqvia |
| `measure` | string | 아니오 | `sales` | sales 또는 qty |
| `brand` | string | 아니오 | `None` | 선택 브랜드명. 목록 미제한, 소속 ATC4에 flag |
| `atc4_codes` | list[str] | 아니오 | `None` | 일반뷰 2단계 선택 ATC4(OR 재산출) |
| `selections` | string(JSON) | 아니오 | `None` | 선택된 차원 필터 JSON(차원 내 OR·차원 간 AND) |
| `market_id` | string | 아니오(deprecated) | `None` | 기존 호출자 호환 시장 id |

- **응답 구조:** `{ view, source, market_id, dimensions[], atc{atc1..4, selectable_levels}, brand, brand_matched{}, default_selections{}, applied_selections{} }`.
- **오류:** `400 {error:"invalid_dynamic_market_filter_options_request", message}`.
- **실응답 — general (`?view=general&brand=리바로&source=ubist`, 200, 1,042,894 bytes):** `dimensions` 8종(molecule 등, molecule 값 1,540개), `atc.atc4` 364개, `brand_matched.atc4:["C10A1"]`, `default_selections.atc4:["C10A1"]`.
- **실응답 — strategic(ML) (`?view=strategic&brand=리바로&source=ubist`, 200, 1,019 bytes):** `market_id:"ml_006"`, `dimensions:[]`, `atc.atc1[C].default:true` — 시장 소속 ATC만 반환(전략뷰는 축소).

### 3.9 `GET /api/dynamic-market/brand-option-check` — 브랜드 기준 옵션·기본선택 확인 (노출)

- **핸들러:** `dynamic_market_brand_option_check` (`routes/dynamic_market.py:475`)

| query | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `brand` | string | **예** | — | 선택 브랜드명(위치·필수) |
| `view` | string | 아니오 | `general` | 뷰 |
| `source` | string | 아니오 | `ubist` | 소스 |

- **응답 구조:** `filter-options`와 동일 계약 + `brand_matched`(dimension_type → 값 리스트). `market_id`는 공개 입력 아님(브랜드로 내부 해석, 미상 파라미터는 무시).
- **오류:** `400 {error:"invalid_dynamic_market_brand_option_check_request", message}`.
- **실응답 (`?brand=리바로&view=general&source=ubist`, 200, 1,042,894 bytes):** filter-options general과 동형(`brand_matched.seller:["JW중외제약"]`, `default_selections.atc4:["C10A1"]`).

### 3.10 `GET /api/market-filter/atc-options` — 시장필터 1단계 ATC 옵션 (노출)

- **핸들러:** `market_filter_atc_options_get` (`routes/market_filter.py:16`), 응답 모델 `MarketFilterAtcOptionsResponse`

| query | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `brand_name` | string | 아니오 | `None` | 선택 브랜드명. general은 생략 가능(생략 시 전체 ATC universe) |
| `view` | `general\|strategic` | 아니오 | `general` | 그 외 값은 **422** |
| `source` | `ubist\|iqvia` | 아니오 | `ubist` | 내부 iqvia_nsa 미노출 |

- **응답 구조:** `{ brand_name, view, source, market_id, flagged_atc4[], atc{atc1..4:[{key, level, parent, flag}]} }`. `flag=true`는 선택 브랜드가 그 ATC 노드 소속(초기 선택/locked 표시).
- **오류:** 입력 오류 `400 {error:"invalid_market_filter_atc_options_request", message}`. 잘못된 `view` literal은 FastAPI 검증 단계에서 `422 literal_error`.
- **실응답 (`?brand=리바로&view=general&source=ubist`, 200, 41,480 bytes):** `brand_name:""`(echo 공란), `market_id:null`, `flagged_atc4:[]`, `atc.atc4` 364개(`{key, level, parent, flag}` 형태).
- **실오류 — 422 (`?view=badview`, 173 bytes):**
```json
{"detail": [{"type":"literal_error","loc":["query","..."],"msg":"Input should be 'general' or 'strategic'","input":"badview","ctx":{"expected":"'general' or 'strategic'"}}]}
```

### 3.11 `GET /api/market-scope/options` — 시장군 옵션 조회 (내부 EP·비노출)

- **핸들러:** `options` (`routes/market_scope.py:34`, `include_in_schema=False`)

| query | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `brand` | string | **예** | — | 브랜드명 |
| `view_family` | string | 아니오 | `strategy` | `strategy`. `general`은 **501** |
| `source` | string | 아니오 | `None` | 지정 시 해당 소스 옵션만 필터 |

- **응답 구조:** `{ brand, view_family, source, options[], catalog_version }`. option 원소: `{ option_id, label, option_type, view_family, source_markets[], atc4_set[], members[], member_status, available_sources[], catalog_version }`.
- **오류:** 잘못된 view_family `400 {error:"invalid_view_family"}`. general `501 {error:"general_scope_not_ready"}`.
- **실응답 (`?brand=리바로&view_family=strategy`, 200, 1,086 bytes):** `options[0].option_id:"group:livalo_family"`, `label:"리바로 시장군"`, `source_markets:["strategy_006"]`, `catalog_version:"GROUP_01_20260716"`.

### 3.12 `POST /api/market-scope/resolve` — 시장 범위 해석 (내부 EP·비노출)

- **핸들러:** `resolve` (`routes/market_scope.py:56`), 바디 `MarketScopeResolveRequest`(`models/market_scope.py:14`)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `brand` | string | **예** | — | 브랜드명 |
| `view_family` | `strategy\|general` | 아니오 | `strategy` | general은 501 |
| `source` | `UBIST\|IQVIA\|ubist\|iqvia\|nsa\|iqvia_nsa` | 아니오 | `UBIST` | |
| `measure` | string | 아니오 | `sales` | |
| `option_ids` | list[str] | **예** | — | 최소 1개(`min_length=1`) |

- **응답 구조:** `{ scope_hash, view_family, selected_option_ids[], resolved_source_markets[], resolved_atc4_set[], excluded_members[], dedup{…}, catalog_version, algorithm_version }`.
- **오류:** `400 {error:"invalid_market_scope"}`, overlap 시 `409 {error:"overlap_without_fact_identity"}`.
- **실응답 (`{"brand":"리바로","view_family":"strategy","source":"UBIST","measure":"sales","option_ids":["group:livalo_family"]}`, 200, 567 bytes):** `resolved_source_markets:["strategy_006"]`, `dedup.disjoint:true, dropped_duplicate_count:0`, `algorithm_version:"strategy-union-recalc-v1"`.

### 3.13 `POST /api/market-scope/cause` — 시장군 원인분석 (내부 EP·비노출)

- **핸들러:** `cause` (`routes/market_scope.py:69`), 바디 `MarketScopeCauseRequest`(resolve 필드 + `view`)

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| (resolve 전 필드 동일) | | | |
| `view` | string | 아니오 | `market_landscape` |

- **응답 구조:** `{ status:"SUCCESS", result:{…} }` — cause 계약과 동형(`data.brand_ranking`, `rankings_by_year` 등).
- **오류:** `400 invalid_market_scope`, `409 unsafe_scope_union`.
- **실응답 (`{…,"option_ids":["group:livalo_family"],"view":"market_landscape"}`, 200, 1,666,415 bytes):** `result.data.brand_ranking.rankings_by_year{2022..2026}`, `period_count_by_year{2026:5}`.

### 3.14 `GET /api/brand-activity/csd-presence` — 브랜드 CSD 원천 존재 여부 (노출)

- **핸들러:** `brand_activity_csd_presence` (`routes/brand_activity.py:69`)

| query | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `brand` | string | 조건부 | `None` | 단일 브랜드 |
| `brands` | string | 조건부 | `None` | 쉼표 구분, 최대 50개 |

- **규칙:** `brand`/`brands` **정확히 하나** 필수(둘 다/둘 다 없음 → 422). 빈 값 422. 50 초과 `422 {error:"too_many_brands", limit:50}`.
- **응답:** 단일 시 `CsdPresence{brand, resolved, csd_present, reason}`, 복수 시 그 list.
- **실응답 (`?brand=리바로`, 200, 70 bytes):**
```json
{"brand": "리바로", "resolved": true, "csd_present": true, "reason": null}
```
- **실오류 — 422 (파라미터 없음, 62 bytes):**
```json
{"detail": {"error": "exactly_one_of_brand_or_brands_required"}}
```

### 3.15 `GET /api/brand-activity/topics` — 전체 토픽 페이로드 (내부 디버그·비노출)

- **핸들러:** `brand_activity_topics` (`routes/brand_activity.py:96`, `include_in_schema=False`)
- **파라미터:** 없음
- **응답:** `{ data:[…topic market payloads] }`. 각 원소 `{ scope{scope_id, display_name, atc4_values[], quality_grade, source_row_count}, axis{topics[]}, brands[], quality{} }`.
- **오류:** `500 {error:"invalid_brand_activity_topic_payload"}`.
- **실응답 (200, 85,183 bytes):** `data` 12건, 첫 원소 `scope.scope_id:"atc4:A02B2", display_name:"PPI Market", source_row_count:10677`, `axis.topics` 8종.

### 3.16 `GET /api/brand-activity/topics/{scope_id}` — 단일 토픽 페이로드 (내부 디버그·비노출)

- **핸들러:** `brand_activity_topic` (`routes/brand_activity.py:105`, `include_in_schema=False`)
- **path:** `scope_id` (string, 필수, 예 `atc4:A02B2`)
- **응답:** `{ data }` 또는 미존재 시 `{ data:null, reason:"scope_not_found", scope_id }`.
- **실응답 (`/topics/atc4:A02B2`, 200, 11,138 bytes):** 단일 scope payload(3.15의 원소 1건과 동형).

### 3.17 `POST /api/brand-activity/topics` — 브랜드별 토픽 그리드 (노출, +mock 별칭)

- **핸들러:** `brand_activity_topic_matrix` (`routes/brand_activity.py:117,121`)
- **mock 별칭:** `POST /jw-brand-activity-mock/api/brand-activity/topics` (동일 핸들러, 비노출)
- **바디:** `BrandActivityTopicsRequest`(`models/brand_activity.py:264`, base `extra="ignore"`)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `view` | string | 아니오 | `general` | general·strategic_ml·strategic_cd |
| `selected_brand` | string | **예** | — | 강조/시장 결정 브랜드 |
| `filters` | `MarketFilter` | 아니오 | `{}` | ATC+분석레벨+채널(§부록 A) |
| `filter` | `MarketFilter` | 아니오 | `{}` | legacy 단수(filters 우선) |
| `market_id` | string | 아니오 | `None` | 전략뷰 ml_id/cd_id |
| `visit_location` | str\|list | 아니오 | `"전체"` | 종별 행 필터 |
| `specialty` | str\|list | 아니오 | `"전체"` | 진료과 행 필터 |
| `interest` | str\|list | 아니오 | `"전체"` | 관심도 행 필터 |
| `prescription_evolution` | str\|list | 아니오 | `"전체"` | 처방변화 행 필터 |
| `start_date` / `period_start` | string | 아니오 | `None` | 시작월 `YYYY-MM`(형식 검증, 둘 다 주면 일치 필수) |
| `end_date` / `period_end` | string | 아니오 | `None` | 종료월 `YYYY-MM`(start ≤ end) |
| `top_n` | int | 아니오 | `5` | 카드별 상위 토픽 개수 `1~10`(`ge=1, le=10`) |

- **응답 계약(핵심):** `{ data{ scope{}, brands[], reason? }, meta{ period{}, request_normalized? } }`. 각 `share_pct`는 독립 계산이라 합이 100% 초과 가능. `etc_pct = max(0, 100 - top_n 토픽 share 합)`(top_n 의존, "기타"가 아닌 호환 필드). `event_count=0`이면 `topic_shares=[]`.
- **오류:** 필터 부재 `400 {error:"invalid_brand_activity_topic_request", message:"view, filters.atc4, and selected_brand are required"}`. 시장 미식별 `404 market_not_found`. 페이로드 오류 `500`.
- **실응답 (`{"view":"general","selected_brand":"리바로","filters":{"atc":{"atc4":["C10A1"]}},"top_n":5}`, 200, 1,936 bytes):** `data.scope.market_id:"C10A1", market_name:"STATINS (HMG-COA RED)"`, `data.brands[0]{brand_key:"리바로", event_count:0, topic_shares:[], etc_pct:100.0}`, `data.reason:"no_topic_scope"`, `meta.period{start_date:"2023-06", end_date:"2026-05"}`.
- **실오류 — 400 (빈 filters, 123 bytes):**
```json
{"detail": {"error": "invalid_brand_activity_topic_request", "message": "view, filters.atc4, and selected_brand are required"}}
```

### 3.18 `POST /api/brand-activity/csd-timeseries` — 활동·처방 추세 (노출, +mock 별칭)

- **핸들러:** `brand_activity_csd_timeseries` (`routes/brand_activity.py:179`)
- **mock 별칭:** `POST /jw-brand-activity-mock/api/brand-activity/csd-timeseries` (비노출)
- **바디:** `CsdTimeseriesRequest`(base + 아래)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `view` / `selected_brand` / `filters` / `filter` | (base) | selected_brand **예** | — | §3.17 base |
| `market_id` | string | 아니오 | `None` | 전략뷰 ml_id/cd_id |
| `csd_market` | string | 아니오 | `None` | 선택 CSD 시장. 미지정 시 전체 합산 |
| `mode` | string | 아니오 | `absolute` | `absolute` 또는 `share` |
| `window` | `{start,end}` | 아니오 | `None` | 분기/월 window |

- **동작:** CSD 활동량은 `jw_channel='TOTAL'`만(region=TOTAL), 월간축(`activity_months`); IQVIA 지표는 분기축(`quarters`).
- **오류:** 입력 `400 invalid_csd_timeseries_request`. 잘못된 `csd_market` `422 {error:"invalid_csd_market", available[]}`. 매핑 없음/모호는 오류 아님 → `200`에 `data.available:false, reason:"no_csd_mapping"|"csd_market_ambiguous"`. 시장 미식별 `404`.
- **실응답 (`{"view":"general","selected_brand":"리바로","filters":{"atc":{"atc4":["C10A1"]}},"mode":"absolute"}`, 200, 36,331 bytes):** `data.scope{csd_market:"LIVALO", ranking_quarter:"2026-Q1", quarters, activity_months, measures}`, `data.brands[0].series{ activity(source:csd, 월간 absolute/ratio), sales·unit·counting_unit·dosage_unit(source:iqvia_nsa, 분기)}`.

### 3.19 `POST /api/brand-activity/csd-activity-series` — CSD 활동량·비율·순위 추세 (노출)

- **핸들러:** `brand_activity_csd_activity_series` (`routes/brand_activity.py:236`), 바디 `CsdActivitySeriesRequest`(`brand_activity_csd_activity_contract.py:58`, `extra="ignore"`)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `view` | string | **예**(모델상 필수) | — | general·strategic_ml·strategic_cd (그 외 미지원) |
| `selected_brand` | str\|list | **예** | — | 강조/시장 결정 브랜드 |
| `filters` / `filter` | `MarketFilter` | 아니오 | `{}` | 시장·차원 필터 |
| `entity_level` | string | 아니오 | `brand` | `brand` 또는 `company` |
| `csd_channel` | string | 아니오 | `TOTAL` | `TOTAL/GH/SHPPI/CPPI/GH+SHPPI` |
| `csd_market` | string | 아니오 | `None` | 미지정 시 매핑 전체 union 합산 |
| `selected_entities` | list[str] | 아니오 | `[]` | 최대 6개(`max_length=6`). 미지정 시 선택+top5 |
| `period` | `{start,end}` | 아니오 | `None` | 분기 window(미지정 시 최신 1년, 최대 3년) |

> 일반뷰(`general`)에서 `filters.atc4`(또는 market_scope) 없이 요청하면 `400`(파싱 단계에서 "filters.atc4 and selected_brand are required").

- **응답 구조:** `{ data{ scope{}, entity_level, channel, period{quarters, months, max_quarters, default_quarters}, entities[], series_by_csd_market{} } }`. entity 원소 `{ key, display_name, is_selected, is_jw, activity{absolute[], share_pct[], rank[]} }`.
- **오류:** `400 invalid_csd_activity_series_request`, `422 invalid_csd_market`, 매핑 없음/모호 `200 data.available:false`, 시장 미식별 `200 {data:null, reason:"market_not_found"}`.
- **실응답 (`{"view":"general","selected_brand":"리바로","filters":{"atc4":["C10A1"]},"entity_level":"brand","csd_channel":"TOTAL"}`, 200, 107,240 bytes):** `data.scope.csd_market:"LIVALO"`, `data.period{max_quarters:12, default_quarters:4}`, `data.entities[0]{key:"리바로", activity{absolute/share_pct/rank}}`, `data.series_by_csd_market.LIVALO{market_totals{}, by_entity{…}}`.

### 3.20 `POST /api/brand-activity/interest-rx-matrix` — 관심도×처방빈도 버블 (노출, +mock 별칭)

- **핸들러:** `brand_activity_interest_rx_matrix` (`routes/brand_activity.py:278`)
- **mock 별칭:** `POST /jw-brand-activity-mock/api/brand-activity/interest-rx-matrix` (비노출)
- **바디:** `BrandActivityInterestRxRequest`(base + 아래)

| 필드 | 타입 | 필수 | 기본값 | 설명 |
|---|---|---|---|---|
| `view` / `selected_brand` / `filters` / `filter` | (base) | selected_brand **예** | — | §3.17 base |
| `market_id` | string | 아니오 | `None` | 전략뷰 ml_id/cd_id |
| `visit_location` | string | 아니오 | `"전체"` | 종별 단일 |
| `specialty` | string | 아니오 | `"전체"` | 진료과 단일 |
| `period_start` / `period_end` | string | 아니오 | `None` | 조회 월 |
| `weights` | object | 아니오 | `None` | interest/rx_frequency/prescription_evolution 가중치 override |

- **응답 구조:** `{ data{ scope{}, filters_applied{}, period{}, levels{}, weights{}, brands[], market_average{} }, meta{ request_normalized } }`. X축=`rx_frequency_score`, Y축=`prescription_evolution_score`, 버블 면적=`event_count`, `market_average`=기준선.
- **오류:** `400 invalid_interest_rx_matrix_request`, 시장 미식별 `404`.
- **실응답 (`{"view":"general","selected_brand":"리바로","filters":{"atc":{"atc4":["C10A1"]}}}`, 200, 5,551 bytes):** `data.scope.market_id:"C10A1"`, `data.brands[0]{brand_key:"리바로", interest_score:0.588, rx_frequency_score:0.678, prescription_evolution_score:0.751, event_count:1329}`, `data.market_average{…}`.

### 3.21 정적 마운트 (라우트 아님, 참고)

- `/static` 및 프리픽스 설정 시 `{prefix}/static` — `StaticFiles(check_dir=False)`, `main.py:90-92`. FRONTEND_DIR(`/app/static` 또는 `docs/reference`) 정적 파일 서빙.

---

## 4. 오류 응답 계약 (실측 요약)

| 상황 | HTTP | 응답 본문(실측) | EP |
|---|---|---|---|
| 없는/미소속 브랜드 | **404** | `{"detail":{"error":"brand_not_found","brand":"…"}}` | cause, deep-analysis |
| 빈 dynamic-market filters | **400** | `{"detail":{"error":"invalid_dynamic_market_request","message":"at least one ATC4 or molecule filter is required"}}` | dynamic-market |
| 빈 brand-activity filters | **400** | `{"detail":{"error":"invalid_brand_activity_topic_request","message":"view, filters.atc4, and selected_brand are required"}}` | brand-activity/topics |
| view literal 오타 | **422** | `{"detail":[{"type":"literal_error","msg":"Input should be 'general' or 'strategic'",…}]}` | market-filter/atc-options |
| 필수 파라미터 누락(brand/brands) | **422** | `{"detail":{"error":"exactly_one_of_brand_or_brands_required"}}` | csd-presence |
| view 계약 충돌 | **422** | `{"detail":{"error":"conflicting_view_contract"}}` | deep-analysis |
| 과부하 | **429** | `{"detail":{"error":"dynamic_market_overloaded"}}` (`Retry-After:2`) | cause, dynamic-market |
| 캐시 부재 | **404** | `{"detail":{"error":"cache_not_found","cache":"…"}}` | market-status, brands |
| 잘못된 CSD 시장 | **422** | `{"detail":{"error":"invalid_csd_market","available":[…]}}` | csd-timeseries, csd-activity-series |
| general scope 미구현 | **501** | `{"detail":{"error":"general_scope_not_ready"}}` | market-scope/* |

> **의뢰서 정정 각주.** 의뢰서에는 "미소속 brand → 400"으로 기술되어 있으나, 2026-07-17 실측 결과 `cause`·`deep-analysis`의 미소속/미존재 브랜드는 **404 `brand_not_found`**를 반환한다(본 명세 기준). 400은 "빈 filters"(dynamic-market·brand-activity)와 같은 요청 스키마 위반에만 사용된다.

FastAPI 관례상 오류 본문은 항상 `{"detail": …}`로 래핑되며, `detail`은 dict(도메인 오류) 또는 list(pydantic 검증 오류)다.

---

## 5. ingest hook API (별도 서비스 · 배포됨 · 리허설 격리 모드)

> ★ **배포 상태(2026-07-17 09:59 UTC 재실측).** ingest hook는 클러스터에 **배포되어 있다** — deployment `jw-ingest-hook`(ns `llmops`, `1/1` Available, age ~36분), service `jw-ingest-hook`(ClusterIP `10.13.33.50`, `8080/TCP`), 컨테이너명 `trigger`. 이미지는 digest-pin된 orchestrator 이미지(`jw-pipeline-orchestrator@sha256:fea29685…`)로, ingest Job과 동일 코드를 구성적으로 실행한다.
>
> ★ **격리 원칙 유지.** `jw-market-backend-api`와는 **프로세스/pod/엔드포인트가 완전 분리된 별도 서비스**다(load·failure 격리, STOP ①). 백엔드 API와 다른 pod·다른 svc·다른 포트(8080)를 사용한다.
>
> ★ **리허설 격리 모드.** 배포 env에 `INGEST_REHEARSAL_ROOT=/tmp/ingest-rehearsal`이 설정되어 있어 job_runner가 **리허설 격리 모드**로 동작한다(실 mart 반영이 아닌 격리 실행). 즉 서비스는 살아 있으나 실제 적재 승격은 아직 리허설 경계 안에 있다. 부작용을 일으키는 POST(webhook·reconcile)는 본 명세 작성 시 **호출하지 않았고**, 스키마는 코드 정본(`pipeline/scripts/ingest_hook/`)을 근거로 한다.

- **실행 진입점:** `uvicorn --factory pipeline.scripts.ingest_hook.app:build --port 8080`. `docs_url=None, redoc_url=None`(OpenAPI 문서 비활성).
- **입력원(rebase 반영):** `app.py:build`는 `INGEST_S3_BUCKET`이 설정되면 MinIO/S3에서 submission을 읽고, 비어 있으면 로컬 `INGEST_INPUT_ROOT` 경로를 읽는다. 현재 배포 env는 `INGEST_S3_BUCKET`가 공란(로컬 경로 모드), `MINIO_ENDPOINT=http://minio.llmops.svc.cluster.local:9000`은 준비만 됨.
- **ledger:** `INGEST_LEDGER_SQLITE` 미설정 → **mysql ledger 분기**(`MARIADB_DATABASE=jw_mart_d2_stage_20260630_r2`). status 실호출이 500이 아닌 404를 반환한 것으로 보아 `ingest_ledger` 조회가 정상 실행됨(테이블 존재·질의 가능; 활성화 시점 DDL 반영 확인).

| 메서드 | 경로 | 핸들러 | 파라미터/바디 | 응답 | 정의 위치 | 호출 |
|---|---|---|---|---|---|---|
| GET | `/healthz` | `healthz` | 없음 | `{"ok": true}` | `app.py:99` | 실호출 O |
| POST | `/ingest/webhook` | `webhook` | 바디 `{"manifest_path": str}` (`WebhookPayload`) | `{epoch, category, manifest_sha, decision, status, reason, job_name}` | `app.py:103` | 스키마만(부작용) |
| GET | `/ingest/status` | `status` | query `epoch`, `category`, `manifest_sha` (**전부 필수**) | `{epoch, category, manifest_sha, status, reason, job_name, uploaded_by, received_at, finished_at}` | `app.py:107` | 실호출 O |
| POST | `/ingest/reconcile` | `reconcile` | 없음 | `{"launched": {category: job_name}}` | `app.py:124` | 스키마만(부작용) |

- **webhook 오류 계약(코드):** `manifest_path`가 input root 밖 → `400`. MinIO 모드에서 key 부재 → `404`. 계약 위반 → `422 "contract violation: …"`. `complete=false` manifest → `409 "manifest is not marked complete"`.
- **status 오류:** 미상 제출 식별자 → `404 "unknown submission identity"`.

### 5.1 실호출 캡처 (read-only GET, 2026-07-17 09:59 UTC)

`kubectl exec deploy/jw-market-backend-api -- python3`(in-mesh)에서 `http://jw-ingest-hook.llmops:8080` 호출. POST는 부작용이 있어 호출하지 않음.

- **`GET /healthz` (200):**
```json
{"ok": true}
```
- **`GET /ingest/status?epoch=2026-W27&category=probe&manifest_sha=000…0`(존재하지 않는 probe 식별자, 404):**
```json
{"detail": "unknown submission identity"}
```
> status 응답은 FastAPI 기본 오류 래핑(`{"detail": …}`) 형식이며, probe 식별자가 ledger에 없어 404다. 정상 조회 시 위 표의 9개 필드를 반환한다(실 데이터 캡처는 부작용 없는 조회 대상 부재로 미수집).

### 5.2 Manifest 계약 (JW_Input_Detection_Contract v2.1)

정본: `ingest_hook/contract.py`. 문서와 코드가 불일치하면 코드가 우선하되, 변경 시 문서(v2.1)를 같은 라운드에 갱신해야 한다.

| 필드 | 타입 | 필수 | 규칙(코드 실측) |
|---|---|---|---|
| `contract_version` | string | **예** | 값은 정확히 `"v2"`여야 함(`CONTRACT_VERSION="v2"`, `contract.py:22`). 그 외 값은 ContractError |
| `epoch` | string | **예** | 정규식 `^\d{4}-(0[1-9]\|1[0-2]\|Q[1-4]\|W(0[1-9]\|[1-4][0-9]\|5[0-3]))$` — **월간 `2026-07` · 분기 `2026-Q2` · 주간 `2026-W27`**(v2.1 델타) |
| `category` | string | **예** | 소문자 정규화, 비어 있으면 오류 |
| `complete` | bool | **예** | JSON boolean. webhook은 `true`만 수락(submit-confirm) |
| `files` | array | **예** | 비어 있지 않은 배열 |
| `files[].path` | string | **예** | |
| `files[].sha256` | string | **예** | 64자리 hex sha256 |
| `files[].rows` | int | 아니오 | 음이 아닌 정수(있을 때) |
| `files[].period_start`/`period_end` | string | 아니오 | |
| `submitted_at` | string | 아니오 | |
| `uploaded_by` | string | 아니오 | **v2.1 델타** — 사이트 세션 이메일(감사용). 부재/이상 타입도 제출을 **절대 실패시키지 않음** |

> `size`/`original_name`은 계약에서 제거됨. 미상 필드는 `Manifest.raw`에 보존. `manifest_sha`는 서버가 원본 바이트의 sha256로 산출.

---

## 부록 A · Brand Activity `MarketFilter` 구조

brand-activity POST 4종이 공유하는 `filters` 스키마(`models/brand_activity.py:163`). 차원 내 OR, 차원 간 AND. camelCase BFF 별칭은 내부에서 snake_case로 정규화된다.

- `filters.atc.atc4`: list[str] — ATC4 OR (예 `["C10A1"]`). flat `filters.atc4`도 호환(둘 다 주면 값 일치 필수, 불일치 시 `422 conflicting_market_filter`).
- `filters.analysis_level.ubist`: `{ seller[], molecule[], molecule_strength[], form[], route[], reimbursement[] }`
- `filters.analysis_level.iqvia`: `{ mfr_name_kor[], molecule_type[], molecule_desc[], pack_desc[], strength[], nhi_type[], audit_code[] }` (audit_code 빈 값 = 전체 채널)
- `filters.channel.audit_code[]`: legacy IQVIA audit code shortcut(신규는 analysis_level.iqvia.audit_code 사용)
- `filters.market_scope`: `{ option_id, member? }` — group:* 시장군의 특정 member 선택(Phase 1, general view만)
- 공개 입력에 `channel_axis` 전달 시 `ValueError`(→ analysis_level.<source>로 이동됨)

dynamic-market의 `analysis_level`은 별도 모델(`models/dynamic_market.py`, `extra="forbid"`)로, value-slice(facility/specialty/pairs, iqvia audit_code)와 row-filter를 구분한다. UBIST/IQVIA는 선택 source에 맞는 쪽만 허용.

---

*본 명세의 모든 파라미터·필드는 develop `761b4def` 실코드와 1:1 대조되었고(G-1 게이트 대상: 누락 0·유령 0), 예시 응답은 2026-07-17 in-mesh 실호출 캡처에서만 인용했다. 확인 불가 항목은 [확인 필요]로 표기하며, 본 문서에는 해당 항목이 없다.*
