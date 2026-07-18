# DOC-2b DB 문서 — 크롤 · brand_activity 테이블

| 항목 | 값 |
|---|---|
| 기준 코드(develop) SHA | `1864e929` (초판 DOC-2 기준 `7ca98403`에서 전진; DOC-1b 각주와 동일 계열) |
| 대상 DB | `jw_mart_d2_stage_20260630_r2`(크롤/생성), `jw_brand_activity_stage`·`jw_brand_activity_raw_stage`(브랜드활동) |
| 캡처일 | 2026-07-18 |
| 문서 버전 | v1.0 |
| 근거 디렉토리 | `docs/delivery/evidence/doc2b_schema_capture.json`(COUNT+컬럼 실측), `doc1b_capture.md` |

> **형식·경계.** 컬럼 타입·인덱스 원문(SHOW CREATE)은 초판 **DOC-2**를 정본으로 참조하고, 본 문서는 크롤/BA 소관 테이블의 **"무엇을 담는지·어디서 오는지·행수·갱신 주기·관계"**에 집중한다(README §3). 컬럼은 실측 목록(`information_schema.COLUMNS`)으로 열거하되 타입은 DOC-2 참조. 행수는 전부 `COUNT(*)` 실측(TABLE_ROWS 미사용).
> **접근 권한 주의.** `jw_brand_activity_stage`/`_raw_stage`는 `jw_mart_d2_writer` 계정 권한 밖이라 본 실측은 root 계정(secret `galera-mariadb-galera`/`mariadb-root-password`)으로 수행했다. BA CronJob도 root 사용(DOC-1b §2.4).

---

## 1. 크롤 계열 테이블 (`jw_mart_d2_stage_20260630_r2`)

| 테이블 | COUNT(*) | 용도 | 생성 주체 | 갱신 주기 |
|---|---|---|---|---|
| news_raw | 35,507 | 크롤 원문 기사(제목·본문·URL·published_date·tier·검색 키워드·수집 provenance) | `crawl_2tier.py`→`corpus_loader.py` | 매일(tier1 18:10 / tier2 18:40 UTC) |
| events_raw | 35,507 | 이벤트 표시용 정규화 기사(title·summary·body·url) — 서빙 이벤트 조인 대상 | `tier2_full_scoring_runner.py sync-events-raw` | 크롤 동반 |
| events | 35,507 | 이벤트(category·category_label·date·period_ubist/iqvia·processed_by) | tier1 스코어 + `refresh-live-categories`(category 갱신) | 크롤 동반 |
| event_brand_scores | 71,318 | 이벤트↔브랜드 스코어(brand_canonical·score·tag·source_processor·derivation) — 브랜드별 뉴스 노출의 원천 | `tier2_full_scoring_runner.py append-live`(wf337) | 크롤 동반 |
| tier2_match_staging | 23,964 | tier2 매칭 중간 산출(run_id·news_id·brand_key·matched_keywords) — **정본 아님(작업 테이블)** | tier2 러너 매칭 단계 | 크롤 시 재생성 |

**컬럼(실측, 타입은 DOC-2 참조)**:
- `news_raw`: news_id, source_name, title, article_url, article_text, raw_html, published_date, search_keyword, ingested_at, corpus_file_path, matched_search_keywords, matched_jw_search_contexts, news_source_file, scored, scored_at, tier, collected_at, expire_at, collection_provenance, legacy_news_ids.
- `events`: event_id, news_id, **category, category_label**(§DOC-1b §1.4 refresh 대상), date, title, summary, body_full, source_name, source_url, period_ubist, period_iqvia, **processed_by, processed_at**, search_keyword, tier, collected_at, expire_at.
- `event_brand_scores`: id, event_id, brand_name, **brand_canonical, score, score_tier, tag, source_processor, derivation**, mirrored_from_jw_brands, news_id, workflow_id, catalog_version, llm_meta, tier, collected_at, expire_at. (정렬 정책은 DOC-4c §4.)
- `events_raw`: news_id, source_name, published_date, title, summary, body, url, created_at, ingested_at.
- `tier2_match_staging`: run_id, news_id, brand_key, brand_canonical, match_source, matched_keywords, created_at.

**source_processor 값 계보**(`tier2_full_scoring_runner.py:30-33`): tier1 = `workflow_196_optionB`/`workflow_196_rev5674`/`cross_match_adapter_v1`, tier2 exact = `tier2_exact_rule_v1`, tier2 LLM = `tier2_llm_v1`(확정)/`tier2_llm_v2_rev5671`(pending 승격).

---

## 2. 토픽 / brand_activity 계열 (`jw_brand_activity_stage`)

| 테이블 | COUNT(*) | 용도 | 생성 주체 | 갱신 주기 |
|---|---|---|---|---|
| csd_channel_dynamics_stage | 49,894 | CSD(ChannelDynamics) 정규화 stage(period_ym·market·jw_channel·master_product·product_details) | `etl/brand_activity/ingest_csd.py` | CSD 파일 도착 시(수시) |
| km_keyword_event_stage | 66,556 | Keyword(의사 메시지) stage(specialty·product_name·interest·처방 지표 등) | `auto_topic` 계열 적재(`data_source.py:19`) | Keyword 파일 도착 시 |
| mart_brand_activity_topics | 11 | **서빙 토픽**(scope_id·display_name·atc4_values·quality_grade·payload·run_id) | `auto_topic/run_auto_topic.py`(GenOS) | 월간(topic-monthly) |
| mart_brand_activity_topics_staging | 11 | 위 staging | 동 | 동 |
| mart_brand_activity_topic_runs | 4 | 토픽 실행 이력(model_id·serving_id·토큰·비용·fingerprint) | 동 | 동 |
| mart_brand_activity_topic_runs_staging | 1 | 위 staging | 동 | 동 |
| row_topic_assignment | 172,419 | row 단위 토픽 배정(row_id·scope_id·brand·topic_id·topic_set_version·batch_id) | `auto_topic/row_topic_*`(row-topic-monthly) | 월간 |
| row_topic_assignment_status | 119,178 | 배정 상태(status·assignment_count·stage_row_sha256) | 동 | 동 |
| row_topic_assignment_share_view | 1,639 | 브랜드×토픽 점유(affected_row_count·brand_total_rows·share_pct) — 서빙 집계 | 동 | 동 |
| stg_master_mapping_table | 5,956 | MI Master 매핑(strategic_market_id·source/target) | MI Master 적재 | 수시 |
| stg_master_market_definition | 16 | 전략 시장 정의(market_atc_codes·competition_brands·analysis_levels) | 동 | 수시 |
| csd_channel_dynamics_stage_bak_20260705_151611 | 44,025 | **백업(정본 아님)** | 스냅샷 | — |

**서빙이 읽는 것 vs 집계**: API 브랜드활동(DOC-3 §3.14~)은 `mart_brand_activity_topics`(토픽 카드)·`csd_channel_dynamics_stage`(CSD 시계열/활동)·`row_topic_assignment_share_view`(점유)를 읽는다. `row_topic_assignment`(17만 행)는 row 단위 원천으로, 서빙은 집계본(share_view)을 읽는다.

---

## 3. raw · staging · 중간 산출 (정본 아님 구분)

| 스키마.테이블 | COUNT(*) | 성격 | 수명 |
|---|---|---|---|
| `jw_brand_activity_raw_stage`.raw_csd_channel_dynamics | 324,885 | CSD **원천 raw**(dedup 전, source_row_key·row_hash·raw_payload_json) | 재적재 시 갱신 |
| `jw_brand_activity_raw_stage`.raw_keyword_events | 71,603 | Keyword **원천 raw**(dedup 전) | 동 |
| `jw_brand_activity_stage`.*_staging (topics/runs) | 11 / 1 | 승격 전 staging | 승격 후 교체 |
| `jw_mart_d2_stage…`.tier2_match_staging | 23,964 | tier2 매칭 작업 테이블 | 크롤 재생성 |
| `…_bak_*` | 44,025 | 백업 스냅샷 | 수동 정리 |

- **raw → stage 흐름**: raw(중복 포함, `dedup_key`·`row_hash` 보유) → dedup/선별(`selected_for_stage`) → stage. raw_csd 324,885 → csd stage 49,894(선별·정규화 결과).

---

## 4. 테이블 관계 · 데이터 흐름 (텍스트 ERD)

```
크롤:  news_raw ──1:N── event_brand_scores ──N:1── events ──1:1── events_raw
         (news_id)          (news_id/event_id)        (news_id)      (news_id)
       tier2_match_staging(작업) → append-live → event_brand_scores
       refresh-live-categories: event_brand_scores(tier2-only) → events.category  [DOC-1b §1.4]

brand_activity:
  MinIO(CSD/Keyword xlsx) → raw_csd_channel_dynamics / raw_keyword_events (원천)
       → (dedup·선별) → csd_channel_dynamics_stage / km_keyword_event_stage (stage)
       → auto_topic(GenOS) → mart_brand_activity_topics (+_runs)  ── 서빙 토픽
       → row_topic → row_topic_assignment → _status / _share_view ── 서빙 점유
```

- **조인 키**: 크롤은 `news_id`(news_raw↔events_raw↔event_brand_scores) + `event_id`(events↔scores). BA는 `scope_id`(topics↔row_topic)·`period_ym`(CSD 시계열).
- **dedup 지점**: BA는 raw→stage 사이(`dedup_key`/`row_hash`). 크롤은 tier2 중복 게이트(batch 내 + news_raw 기존 news_id 차집합, DOC-1b §1.1).
- **재적재**: CSD/Keyword는 파일 도착 시 raw 재적재 후 stage 재선별. 크롤은 매일 증분(기존 news_id 제외).

---

## jw market 확인 결과 (2026-07-18 실측)

아래 1~2는 jw market 세션이 실측 해소했다(근거: `evidence/openq_resolution_20260718.md` Q-4~Q-5). 3은 정책 사안으로 `OPEN_QUESTIONS.md`에 등재.

1. **컬럼 타입·인덱스 상호참조** → [DOC-2](DOC-2_DB_스키마정의서.md)가 크롤/BA 테이블을 실제로 담고 있어 참조 유효. 앵커: 크롤 계열 `#### news_raw`·`#### events_raw`·`#### events`·`#### event_brand_scores`(DOC-2 §크롤), BA stage는 [DOC-2 §2.11 브랜드활동 stage DB](DOC-2_DB_스키마정의서.md) 하위 `csd_channel_dynamics_stage`·`km_keyword_event_stage`·`mart_brand_activity_topics`·`mart_brand_activity_topic_runs`·`row_topic_assignment`·`row_topic_assignment_status`·`row_topic_assignment_share_view`(VIEW). 타입/인덱스/제약 원문은 해당 소절 `SHOW CREATE`.
2. **km_keyword_event_stage 적재 스크립트** → 적재 주체 = `pipeline/scripts/etl/brand_activity/ingest_keyword.py`(워크북 파싱, KEYWORD_HEADERS) → `pipeline/scripts/etl/brand_activity/load_raw_staging.py:229-231`(`raw_keyword_events` 적재 + `km_keyword_event_stage` stage 적재). DDL 헬퍼 = `ingest_keyword_stage.py`. ※ 위 §2 표의 `auto_topic/data_source.py:19`는 **소비처**(토픽 생성 읽기)이지 적재 주체가 아니다.
3. **BA 서빙 계정 grant** → **PL/플랫폼 판단 사안**(권한 정책). 서빙 backend는 `jw_brand_activity_stage`를 config 기본으로 읽으나(DOC-1 §2.1) 실측 계정 `jw_mart_d2_writer`는 권한 부재라 BA CronJob·본 실측은 root(secret `galera-mariadb-galera`/`mariadb-root-password`)로 수행 중. writer grant 부여 vs root 현행 유지 결정 필요 → [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md).
