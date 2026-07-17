# DOC-2 · DB 스키마 정의서

| 항목 | 값 |
|---|---|
| 기준 소스 | `develop` @ `7ca98403` (워크트리 `/tmp/jwm-develop-docs`) [^sha] |
| 운영 DB (전 차원 공통) | `jw_mart_d2_stage_20260630_r2` |
| 운영 DB (브랜드활동) | `jw_brand_activity_stage` |
| DB 엔진 | MariaDB (Galera 3노드 STS `galera-mariadb-galera`, 서비스 `llmops-mariadb-service` / `galera-mariadb-galera:3306`) |
| 스키마 캡처일 | 2026-07-17 |
| 문서 버전 | v1.1 |

[^sha]: 문서 머리 기준 SHA를 `761b4def` → `7ca98403`으로 갱신. mart/etl·API 영역은 `761b4def`와 무변경이며, SHA 갱신은 ingest hook 활성화 관련 반영분에 한정된다.

> 본 문서의 모든 컬럼 정의·타입·NULL·키·기본값·행수는 캡처 시점의 `SHOW CREATE TABLE` / `SELECT COUNT(*)` 실측(`evidence/db_schema_dump.txt`)과 1:1로 대응한다. 추측·자격값은 포함하지 않으며, 확인 불가 항목은 `[확인 필요]`로 표기한다.

---

## 0. 요약

| 구분 | jw_mart_d2_stage_20260630_r2 | jw_brand_activity_stage | 합계 |
|---|---|---|---|
| 정본 테이블 | 36 | 6 | 42 |
| 정본 뷰(VIEW) | 0 | 1 | 1 |
| 백업/작업용 테이블 | 55 | 0 | 55 |
| 캡처 시점 총 오브젝트 | 91 | 7 | 98 |

- `ingest_ledger`: 2026-07-17 활성화로 운영 DB `jw_mart_d2_stage_20260630_r2`에 **생성됨**(리허설 격리 모드 운용 중, 행수 3). 초기 스키마 캡처(`db_schema_dump.txt`, 91 오브젝트) 이후 생성되어 위 총계에는 미포함이며, 정본 테이블로 4절에 별도 수록. 반영 시 정본 테이블은 jw_mart_d2 37 / 합계 43.
- `[확인 필요]` 항목: 0건 (모든 정본 테이블의 생성 주체를 소스에서 확정).
- 정본 중 **제거 예정** 계열: `cache_cause`, `cache_deep_analysis`, `cache_deep_analysis_general`, `cache_deep_analysis_ai_analysis` (2.3절에 명기).

분류 규칙: 테이블명이 `_bak_*` · `_backup_*` · `_stage_*`(날짜 suffix) · `_mig_stg_*` · `_old_*` · `__failed_*` · `_cutover_*` · `_stage_ops_*` · `_staging_YYYYMMDD_*` 패턴이면 백업/작업용으로 분류하며, 납품 스키마가 아니다(3절에 목록만 수록). `tier2_match_staging`은 이름에 `staging`이 있으나 날짜 suffix가 없는 상시 운영 스테이징 테이블이므로 정본으로 분류한다.

---

## 1. 텍스트 ERD (정본 실컬럼 기반)

```
[카탈로그(ground truth)]
  catalog_ml_market (ml_id PK)
        │  ml_id
        ├──< catalog_cd_market (cd_id PK, ml_id FK-논리)
        └──< catalog_strategic_brand (brand_id PK, ml_id, cd_id, general_brand_key)

[일반뷰 mart]  ← atc4_code 축
  mart_general_market_metric (atc4_code+source+measure)
  mart_general_brand_metric  (brand_key+atc4_code+source+measure)
  mart_general_filter_dimension_metric (source+measure+atc4_code+brand_key+product_code+dimension_type+hash)
        │  brand_key
        └── brand_key ↔ catalog_strategic_brand.general_brand_key

[전략뷰 mart]  ← ml_id / cd_id 축
  mart_strategic_ml_market_metric (ml_id+source+measure)
  mart_strategic_ml_brand_metric  (ml_id+brand_id+source+measure)
  mart_strategic_cd_market_metric (cd_market_id+source+measure)
  mart_strategic_cd_brand_metric  (cd_market_id+cd_brand_id+source+measure)
  mart_strategic_filter_dimension_metric (market_kind+market_id+brand_id+…)

[브랜드↔분자/별칭]
  mart_brand_molecule (brand_key+atc4_code+mart_source+molecule_norm)
  brand_alias (alias_name PK → brand_key)

[Agent3 브랜드 강도]
  agent3_brand_strength (brand_key PK)
  agent3_brand_strength_source (brand_key+source)
  agent3_brand_strength_market (brand_key+source+market_id, view_kind∈{ml,cd})

[예측]
  deep_forecast_block   (brand_key+source+market_id, view_kind∈{ml,cd,general})
  deep_forecast_horizon (market_id+source+measure)

[캐시 서빙층]  ← mart/agent3/forecast에서 파생
  cache_brands · cache_market_status (단일 스냅샷 query_key)
  cache_brand_elements (brand_key)          ← agent3_brand_strength
  cache_dynamic_market_response (cache_key)  ← 동적 응답 조립 캐시
  cache_market_forecast_general (atc4_code+source+measure) ← deep_forecast
  cache_cause / cache_deep_analysis* (제거 예정)

[뉴스·이벤트 파이프라인]
  news_raw (news_id PK)
      │ news_id
      └──< events_raw (news_id PK) ──< events (event_id PK, news_id)
                                          │ event_id
                                          └──< event_brand_scores (event_id+brand_canonical UNIQUE)
  tier2_match_staging (run_id+news_id+brand_key)  ← 티어2 매칭 스테이징
  iqvia_nsa_quarterly_raw (id PK)  ← IQVIA 원천 적재

[AI 분석 산출]
  zeta_analysis_runs (run_id PK) ──< zeta_analysis_outputs (run_id+stage, FK CASCADE)

[브랜드활동 stage DB]
  km_keyword_event_stage (id PK) ─┐
  csd_channel_dynamics_stage       ├─ row_topic_assignment (row_id+topic_id+version)
  mart_brand_activity_topics ←─────┘   row_topic_assignment_status
      (scope_id PK, run_id → mart_brand_activity_topic_runs.run_id)
      row_topic_assignment_share_view (VIEW)
```

관계는 대부분 애플리케이션 레벨 논리 조인(문자열 키)이며, 물리 FK 제약은 `zeta_analysis_outputs → zeta_analysis_runs`(ON DELETE CASCADE) 1건만 정본에 존재한다.

---

## 2. 정본 테이블

각 표의 컬럼/타입/NULL/키/기본값은 `SHOW CREATE TABLE` 원문 그대로다. 키 열: PK=기본키, UQ=유니크키 구성, IDX=보조 인덱스 구성.

### 2.1 카탈로그 (ground truth)

카탈로그 3종은 운영 판단의 기준 데이터(ground truth)이며, `pipeline/etl/io/catalog/db_sync.py`의 `_create_catalog_table()` / `sync_catalog_tables()`가 원천 파일(엑셀 등)에서 동기화 적재한다.

#### catalog_ml_market — Market Landscape(ML) 시장 카탈로그
행수 16. 생성 주체: `pipeline/etl/io/catalog/db_sync.py`.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| ml_id | varchar(32) | NO | PK | |
| name | varchar(255) | YES | | NULL |
| data_source | varchar(32) | YES | | NULL |
| atc_codes_json | longtext | YES | | NULL |
| analyze_class | tinyint(1) | YES | | NULL |
| analyze_molecule | tinyint(1) | YES | | NULL |
| analyze_dosage_form | tinyint(1) | YES | | NULL |
| analyze_strength_pack | tinyint(1) | YES | | NULL |
| analyze_nhi_type | tinyint(1) | YES | | NULL |
| analyze_ox_gx | tinyint(1) | YES | | NULL |
| analyze_fish_oil | tinyint(1) | YES | | NULL |
| target_iqvia_1..3 | varchar(255) | YES | | NULL |
| target_ubist_1..4 | varchar(255) | YES | | NULL |
| source_file_version | varchar(512) | YES | | NULL |
| ingested_at | datetime(6) | YES | | NULL |
| catalog_manifest_hash | char(64) | YES | | NULL |

#### catalog_cd_market — Competitive Dynamics(CD) 시장 카탈로그
행수 19. 생성 주체: `pipeline/etl/io/catalog/db_sync.py`.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| cd_id | varchar(32) | NO | PK | |
| name | varchar(255) | YES | | NULL |
| ml_id | varchar(32) | YES | | NULL |
| cd_filter_id | varchar(32) | YES | | NULL |
| data_source | varchar(32) | YES | | NULL |
| analyze_class / molecule / dosage_form / strength_pack / nhi_type / ox_gx / fish_oil | tinyint(1) | YES | | NULL |
| target_iqvia_1..3 | varchar(255) | YES | | NULL |
| target_ubist_1..4 | varchar(255) | YES | | NULL |
| source_file_version | varchar(512) | YES | | NULL |
| ingested_at | datetime(6) | YES | | NULL |
| catalog_manifest_hash | char(64) | YES | | NULL |

#### catalog_strategic_brand — 전략 브랜드 카탈로그
행수 5,100. 생성 주체: `pipeline/etl/io/catalog/db_sync.py`. `general_brand_key`가 일반뷰 mart의 `brand_key`와 연결되고, `ml_id`/`cd_id`가 전략뷰 축과 연결된다.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| brand_id | varchar(128) | NO | PK | |
| name | varchar(255) | YES | | NULL |
| merge_name | varchar(255) | YES | | NULL |
| ml_id | varchar(32) | YES | | NULL |
| cd_id | varchar(32) | YES | | NULL |
| is_excluded | tinyint(1) | YES | | NULL |
| is_class_excluded | tinyint(1) | YES | | NULL |
| allowed_atc4_codes_json | longtext | YES | | NULL |
| class / class_1 / class_2 | varchar(255) | YES | | NULL |
| molecule | varchar(255) | YES | | NULL |
| dosage_form | varchar(255) | YES | | NULL |
| strength_pack | longtext | YES | | NULL |
| nhi_type / ox_gx / fish_oil | varchar(255) | YES | | NULL |
| 판매사 | varchar(255) | YES | | NULL |
| 제조사 | varchar(255) | YES | | NULL |
| source_file_version | varchar(512) | YES | | NULL |
| ingested_at | datetime(6) | YES | | NULL |
| is_jw | tinyint(1) | YES | | NULL |
| is_target | tinyint(1) | YES | | NULL |
| canonical_name | varchar(255) | YES | | NULL |
| general_brand_key | varchar(255) | YES | | NULL |
| strategy_id | varchar(32) | YES | | NULL |
| catalog_manifest_hash | char(64) | YES | | NULL |

> 참고: 심층분석 500 회귀 이력과 관련해, 서빙 프로젝션에는 존재하지 않는 `is_target` 컬럼을 `SELECT *`로 노출했다가 오류가 발생한 사례가 있다(테이블에는 `is_jw`·`is_target` 모두 실재). 다운스트림은 `is_jw` 기준으로 동작.

### 2.2 일반뷰 mart

일반뷰 3종 + 필터차원. 빌드 주체는 ETL 스테이지 `pipeline/etl/stages/s4_mart.py` → `pipeline/etl/io/mart/general_compute.py`(brand/market), 필터차원은 `pipeline/etl/io/mart/filter_dimension_metric.py`. API는 이 마트를 직접 읽거나 캐시로 서빙한다.

#### mart_general_market_metric — 일반뷰 시장(ATC4) 지표
행수 2,880. UNIQUE `uq_general_market`(atc4_code, source, measure).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| atc4_code | varchar(16) | NO | UQ, IDX | |
| atc4_desc | varchar(255) | YES | | NULL |
| source | varchar(16) | NO | UQ, IDX | |
| measure | varchar(32) | NO | UQ, IDX | |
| unit_label | varchar(32) | NO | | |
| market_size_series | longtext (json) | NO | | |
| hhi_series | longtext (json) | NO | | |
| brand_ranking | longtext (json) | NO | | |
| company_ranking_stacked | longtext (json) | NO | | |
| company_concentration_trend | longtext (json) | NO | | |
| ei_ms_matrix | longtext (json) | NO | | |
| growth_contribution_ms_matrix | longtext (json) | NO | | |
| growth_contribution | longtext (json) | NO | | |
| analysis_levels | longtext (json) | NO | | |
| level_top5_trend | longtext (json) | NO | | |
| target_customer_competition | longtext (json) | NO | | |
| payload | longtext (json) | YES | | NULL |
| computation_version | varchar(16) | YES | | 'v3' |
| computed_at | timestamp | YES | | current_timestamp() |

#### mart_general_brand_metric — 일반뷰 브랜드 지표
행수 114,898. UNIQUE `uq_general_brand`(brand_key, atc4_code, source, measure).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| brand_key | varchar(255) | NO | UQ, IDX | |
| brand_name | varchar(255) | NO | | |
| atc4_code | varchar(16) | NO | UQ, IDX | |
| atc4_desc | varchar(255) | YES | | NULL |
| source | varchar(16) | NO | UQ, IDX | |
| measure | varchar(32) | NO | UQ, IDX | |
| unit_label | varchar(32) | NO | | |
| metric_history | longtext (json) | NO | | |
| extended_metric_history | longtext (json) | NO | | |
| channel_data | longtext (json) | NO | | |
| specialty_data | longtext (json) | NO | | |
| channel_specialty_matrix | longtext (json) | YES | | NULL |
| dimension_data | longtext (json) | NO | | |
| dimension_channel_data | longtext (json) | NO | | |
| by_dimension | longtext (json) | NO | | |
| raw_value_history | longtext (json) | NO | | |
| payload | longtext (json) | YES | | NULL |
| computation_version | varchar(16) | YES | | 'v3' |
| computed_at | timestamp | YES | | current_timestamp() |
| audit_code_matrix | longtext (json) | YES | | NULL |

#### mart_general_filter_dimension_metric — 일반뷰 필터차원 지표 (분석레벨/필터옵션 원천)
행수 973,908. UNIQUE `uq_filter_dimension`(source, measure, atc4_code, brand_key, product_code, dimension_type, dimension_value_hash). 생성 주체: `pipeline/etl/io/mart/filter_dimension_metric.py`. API가 직접 읽는 차원 원천(`pipeline/scripts/api/dynamic_market/filter_options.py`, `response_cache.py`)이며, 일반뷰 분석레벨 이중계상 수정의 실제 대상 테이블이다.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| source | varchar(16) | NO | UQ, IDX | |
| measure | varchar(32) | NO | UQ, IDX | |
| atc4_code | varchar(16) | NO | UQ, IDX | |
| brand_key | varchar(255) | NO | UQ, IDX | |
| brand_name | varchar(255) | NO | | |
| product_code | varchar(255) | NO | UQ | |
| dimension_type | varchar(64) | NO | UQ, IDX | |
| dimension_value | text | NO | | |
| dimension_value_norm | text | NO | | |
| dimension_value_hash | char(64) | NO | UQ, IDX | |
| raw_value_history | longtext (json) | NO | | |
| computed_at | timestamp | YES | | current_timestamp() |

인덱스: `idx_filter_lookup`, `idx_filter_atc_brand`, `idx_filter_option`, `idx_filter_norm_prefix`, `idx_general_option_universe`, `idx_general_atc_scope`, `idx_general_brand_scope`.

#### mart_analysis_level_block — 분석레벨 블록 (사전빌드)
행수 0 (캡처 시점 비어 있음; f096v10 이후 미채움 상태). 생성 주체: `pipeline/scripts/etl/build_analysis_level_blocks.py`.
PK(view, market_id, source, measure, profile_sig, trim_mode). CHECK: view∈{general, strategic_ml, strategic_cd}, source∈{UBIST, IQVIA}, measure∈{sales, volume, unit, counting_unit, dosage_unit}.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| view | varchar(32) | NO | PK | |
| market_id | varchar(64) | NO | PK | |
| source | varchar(16) | NO | PK | |
| measure | varchar(32) | NO | PK | |
| profile_sig | char(64) ascii_bin | NO | PK | '' |
| trim_mode | varchar(8) | NO | PK | 'full' |
| analysis_levels_json | longtext (json) | NO | | |
| analysis_level_market_status_json | longtext (json) | NO | | |
| payload_sha256 | char(64) ascii_bin | NO | | |
| source_epoch | char(64) ascii_bin | NO | IDX | |
| build_version | varchar(128) | NO | IDX | |
| payload_size | int(10) unsigned | NO | | |
| built_at | datetime(6) | NO | IDX | |

### 2.3 캐시 서빙층

`cache_*` 계열은 `pipeline/etl/io/cache/schema.py`가 DDL 정본(단, `cache_deep_analysis_ai_analysis`는 예외 — 아래 명기). mart/agent3/forecast 산출을 응답 형태로 미리 조립해 저빙한다.

> **제거 예정 명기**: `cache_cause`, `cache_deep_analysis`, `cache_deep_analysis_general`, `cache_deep_analysis_ai_analysis` 4종은 제거 예정 계열이다. 신규 서빙 경로는 `cache_dynamic_market_response`(동적 조립) 및 mart 직독으로 이관 중이므로, 납품 후 유지보수 대상에서 제외 예정임을 유의한다.

#### cache_dynamic_market_response — 동적 시장 응답 캐시 (핵심 서빙 캐시)
행수 339. 생성 주체: `pipeline/etl/io/cache/schema.py` (+ `pipeline/scripts/deploy/sql/cache_dynamic_market_response.sql`). lease 기반 빌드 상태·eviction 관리.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| cache_key | char(64) | NO | PK | |
| namespace | varchar(32) | NO | | 'dynamic' |
| request_json | longtext (json) | NO | | |
| source_epoch | char(64) | NO | | |
| state | enum('building','ready','failed') | NO | IDX | |
| lease_owner | varchar(64) | YES | | NULL |
| lease_expires_at | datetime | YES | IDX | NULL |
| response_json | longtext (json) | YES | | NULL |
| response_sha256 | char(64) | YES | | NULL |
| payload_size | int(10) unsigned | YES | | NULL |
| expires_at | datetime | YES | IDX | NULL |
| hit_count | bigint(20) unsigned | NO | IDX | 0 |
| last_hit_at | datetime | YES | IDX | NULL |
| created_at | datetime | NO | | |
| updated_at | datetime | NO | IDX | |
| failure_reason | varchar(255) | YES | | NULL |
| attempt_count | int(10) unsigned | NO | | 0 |
| last_error | text | YES | | NULL |
| last_attempt_at | datetime | YES | | NULL |

인덱스: `idx_dynamic_response_expiry`(state,expires_at), `idx_dynamic_response_lease`(state,lease_expires_at), `idx_dynamic_response_eviction`(state,hit_count,last_hit_at,updated_at), `idx_dynamic_response_namespace_eviction`.

#### cache_brand_elements — 브랜드 요소/강도 캐시
행수 26,411. 생성 주체: `pipeline/etl/io/cache/schema.py`. `agent3_brand_strength` 파생.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| brand_key | varchar(255) | NO | PK | |
| brand_name | varchar(255) | NO | | |
| brand_name_compact | varchar(255) | NO | IDX | |
| factors_json | longtext (json) | NO | | |
| strength_json | longtext (json) | NO | | |
| strength_generated_at | datetime | YES | | NULL |
| strength_workflow_rev | varchar(64) | YES | | NULL |
| updated_at | timestamp | NO | IDX | current_timestamp() ON UPDATE current_timestamp() |
| source_computed_at | timestamp | YES | | NULL |
| expires_at | timestamp | YES | IDX | NULL |

#### cache_market_forecast_general — 일반뷰 시장예측 캐시
행수 2,880. 생성 주체: `pipeline/etl/io/cache/schema.py`. `deep_forecast_*` 파생. PK(atc4_code, source, measure).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| atc4_code | varchar(16) | NO | PK | |
| source | varchar(32) | NO | PK | |
| measure | varchar(32) | NO | PK | |
| market_forecast_json | longtext (json) | NO | | |
| payload_size | int(11) | NO | | |
| source_row_count | int(11) | NO | | |
| source_computed_at | timestamp | YES | | NULL |
| expires_at | timestamp | YES | IDX | NULL |
| updated_at | timestamp | NO | | current_timestamp() ON UPDATE current_timestamp() |
| is_stale | tinyint(1) | NO | | 0 |
| stale_reason | varchar(255) | YES | | NULL |
| stale_marked_at | timestamp | YES | | NULL |

#### cache_brands — 브랜드 목록 스냅샷 캐시
행수 1. 생성 주체: `pipeline/etl/io/cache/schema.py`. 단일 `query_key`로 전 브랜드 응답 스냅샷 저장.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| query_key | varchar(255) | NO | PK | |
| response_json | longtext (json) | NO | | |
| payload_size | int(11) | NO | | |
| updated_at | timestamp | NO | | current_timestamp() ON UPDATE current_timestamp() |
| build_sha | varchar(64) | YES | | NULL |
| input_manifest_json | longtext | YES | | NULL |

#### cache_market_status — 시장 상태 스냅샷 캐시
행수 1. 생성 주체: `pipeline/etl/io/cache/schema.py`. 컬럼 구성은 `cache_brands`와 동일(query_key PK, response_json, payload_size, updated_at, build_sha, input_manifest_json).

#### cache_cause — 원인분석 캐시 (제거 예정)
행수 168. 생성 주체: `pipeline/etl/io/cache/schema.py`, 빌드 `pipeline/scripts/etl/build_cache_cause.py`. PK(brand, view_type, source, measure, market_id).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| brand | varchar(255) | NO | PK, IDX | |
| view_type | varchar(30) | NO | PK | |
| source | varchar(10) | NO | PK | |
| measure | varchar(20) | NO | PK | |
| market_id | varchar(20) | NO | PK, IDX | |
| response_json | longtext (json) | NO | | |
| payload_size | int(11) | NO | | |
| updated_at | timestamp | NO | | current_timestamp() ON UPDATE current_timestamp() |

#### cache_deep_analysis — 심층분석 캐시 (제거 예정)
행수 4,695. 생성 주체: `pipeline/etl/io/cache/schema.py`, 빌드 `pipeline/scripts/etl/build_cache_deep_analysis.py`. PK(brand).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| brand | varchar(255) | NO | PK | |
| market_id | varchar(20) | NO | IDX | |
| response_json | longtext (json) | NO | | |
| payload_size | int(11) | NO | | |
| brand_factors | longtext (json) | YES | | NULL |
| updated_at | timestamp | NO | | current_timestamp() ON UPDATE current_timestamp() |

#### cache_deep_analysis_general — 일반뷰 심층분석 캐시 (제거 예정)
행수 34,378. 생성 주체: `pipeline/etl/io/cache/schema.py`. PK(brand_key, atc4_code).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| brand_key | varchar(255) | NO | PK | |
| brand | varchar(255) | NO | IDX | |
| atc4_code | varchar(16) | NO | PK, IDX | |
| market_id | varchar(32) | NO | IDX | |
| response_json | longtext (json) | NO | | |
| payload_size | int(11) | NO | | |
| brand_factors | longtext (json) | YES | | NULL |
| updated_at | timestamp | NO | | current_timestamp() ON UPDATE current_timestamp() |
| source_computed_at | timestamp | YES | | NULL |
| expires_at | timestamp | YES | IDX | NULL |
| is_stale | tinyint(1) | NO | | 0 |
| stale_reason | varchar(255) | YES | | NULL |
| stale_marked_at | timestamp | YES | | NULL |

#### cache_deep_analysis_ai_analysis — 심층분석 AI 텍스트 캐시 (제거 예정)
행수 24,789. 생성 주체: `pipeline/scripts/ai_analysis/stage3a7_create_and_insert_ai_analysis.py` (cache/schema.py 아님). PK(brand). short/long 각각의 워크플로 메타 컬럼을 보유.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| brand | varchar(255) | NO | PK | |
| brand_key | varchar(255) | YES | | NULL |
| market_id | varchar(20) | YES | | NULL |
| ai_analysis_json | longtext | YES | | NULL |
| ai_analysis_short_json | longtext | YES | | NULL |
| ai_analysis_long_json | longtext | YES | | NULL |
| updated_at | datetime | YES | | current_timestamp() ON UPDATE current_timestamp() |
| short_workflow_id / short_workflow_revision_id | int(11) | YES | | NULL |
| short_generation_id | varchar(255) | YES | | NULL |
| short_input_hash | char(64) | YES | | NULL |
| short_generated_at | datetime(6) | YES | | NULL |
| short_source_epoch | varchar(255) | YES | | NULL |
| short_generation_status | varchar(32) | YES | | NULL |
| long_workflow_id / long_workflow_revision_id | int(11) | YES | | NULL |
| long_generation_id | varchar(255) | YES | | NULL |
| long_input_hash | char(64) | YES | | NULL |
| long_generated_at | datetime(6) | YES | | NULL |
| long_source_epoch | varchar(255) | YES | | NULL |
| long_generation_status | varchar(32) | YES | | NULL |

### 2.4 전략뷰 mart

전략뷰 ML/CD 4종 + 필터차원. 빌드 주체는 `pipeline/etl/stages/s5_mart.py` → `pipeline/etl/io/mart/strategic_ml.py` / `strategic_cd.py`, 브랜드-분자 교량은 `molecule_bridge_build.py`. 필터차원은 `strategic_filter_dimension_metric.py`.

#### mart_strategic_ml_market_metric — 전략 ML 시장 지표
행수 56. UNIQUE `uq_ml_market`(ml_id, source, measure).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| ml_id | varchar(32) | NO | UQ | |
| ml_name | varchar(255) | YES | | NULL |
| source | varchar(16) | NO | UQ | |
| measure | varchar(32) | NO | UQ | |
| unit_label | varchar(32) | NO | | |
| market_size_series | longtext (json) | NO | | |
| hhi_series_5y | longtext (json) | NO | | |
| brand_ranking_stacked | longtext (json) | NO | | |
| company_ranking_stacked | longtext (json) | NO | | |
| company_concentration_trend | longtext (json) | NO | | |
| ei_ms_matrix | longtext (json) | NO | | |
| growth_contribution_ms_matrix | longtext (json) | NO | | |
| growth_contribution | longtext (json) | NO | | |
| analysis_levels | longtext (json) | YES | | NULL |
| level_top5_trend | longtext (json) | YES | | NULL |
| target_customer_competition | longtext (json) | YES | | NULL |
| payload | longtext (json) | YES | | NULL |
| computation_version | varchar(16) | YES | | 'v3' |
| computed_at | timestamp | YES | | current_timestamp() |

#### mart_strategic_ml_brand_metric — 전략 ML 브랜드 지표
행수 14,328. UNIQUE `uq_ml_brand`(ml_id, brand_id, source, measure).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| ml_id | varchar(32) | NO | UQ | |
| brand_id | varchar(255) | NO | UQ | |
| brand_key | varchar(255) | NO | IDX | |
| brand_name | varchar(255) | NO | IDX | |
| source | varchar(16) | NO | UQ, IDX | |
| measure | varchar(32) | NO | UQ, IDX | |
| is_jw | tinyint(1) | YES | | NULL |
| unit_label | varchar(32) | NO | | |
| metric_history | longtext (json) | NO | | |
| extended_metric_history | longtext (json) | NO | | |
| channel_data | longtext (json) | NO | | |
| specialty_data | longtext (json) | NO | | |
| dimension_data | longtext (json) | NO | | |
| dimension_channel_data | longtext (json) | NO | | |
| dimension_specialty_data | longtext (json) | YES | | NULL |
| by_dimension | longtext (json) | NO | | |
| raw_value_history | longtext (json) | NO | | |
| overlay_data | longtext (json) | YES | | NULL |
| payload | longtext (json) | YES | | NULL |
| computation_version | varchar(16) | YES | | 'v3' |
| computed_at | timestamp | YES | | current_timestamp() |
| ubist_channel_by_display | longtext (json) | YES | | NULL |
| ubist_channel_by_code | longtext (json) | YES | | NULL |

#### mart_strategic_cd_market_metric — 전략 CD 시장 지표
행수 64. UNIQUE `uq_cd_market`(cd_market_id, source, measure).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| cd_market_id | varchar(32) | NO | UQ | |
| cd_market_name | varchar(255) | YES | | NULL |
| source | varchar(16) | NO | UQ | |
| measure | varchar(32) | NO | UQ | |
| unit_label | varchar(32) | NO | | |
| market_size_series | longtext (json) | NO | | |
| hhi_series_5y | longtext (json) | NO | | |
| brand_ranking_stacked | longtext (json) | NO | | |
| company_ranking_stacked | longtext (json) | NO | | |
| company_concentration_trend | longtext (json) | NO | | |
| ei_ms_matrix | longtext (json) | NO | | |
| growth_contribution_ms_matrix | longtext (json) | NO | | |
| growth_contribution | longtext (json) | NO | | |
| analysis_levels | longtext (json) | YES | | NULL |
| level_top5_trend | longtext (json) | YES | | NULL |
| target_customer_competition | longtext (json) | YES | | NULL |
| payload | longtext (json) | YES | | NULL |
| computation_version | varchar(16) | YES | | 'v3' |
| computed_at | timestamp | YES | | current_timestamp() |

#### mart_strategic_cd_brand_metric — 전략 CD 브랜드 지표
행수 4,976. UNIQUE `uq_cd_brand`(cd_market_id, cd_brand_id, source, measure).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| cd_market_id | varchar(32) | NO | UQ | |
| cd_brand_id | varchar(255) | NO | UQ | |
| brand_key | varchar(255) | NO | IDX | |
| brand_name | varchar(255) | NO | IDX | |
| source | varchar(16) | NO | UQ, IDX | |
| measure | varchar(32) | NO | UQ, IDX | |
| is_jw | tinyint(1) | YES | | NULL |
| unit_label | varchar(32) | NO | | |
| metric_history | longtext (json) | NO | | |
| extended_metric_history | longtext (json) | NO | | |
| channel_data | longtext (json) | NO | | |
| specialty_data | longtext (json) | NO | | |
| dimension_data | longtext (json) | NO | | |
| dimension_channel_data | longtext (json) | NO | | |
| by_dimension | longtext (json) | NO | | |
| raw_value_history | longtext (json) | NO | | |
| cd_overlay | longtext (json) | YES | | NULL |
| overlay_data | longtext (json) | YES | | NULL |
| payload | longtext (json) | YES | | NULL |
| computation_version | varchar(16) | YES | | 'v3' |
| computed_at | timestamp | YES | | current_timestamp() |
| ubist_channel_by_display | longtext (json) | YES | | NULL |
| ubist_channel_by_code | longtext (json) | YES | | NULL |

#### mart_strategic_filter_dimension_metric — 전략뷰 필터차원 지표
행수 108,860. 생성 주체: `pipeline/etl/io/mart/strategic_filter_dimension_metric.py`. PK(id), 유니크키 없음(중복 방지는 인덱스 기반 조회).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| market_kind | varchar(8) | NO | IDX | |
| market_id | varchar(32) | NO | IDX | |
| brand_id | varchar(255) | NO | IDX | |
| brand_key | varchar(255) | NO | | |
| brand_name | varchar(255) | NO | | |
| source | varchar(16) | NO | IDX | |
| measure | varchar(32) | NO | IDX | |
| unit_label | varchar(32) | YES | | NULL |
| product_code | varchar(255) | NO | IDX | |
| product_name | varchar(512) | YES | | NULL |
| dimension_type | varchar(64) | NO | IDX | |
| dimension_value | text | NO | | |
| dimension_value_norm | text | NO | | |
| dimension_value_hash | char(64) | NO | IDX | |
| raw_value_history | longtext | NO | | |
| computed_at | timestamp | YES | | current_timestamp() |

인덱스: `idx_scope`, `idx_brand`, `idx_options`, `idx_product`.

### 2.5 브랜드-분자/별칭

#### mart_brand_molecule — 브랜드↔분자 교량
행수 58,395. 생성 주체: `pipeline/etl/io/mart/molecule_bridge_schema.py`(DDL) / `molecule_bridge_build.py`(적재). UNIQUE `uq_brand_molecule`(brand_key, atc4_code, mart_source, molecule_norm).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| brand_key | varchar(255) | NO | UQ, IDX | |
| brand_name | varchar(255) | NO | | |
| atc4_code | varchar(16) | NO | UQ, IDX | '' |
| mart_source | varchar(16) | NO | UQ, IDX | 'any' |
| molecule_norm | varchar(255) | NO | UQ, IDX | |
| molecule_display | varchar(255) | NO | | |
| molecule_raw_examples | longtext (json) | NO | | |
| evidence_scopes | longtext (json) | NO | | |
| evidence_count | int(11) | NO | | |
| component_count | int(11) | NO | | |
| is_combo_component | tinyint(1) | NO | | 0 |
| computed_at | timestamp | YES | | current_timestamp() |

#### brand_alias — 브랜드 별칭 매핑
행수 1,688. 생성 주체: `pipeline/scripts/agent3/sql/005_create_brand_alias.sql`(DDL) / `006_seed_brand_alias.sql`(시드). PK(alias_name).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| alias_name | varchar(255) | NO | PK | |
| brand_key | varchar(255) | NO | IDX | |
| alias_type | varchar(32) | NO | | |
| alias_sources | varchar(64) | YES | | NULL |
| note | varchar(255) | YES | | NULL |
| created_at | datetime | NO | | |

### 2.6 Agent3 브랜드 강도

생성 주체: `pipeline/scripts/agent3/sql/*.sql` (001/004/005 create). 일 1회 `jw-agent3-refresh-daily` CronJob이 갱신.

#### agent3_brand_strength — 브랜드 강도(전역)
행수 25,153. PK(brand_key). UNIQUE `uq_agent3_brand_strength_serving_brand_name`(serving_brand_name).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| brand_key | varchar(255) | NO | PK | |
| brand_name | varchar(255) | NO | IDX | |
| serving_brand_name | varchar(255) | YES | UQ | NULL |
| profile_json | longtext (json) | NO | | |
| strength_candidates_json | longtext (json) | NO | | |
| strength_summary_json | longtext (json) | NO | | |
| workflow_id | int(11) | NO | | |
| workflow_rev | int(11) | NO | | |
| input_hash | char(64) | NO | IDX | |
| generated_at | datetime | NO | IDX | |

#### agent3_brand_strength_source — 브랜드 강도(소스별)
행수 35,521. PK(brand_key, source).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| brand_key | varchar(255) | NO | PK | |
| source | varchar(16) | NO | PK | |
| brand_name | varchar(255) | NO | | |
| serving_brand_name | varchar(255) | YES | IDX | NULL |
| profile_json | longtext (json) | NO | | |
| strength_candidates_json | longtext (json) | NO | | |
| strength_summary_json | longtext (json) | NO | | |
| workflow_id | int(11) | NO | | |
| workflow_rev | int(11) | NO | | |
| input_hash | char(64) | NO | | |
| generated_at | datetime | NO | IDX | |

#### agent3_brand_strength_market — 브랜드 강도(시장별)
행수 7,706. PK(brand_key, source, market_id). CHECK: source∈{iqvia, ubist}, view_kind/market_id 정합(ml↔ml_%, cd↔cd_%).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| brand_key | varchar(255) | NO | PK | |
| source | varchar(16) | NO | PK | |
| market_id | varchar(32) | NO | PK | |
| view_kind | varchar(32) | NO | IDX | |
| brand_name | varchar(255) | NO | | |
| serving_brand_name | varchar(255) | YES | IDX | NULL |
| profile_json | longtext (json) | NO | | |
| strength_candidates_json | longtext (json) | NO | | |
| strength_summary_json | longtext (json) | NO | | |
| workflow_id | int(11) | NO | | |
| workflow_rev | int(11) | NO | | |
| input_hash | char(64) | NO | | |
| generation_status | varchar(32) | NO | IDX | |
| generated_at | datetime | NO | IDX | |

### 2.7 예측(forecast)

생성 주체: `pipeline/scripts/etl/ops_forecast_store.py` (+ `ops_forecast_builder.py`, `migrate_unified_forecast_tables.py`). 전략·일반 예측을 통합 적재.

#### deep_forecast_block — 브랜드 예측 블록
행수 43,474. PK(brand_key, source, market_id). CHECK: source∈{iqvia_nsa, ubist}, simulation_available↔simulation_json 정합, view_kind∈{market_landscape(ml_%), competitive_dynamics(cd_%), general(그 외)}.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| brand_key | varchar(255) | NO | PK | |
| source | varchar(16) | NO | PK | |
| market_id | varchar(64) | NO | PK | |
| view_kind | varchar(32) | NO | | |
| forecast_json | longtext (json) | NO | | |
| simulation_json | longtext (json) | YES | | NULL |
| generation_status | varchar(64) | YES | | NULL |
| no_history_fallback | longtext (json) | YES | | NULL |
| simulation_available | tinyint(1) | NO | | |
| source_epoch | varchar(64) | NO | | |
| source_computed_at | datetime | YES | | NULL |
| generated_at | datetime | NO | | (payload.generated_at 우선, 없으면 소스 캐시 updated_at) |

#### deep_forecast_horizon — 시장 예측 지평
행수 3,000. PK(market_id, source, measure). CHECK: source∈{iqvia_nsa, ubist}, view_kind 정합.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| market_id | varchar(64) | NO | PK | |
| source | varchar(16) | NO | PK | |
| measure | varchar(32) | NO | PK | |
| view_kind | varchar(32) | NO | | |
| forecast_horizon_json | longtext (json) | NO | | |
| source_row_count | int(11) | NO | | |
| source_epoch | varchar(64) | NO | | |
| source_computed_at | datetime | YES | | NULL |
| generated_at | datetime | NO | | (동일 fallback) |

### 2.8 원천 적재 (raw ingest)

#### iqvia_nsa_quarterly_raw — IQVIA NSA 분기 원천
행수 891,567. 생성 주체: `pipeline/etl/io/iqvia_loader.py` (`NSA_TABLE`). PK(id).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| source_file | varchar(255) | NO | | |
| sheet_name | varchar(255) | YES | | NULL |
| source_row_no | int(11) | NO | | |
| audit_code | varchar(64) | YES | IDX | NULL (IQVIA AUDIT CODE) |
| audit_desc | varchar(512) | YES | | NULL |
| mfr_code | varchar(64) | YES | IDX | NULL |
| mfr_name | varchar(255) | YES | | NULL |
| period_yyyy | smallint(6) | YES | IDX | NULL |
| period_quarter | tinyint(4) | YES | IDX | NULL |
| period_label | varchar(32) | YES | | NULL |
| payload | longtext (json, utf8mb4_bin) | NO | | |
| source_master_version | varchar(255) | YES | | NULL |
| ingested_at | timestamp | YES | | current_timestamp() |

### 2.9 뉴스·이벤트 파이프라인

원천 뉴스→이벤트 추출→브랜드 스코어링 흐름. 생성 주체: 크롤러/피처 `pipeline/scripts/crawler/*`, `pipeline/scripts/etl/phase29_events.py`.

#### news_raw — 원천 뉴스
행수 35,484. 생성 주체: 크롤러 파이프라인(기본 DDL) + tier 보존 정책 `pipeline/scripts/crawler/sql/001_news_tier_retention.sql`. PK(news_id).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| news_id | varchar(64) | NO | PK | |
| source_name | varchar(50) | NO | IDX | |
| title | text | NO | | |
| article_url | text | YES | | NULL |
| article_text | longtext | YES | | NULL |
| raw_html | longtext | YES | | NULL |
| published_date | date | YES | IDX | NULL |
| search_keyword | varchar(255) | YES | | NULL |
| ingested_at | datetime | YES | | current_timestamp() |
| corpus_file_path | varchar(1000) | YES | | NULL |
| matched_search_keywords | longtext (json) | YES | | NULL |
| matched_jw_search_contexts | longtext (json) | YES | | NULL |
| news_source_file | varchar(500) | YES | | NULL |
| scored | tinyint(1) | NO | IDX | 0 |
| scored_at | timestamp | YES | IDX | NULL |
| tier | tinyint(4) | NO | IDX | 1 |
| collected_at | datetime | NO | IDX | current_timestamp() |
| expire_at | datetime | YES | IDX | NULL |
| collection_provenance | longtext (json) | YES | | NULL |
| legacy_news_ids | longtext (json) | YES | | NULL |

#### events_raw — 원천 이벤트 텍스트
행수 35,484. 생성 주체: `pipeline/scripts/etl/phase29_events.py`. PK(news_id).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| news_id | varchar(64) | NO | PK | |
| source_name | varchar(100) | YES | IDX | NULL |
| published_date | date | YES | IDX | NULL |
| title | text | YES | | NULL |
| summary | text | YES | | NULL |
| body | longtext | YES | | NULL |
| url | text | YES | | NULL |
| created_at | datetime | YES | | NULL |
| ingested_at | datetime | YES | | NULL |

#### events — 추출 이벤트
행수 35,484. 생성 주체: `pipeline/scripts/etl/phase29_events.py`. PK(event_id), news_id로 news_raw 참조(논리).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| event_id | varchar(64) | NO | PK | |
| news_id | varchar(64) | NO | IDX | |
| category | varchar(50) | YES | IDX | NULL |
| category_label | varchar(50) | YES | | NULL |
| date | date | YES | IDX | NULL |
| title | text | YES | | NULL |
| summary | text | YES | | NULL |
| body_full | longtext | YES | | NULL |
| source_name | varchar(50) | YES | | NULL |
| source_url | text | YES | | NULL |
| period_ubist | varchar(20) | YES | | NULL |
| period_iqvia | varchar(20) | YES | | NULL |
| processed_by | varchar(50) | YES | | NULL |
| processed_at | datetime | YES | | NULL |
| search_keyword | varchar(255) | YES | | NULL |
| tier | tinyint(4) | NO | IDX | 1 |
| collected_at | datetime | NO | IDX | current_timestamp() |
| expire_at | datetime | YES | IDX | NULL |

#### event_brand_scores — 이벤트-브랜드 스코어
행수 71,240. 생성 주체: `pipeline/scripts/crawler/tier2_full_scoring_runner.py`. PK(id), UNIQUE `uq_event_brand`(event_id, brand_canonical).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| event_id | varchar(64) | NO | UQ | |
| brand_name | varchar(255) | NO | | |
| brand_canonical | varchar(255) | YES | UQ, IDX | NULL |
| brand_id | varchar(64) | YES | | NULL |
| ml_id | varchar(20) | YES | IDX | NULL |
| cd_id | varchar(20) | YES | | NULL |
| is_jw | tinyint(4) | YES | | 0 |
| score | int(11) | NO | | (CHECK 0~100) |
| score_tier | varchar(30) | YES | | NULL |
| reason | text | YES | | NULL |
| source_processor | varchar(50) | YES | | NULL |
| generated_at | datetime | YES | | current_timestamp() |
| news_id | varchar(64) | YES | IDX | NULL |
| derivation | enum('llm_direct','cross_match','manual') | NO | IDX | 'llm_direct' |
| mirrored_from_jw_brands | longtext (json, utf8mb4_bin) | YES | | NULL |
| tag | varchar(50) | YES | | NULL |
| summary | text | YES | | NULL |
| workflow_id | int(11) | YES | IDX | 196 |
| catalog_version | varchar(64) | YES | IDX | NULL |
| llm_meta | longtext (json, utf8mb4_bin) | YES | | NULL |
| tier | tinyint(4) | NO | IDX | 1 |
| collected_at | datetime | NO | IDX | current_timestamp() |
| expire_at | datetime | YES | IDX | NULL |

#### tier2_match_staging — 티어2 본문매칭 스테이징
행수 23,964. 생성 주체: `pipeline/scripts/crawler/tier2_body_match_runner.py`. PK(run_id, news_id, brand_key). 날짜 suffix가 없는 상시 운영 스테이징 테이블이므로 정본으로 분류.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| run_id | varchar(64) | NO | PK | |
| news_id | varchar(64) | NO | PK, IDX | |
| brand_key | varchar(255) | NO | PK, IDX | |
| brand_canonical | varchar(255) | NO | | |
| match_source | varchar(64) | NO | IDX | |
| matched_keywords | longtext (json) | NO | | |
| created_at | datetime | NO | | current_timestamp() |

### 2.10 AI 분석 산출 (zeta)

생성 주체: `pipeline/scripts/ai_analysis/phase_zeta_runner/output_composer.py` (+ `stage3a7_create_and_insert_ai_analysis.py`).

#### zeta_analysis_runs — 분석 실행 이력
행수 26,893. PK(run_id), UNIQUE `uq_brand_snapshot_variant`(brand, snapshot_at, analysis_variant).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| run_id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| brand | varchar(255) | NO | UQ, IDX | |
| snapshot_at | datetime | NO | UQ | |
| analysis_variant | varchar(16) | NO | UQ | 'legacy' |
| config_version | varchar(64) | NO | | |
| builder_version | varchar(64) | NO | | |
| bundle_hash | varchar(80) | NO | IDX | |
| model_version | varchar(64) | YES | | NULL |
| status | varchar(32) | NO | IDX | |
| total_tokens_in | int(11) | YES | | NULL |
| total_tokens_out | int(11) | YES | | NULL |
| cost_usd | decimal(10,6) | YES | | NULL |
| duration_sec | decimal(8,2) | YES | | NULL |
| input_bundle | longtext | YES | | NULL |
| error_log | text | YES | | NULL |
| created_at | timestamp | YES | | current_timestamp() |

#### zeta_analysis_outputs — 분석 산출물(스테이지별)
행수 107,572. PK(output_id), UNIQUE `uq_run_stage`(run_id, stage). **물리 FK** `zeta_analysis_outputs_ibfk_1`: run_id → zeta_analysis_runs(run_id) ON DELETE CASCADE.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| output_id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| run_id | bigint(20) | NO | UQ, FK | |
| stage | varchar(20) | NO | UQ | |
| title | varchar(500) | YES | | NULL |
| body | text | YES | | NULL |
| bullets | longtext (json, utf8mb4_bin) | YES | | NULL |
| raw_response | longtext | YES | | NULL |
| validated | tinyint(1) | YES | | 0 |
| validation_log | longtext | YES | | NULL |
| tokens_in | int(11) | YES | | NULL |
| tokens_out | int(11) | YES | | NULL |
| created_at | timestamp | YES | | current_timestamp() |

### 2.11 브랜드활동 stage DB (`jw_brand_activity_stage`)

브랜드활동(채널 다이내믹스·키워드 이벤트·토픽 분류) 전용 스키마. 7개 오브젝트 중 6개 테이블 + 1개 뷰.

#### csd_channel_dynamics_stage — 채널 다이내믹스 스테이지
행수 49,894. 생성 주체: `pipeline/scripts/etl/brand_activity/ingest_csd.py`. PK(period_ym, market, jw_channel, master_product, representing_company).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| period_ym | char(7) | NO | PK | |
| market | varchar(128) | NO | PK, IDX | |
| jw_channel | varchar(32) | NO | PK | |
| master_product | varchar(255) | NO | PK, IDX | |
| representing_company | varchar(255) | NO | PK | |
| product_details | int(11) | NO | | |
| source_file | varchar(255) | NO | | |
| source_sheet | varchar(128) | NO | | |
| source_row_no | int(11) | NO | | |
| loaded_at | timestamp | NO | | current_timestamp() |

#### km_keyword_event_stage — 키워드 이벤트 스테이지
행수 66,556. 생성 주체: `pipeline/scripts/etl/brand_activity/ingest_keyword_stage.py`. PK(id).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) unsigned | NO | PK, AUTO_INCREMENT | |
| period_ym | char(7) | NO | IDX | |
| visit_location | varchar(255) | NO | | |
| specialty | varchar(255) | NO | | |
| representing_company | varchar(255) | NO | | |
| product_name | varchar(255) | NO | IDX | |
| therapeutic_class | varchar(64) | NO | IDX | |
| keyword_text | longtext | NO | | |
| interest | varchar(64) | NO | | |
| prescription_frequency | varchar(128) | NO | | |
| prescription_evolution | varchar(128) | NO | | |
| abstract_lit / patient_lit / promotional_lit / samples_left / other_materials_left | varchar(16) | NO | | |
| what_other_materials | text | NO | | |
| other_comments | text | NO | | |
| source_file | varchar(255) | NO | IDX | |
| source_sheet | varchar(64) | NO | IDX | |
| source_row_no | int(11) | NO | IDX | |
| source_file_sha256 | char(64) | NO | | |
| stage_row_sha256 | char(64) | NO | | |
| loaded_at | timestamp | NO | | current_timestamp() |

#### mart_brand_activity_topics — 브랜드활동 토픽
행수 11. 생성 주체: `pipeline/scripts/analysis/brand_activity/auto_topic/topic_store_db.py` (+ `row_topic_sql.py`). PK(scope_id).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| scope_id | varchar(128) | NO | PK | |
| display_name | varchar(255) | NO | | |
| atc4_values | longtext (json, utf8mb4_bin) | NO | | |
| quality_grade | varchar(8) | NO | | |
| source_row_count | int(11) | NO | | |
| payload | longtext (json, utf8mb4_bin) | NO | | |
| run_id | varchar(160) | NO | IDX | |
| updated_at | timestamp | NO | | current_timestamp() ON UPDATE current_timestamp() |

#### mart_brand_activity_topic_runs — 토픽 실행 이력
행수 4. 생성 주체: `pipeline/scripts/analysis/brand_activity/auto_topic/topic_store_db.py`. PK(run_id).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| run_id | varchar(160) | NO | PK | |
| created_at | datetime | NO | | |
| model_id | varchar(128) | NO | | |
| serving_id | varchar(32) | NO | | |
| route | varchar(64) | NO | | |
| total_prompt_tokens | bigint(20) | NO | | |
| total_completion_tokens | bigint(20) | NO | | |
| est_cost_usd | decimal(12,4) | NO | | |
| market_count | int(11) | NO | | |
| brand_count | int(11) | NO | | |
| axis_compound_count | int(11) | NO | | |
| brand_specific_dup_count | int(11) | NO | | |
| sha256 | char(64) | NO | | |
| input_fingerprint | char(64) | YES | | NULL |
| updated_at | timestamp | NO | | current_timestamp() ON UPDATE current_timestamp() |

#### row_topic_assignment — 행-토픽 배정
행수 172,419. 생성 주체: `pipeline/scripts/analysis/brand_activity/auto_topic/row_topic_sql.py`. PK(row_id, topic_id, topic_set_version).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| row_id | bigint(20) unsigned | NO | PK | |
| scope_id | varchar(128) | NO | IDX | |
| brand | varchar(255) | NO | IDX | |
| topic_id | varchar(128) | NO | PK, IDX | |
| topic_set_version | varchar(128) | NO | PK, IDX | |
| prompt_version | varchar(64) | NO | | |
| assigned_at | datetime | NO | | current_timestamp() |
| batch_id | varchar(160) | NO | | |

#### row_topic_assignment_status — 행-토픽 배정 상태
행수 119,178. 생성 주체: `pipeline/scripts/analysis/brand_activity/auto_topic/row_topic_sql.py`. PK(topic_set_version, scope_id, row_id).

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| topic_set_version | varchar(128) | NO | PK, IDX | |
| scope_id | varchar(128) | NO | PK | |
| row_id | bigint(20) unsigned | NO | PK | |
| stage_row_sha256 | char(64) | NO | | |
| prompt_version | varchar(64) | NO | | |
| batch_id | varchar(192) | NO | IDX | |
| status | varchar(32) | NO | IDX | |
| assignment_count | int(11) | NO | | 0 |
| classified_at | datetime | NO | | current_timestamp() |

#### row_topic_assignment_share_view — 행-토픽 배정 공유 뷰 (VIEW)
캡처 시점 반환 행수 1,639. **테이블이 아닌 VIEW**(`SHOW CREATE TABLE`이 CREATE 정의를 반환하지 않음). 정의 주체: `pipeline/scripts/analysis/brand_activity/auto_topic/row_topic_sql.py`. 서빙 편의를 위한 파생 조회 뷰이며 물리 컬럼 저장은 없다.

---

## 3. 백업/작업용 오브젝트 (납품 스키마 아님)

아래 오브젝트는 마이그레이션·컷오버·백업·실패 롤백용으로 남은 것이며 납품 스키마 정의에 포함되지 않는다. 컬럼 구성은 각 원본 정본 테이블과 동일하거나 이력 스냅샷이다.

### jw_mart_d2_stage_20260630_r2 (55건)

| # | 테이블 | 패턴 | 행수 | 대응 정본 |
|---|---|---|---|---|
| 1 | _cutover_news_map_final_843_20260707_152039 | _cutover_ | 35,474 | 뉴스 컷오버 매핑 |
| 2 | _cutover_score_pick_final_843_20260707_152039 | _cutover_ | 59,240 | 스코어 픽 컷오버 |
| 3 | agent3_brand_strength_source_bak_profileonly_20260711 | _bak_ | 35,521 | agent3_brand_strength_source |
| 4 | agent3_brand_strength_source_bak_taxonomy_20260710 | _bak_ | 35,521 | agent3_brand_strength_source |
| 5 | cache_brands_old_f096v10_0715a | _old_ | 1 | cache_brands |
| 6–10 | cache_cause_bak_freshness_* (2026071014… ×3, _r2_ ×2) | _bak_ | 168 각 | cache_cause |
| 11 | cache_deep_analysis_ai_analysis_bak_pre_promotion_20260710 | _bak_ | 279 | cache_deep_analysis_ai_analysis |
| 12 | cache_deep_analysis_ai_analysis_bak_pre_short_long_20260712 | _bak_ | 23,331 | cache_deep_analysis_ai_analysis |
| 13 | cache_deep_analysis_ai_analysis_stage_general_sample_20260712 | _stage_ | 18 | cache_deep_analysis_ai_analysis |
| 14 | cache_deep_analysis_ai_analysis_stage_short_long_20260712 | _stage_ | 349 | cache_deep_analysis_ai_analysis |
| 15 | cache_deep_analysis_audit_p0_stage_20260710 | _stage_ | 4,695 | cache_deep_analysis |
| 16 | cache_deep_analysis_backup_audit_p0_20260710_1340 | _backup_ | 4,695 | cache_deep_analysis |
| 17–26 | cache_deep_analysis_bak_d2_prev3_* (2026-07-09 ~ 07-16, 10건) | _bak_ | 4,695 각 | cache_deep_analysis |
| 27 | cache_deep_analysis_general_backup_pre_rng_20260712 | _backup_ | 597 | cache_deep_analysis_general |
| 28 | cache_market_forecast_general_backup_pre_rng_20260712 | _backup_ | 830 | cache_market_forecast_general |
| 29–33 | cache_market_status_bak_freshness_* (×3, _r2_ ×2) | _bak_ | 1 각 | cache_market_status |
| 34 | deep_forecast_block_stage_ops_20260713 | _stage_ops_ | 43,474 | deep_forecast_block |
| 35 | deep_forecast_horizon_stage_ops_20260713 | _stage_ops_ | 3,000 | deep_forecast_horizon |
| 36 | event_brand_scores_bak_precutover_20260707_155418 | _bak_ | 60,555 | event_brand_scores |
| 37 | event_brand_scores_bak_tier2_exact_20260708_005132 | _bak_ | 13,202 | event_brand_scores |
| 38 | event_brand_scores_bak_v3rescore_20260704_pre5347 | _bak_ | 33,066 | event_brand_scores |
| 39 | event_brand_scores_mig_stg_20260707093653 | _mig_stg_ | 60,555 | event_brand_scores |
| 40 | event_brand_scores_tier2_staging_20260707_105528 | _staging_(날짜) | 23,964 | event_brand_scores |
| 41 | events_bak_precutover_20260707_155418 | _bak_ | 35,474 | events |
| 42 | events_mig_stg_20260707093653 | _mig_stg_ | 34,631 | events |
| 43 | events_raw_bak_precutover_20260707_155418 | _bak_ | 35,474 | events_raw |
| 44 | events_raw_mig_stg_20260707093653 | _mig_stg_ | 34,631 | events_raw |
| 45 | mart_analysis_level_block_old_f096v10_0715a | _old_ | 4,525 | mart_analysis_level_block |
| 46 | mart_general_filter_dimension_metric__backup_dimfix_20260716 | __backup_ | (백업) | mart_general_filter_dimension_metric |
| 47 | mart_general_filter_dimension_metric__backup_f124a_20260715 | __backup_ | (백업) | mart_general_filter_dimension_metric |
| 48 | mart_strategic_cd_brand_metric__failed_f116_0715b | __failed_ | 4,976 | mart_strategic_cd_brand_metric |
| 49 | mart_strategic_cd_market_metric__failed_f116_0715b | __failed_ | 64 | mart_strategic_cd_market_metric |
| 50 | mart_strategic_ml_brand_metric__failed_f116_0715b | __failed_ | 14,328 | mart_strategic_ml_brand_metric |
| 51 | mart_strategic_ml_market_metric__failed_f116_0715b | __failed_ | 56 | mart_strategic_ml_market_metric |
| 52 | news_mig_component_stg_20260707093653 | _mig_..._stg_ | 34,631 | 뉴스 컷오버 컴포넌트 |
| 53 | news_mig_map_stg_20260707093653 | _mig_..._stg_ | (매핑) | 뉴스 컷오버 매핑 |
| 54 | news_raw_bak_precutover_20260707_155418 | _bak_ | 35,474 | news_raw |
| 55 | news_raw_mig_stg_20260707093653 | _mig_stg_ | 34,631 | news_raw |

### jw_brand_activity_stage
백업/작업용 없음 (7개 오브젝트 전부 정본).

---

## 4. 증분 적재 훅 원장: `ingest_ledger` (2026-07-17 활성화로 생성됨)

증분 적재 훅(ingest hook)의 멱등성 락 및 상태 소스인 `ingest_ledger`는 **2026-07-17 활성화로 운영 DB `jw_mart_d2_stage_20260630_r2`에 생성됨**(재실측 2026-07-17 09:54 UTC 기준). 현재 리허설 격리 모드로 운용 중이며 캡처 시점 행수 3(AUTO_INCREMENT=8). 초기 스키마 캡처(`db_schema_dump.txt`, 91 오브젝트) 이후 생성되어 0절 총계에는 미포함이나, 정본 테이블로 분류한다.

DDL 정본은 `pipeline/scripts/ingest_hook/ledger.py`의 `_DDL_MYSQL`(mysql/MariaDB 분기). 아래 컬럼표는 운영 DB 재실측 DDL 기준이다. PK(id), UNIQUE `uq_ledger_identity`(epoch, category, manifest_sha), InnoDB / utf8mb4_unicode_ci.

| 컬럼 | 타입 | NULL | 키 | 기본값 |
|---|---|---|---|---|
| id | bigint(20) | NO | PK, AUTO_INCREMENT | |
| epoch | varchar(32) | NO | UQ | |
| category | varchar(32) | NO | UQ, IDX | |
| manifest_sha | char(64) | NO | UQ | |
| manifest_path | varchar(512) | NO | | |
| uploaded_by | varchar(128) | YES | | NULL |
| status | varchar(16) | NO | IDX | |
| reason | text | YES | | NULL |
| job_name | varchar(128) | YES | | NULL |
| run_id | varchar(64) | YES | | NULL |
| row_counts | text | YES | | NULL |
| received_at | datetime | NO | | |
| started_at | datetime | YES | | NULL |
| finished_at | datetime | YES | | NULL |

인덱스: UNIQUE `uq_ledger_identity`(epoch, category, manifest_sha), KEY `idx_ledger_category_status`(category, status).

**코드-실물 일치 판정**: 운영 DB 재실측 DDL은 코드 정본 `_DDL_MYSQL`(4절 상단 파일)과 컬럼명·타입·NULL 제약·유니크키·보조인덱스가 완전 일치(불일치 0).

용도: (epoch, category, manifest_sha) 유니크로 동일 매니페스트 중복 처리를 차단하고, `status`(queued/started/…)로 증분 적재 작업의 단일 진실원을 유지한다. 코드는 `sqlite`/`mysql` 두 방언을 지원하며, 운영은 mysql 분기를 사용한다.

---

## 5. 테이블 생성 주체 매핑 (grep 근거)

| 정본 테이블 | 생성/빌드 주체 (파일 경로) |
|---|---|
| catalog_ml_market / catalog_cd_market / catalog_strategic_brand | pipeline/etl/io/catalog/db_sync.py (`_create_catalog_table` / `sync_catalog_tables`) |
| mart_general_brand_metric / mart_general_market_metric | pipeline/etl/stages/s4_mart.py → pipeline/etl/io/mart/general_compute.py |
| mart_general_filter_dimension_metric | pipeline/etl/io/mart/filter_dimension_metric.py |
| mart_analysis_level_block | pipeline/scripts/etl/build_analysis_level_blocks.py |
| mart_strategic_ml_market_metric / mart_strategic_ml_brand_metric | pipeline/etl/stages/s5_mart.py → pipeline/etl/io/mart/strategic_ml.py |
| mart_strategic_cd_market_metric / mart_strategic_cd_brand_metric | pipeline/etl/stages/s5_mart.py → pipeline/etl/io/mart/strategic_cd.py |
| mart_strategic_filter_dimension_metric | pipeline/etl/io/mart/strategic_filter_dimension_metric.py |
| mart_brand_molecule | pipeline/etl/io/mart/molecule_bridge_schema.py (DDL) / molecule_bridge_build.py (적재) |
| brand_alias | pipeline/scripts/agent3/sql/005_create_brand_alias.sql / 006_seed_brand_alias.sql |
| agent3_brand_strength / _source / _market | pipeline/scripts/agent3/sql/001·004·005_*.sql |
| cache_brands / cache_brand_elements / cache_market_status / cache_market_forecast_general / cache_cause / cache_deep_analysis / cache_deep_analysis_general / cache_dynamic_market_response | pipeline/etl/io/cache/schema.py (cache_dynamic_market_response는 pipeline/scripts/deploy/sql/cache_dynamic_market_response.sql 병기) |
| cache_deep_analysis_ai_analysis | pipeline/scripts/ai_analysis/stage3a7_create_and_insert_ai_analysis.py |
| deep_forecast_block / deep_forecast_horizon | pipeline/scripts/etl/ops_forecast_store.py (+ ops_forecast_builder.py, migrate_unified_forecast_tables.py) |
| iqvia_nsa_quarterly_raw | pipeline/etl/io/iqvia_loader.py |
| news_raw | 크롤러 파이프라인 기본 DDL + pipeline/scripts/crawler/sql/001_news_tier_retention.sql |
| events_raw / events | pipeline/scripts/etl/phase29_events.py |
| event_brand_scores | pipeline/scripts/crawler/tier2_full_scoring_runner.py |
| tier2_match_staging | pipeline/scripts/crawler/tier2_body_match_runner.py |
| zeta_analysis_runs / zeta_analysis_outputs | pipeline/scripts/ai_analysis/phase_zeta_runner/output_composer.py (+ stage3a7_create_and_insert_ai_analysis.py) |
| csd_channel_dynamics_stage | pipeline/scripts/etl/brand_activity/ingest_csd.py |
| km_keyword_event_stage | pipeline/scripts/etl/brand_activity/ingest_keyword_stage.py |
| mart_brand_activity_topics / mart_brand_activity_topic_runs | pipeline/scripts/analysis/brand_activity/auto_topic/topic_store_db.py (+ row_topic_sql.py) |
| row_topic_assignment / row_topic_assignment_status / row_topic_assignment_share_view(VIEW) | pipeline/scripts/analysis/brand_activity/auto_topic/row_topic_sql.py |
| ingest_ledger (2026-07-17 생성) | pipeline/scripts/ingest_hook/ledger.py (`_DDL_MYSQL`) |

---

## 6. 데이터 흐름 (raw → etl → mart → cache/응답)

```
[원천 입력]
  UBIST 엑셀 ─┐
  IQVIA 엑셀 ─┼─→ iqvia_loader.py ─→ iqvia_nsa_quarterly_raw
  카탈로그 엑셀 ─→ catalog/db_sync.py ─→ catalog_ml_market / catalog_cd_market / catalog_strategic_brand
  뉴스 크롤 ─→ news_raw ─→ phase29_events.py ─→ events_raw / events
                                                └→ tier2_body_match_runner ─→ tier2_match_staging
                                                └→ tier2_full_scoring_runner ─→ event_brand_scores
  브랜드활동 엑셀 ─→ ingest_csd.py / ingest_keyword_stage.py ─→ csd_channel_dynamics_stage / km_keyword_event_stage

[ETL → MART]
  s4_mart.py (general_compute) ─→ mart_general_market_metric / mart_general_brand_metric
  filter_dimension_metric.py   ─→ mart_general_filter_dimension_metric
  s5_mart.py (strategic_ml/cd) ─→ mart_strategic_{ml,cd}_{market,brand}_metric
  strategic_filter_dimension_metric.py ─→ mart_strategic_filter_dimension_metric
  molecule_bridge_build.py     ─→ mart_brand_molecule
  agent3 SQL/CronJob           ─→ agent3_brand_strength{,_source,_market}
  ops_forecast_store.py        ─→ deep_forecast_block / deep_forecast_horizon
  auto_topic/topic_store_db.py ─→ mart_brand_activity_topics{,_topic_runs} / row_topic_assignment{,_status}

[MART → CACHE/응답]
  cache/schema.py 빌더         ─→ cache_brands / cache_market_status / cache_brand_elements /
                                   cache_market_forecast_general / cache_cause(제거예정) / cache_deep_analysis*(제거예정)
  동적 응답 조립               ─→ cache_dynamic_market_response (lease 기반 building→ready)
  API 직독                     ─→ mart_general_filter_dimension_metric (필터옵션/분석레벨), mart_* 각 지표

[AI 분석]
  zeta_runner ─→ zeta_analysis_runs / zeta_analysis_outputs
  stage3a7    ─→ cache_deep_analysis_ai_analysis (제거예정)
```

재적재·dedup 지점 (코드 근거):
- **카탈로그 재동기화**: `catalog/db_sync.py`의 `sync_catalog_tables()`가 매니페스트 해시(`catalog_manifest_hash`) 기준으로 재적재.
- **일반뷰 필터차원 재적재**: `mart_general_filter_dimension_metric`은 `uq_filter_dimension` 유니크로 중복 차원을 흡수하며, dimfix/f124a 백업(3절 46·47)은 재적재 스냅샷.
- **예측 통합 적재**: `ops_forecast_store.py`가 staging(`_stage_ops_*`)에 적재 후 정본으로 승격(RENAME/스왑).
- **증분 적재 멱등**: `ingest_ledger`(2026-07-17 생성, 리허설 격리 모드)의 `uq_ledger_identity`(epoch, category, manifest_sha)가 dedup 락 — 운영 mysql 분기 사용.
- **뉴스 컷오버 dedup**: `_cutover_*` / `_mig_stg_*`는 2026-07-07 뉴스 ID 컷오버·컴포넌트 병합 이력(정본 아님).
