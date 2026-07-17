# JW Market Backend API — 엔드포인트 전수 목록

- 코드 기준: develop worktree `761b4def` — `/tmp/jwm-develop-docs/pipeline/scripts/api/`
- 라우터 등록: `main.py:80-88` (health, brands, market_status, cause, deep_analysis, dynamic_market, market_filter, market_scope, brand_activity)
- 운영: deployment `jw-market-backend-api` (ns=llmops, 8 replicas), 컨테이너 포트 8000, service `jw-market-backend-api-service:80→8000`, `EXTERNAL_PATH_PREFIX=/jw-market-backend-api`(root_path, 실경로는 프리픽스 없이 `/api/...`).
- `include_in_schema=False` = OpenAPI 문서 비노출(내부/디버그·mock 별칭). 경로 자체는 유효.

## 전수 표 (파일:라인)

| # | 메서드 | 경로 | 핸들러 | 파라미터 / 바디 | 응답 | 정의 위치 | 문서노출 |
|---|--------|------|--------|-----------------|------|-----------|----------|
| 1 | GET | `/` | `serve_frontend` | 없음 | `FileResponse` (mockup HTML `jw_market_hardcoded_mockup_v3_4.html`) | `main.py:95` | 비노출 |
| 1b| GET | `/jw-market-backend-api` 및 `/jw-market-backend-api/` | `serve_frontend` | 없음 | HTML (프리픽스 존재 시 추가 등록) | `main.py:100-102` | 비노출 |
| 2 | GET | `/api/health` | `health` | 없음 | dict `{status, markets_loaded, brands_loaded, version}` | `routes/health.py:13` | 노출(META) |
| 3 | GET | `/api/market-status` | `market_status` | 없음 | dict (cache_market_status.response_json; 404 cache_not_found) | `routes/market_status.py:13` | 노출 |
| 4 | GET | `/api/brands` | `list_brands` | `q`:str? (완전일치), `query`:str? (BFF 호환 검색), `market_id`:str?, `limit`:int=20 (1~50) | list[dict]. 검색 시 X-Has-More/X-Total-Matches/X-Result-Limit 헤더. q≠query 시 422 | `routes/brands.py:130` | 노출 |
| 5 | GET | `/api/cause/{brand_name}` | `cause` | path `brand_name`; query `view`="market_landscape"(or competitive_dynamics), `source`="UBIST", `measure`="sales", `market_id`:str? | dict 23섹션 payload. 미존재 404 brand_not_found, 과부하 429 | `routes/cause.py:120` | 노출 |
| 6 | GET | `/api/deep-analysis/{brand_name}` | `deep_analysis` | path `brand_name`; query `view`?(general/strategic), `view_kind`?(general/strategic_ml/strategic_cd), `market_id`?, `source`?(ubist/iqvia). legacy view와 formal(view_kind…) 병용 시 422, atc4 쿼리 422 | dict 심층분석 payload. 404 brand_not_found | `routes/deep_analysis.py:1367` | 노출 |
| 7 | POST | `/api/dynamic-market` | `dynamic_market` | body `DynamicMarketRequest` {view?, filters{atc4[], view_kind?, focus_brand_key?, analysis_level{ubist,iqvia}}, source="ubist", measure="sales", options{period_range}} | success_envelope/cause 계약 dict. scope 초과 400 dynamic_scope_too_broad, 429 overloaded, 422 invalid view | `routes/dynamic_market.py:64` | 노출 |
| 8 | GET | `/api/dynamic-market/filter-options` | `dynamic_market_filter_options` | `view`="general"(general/strategic/strategic_cd), `source`="ubist", `measure`="sales", `brand`?, `atc4_codes`:list?, `selections`:json-str?, `market_id`?(deprecated) | dict 필터 옵션. 400 invalid_request | `routes/dynamic_market.py:412` | 노출 |
| 9 | GET | `/api/dynamic-market/brand-option-check` | `dynamic_market_brand_option_check` | `brand`:str(필수), `view`="general", `source`="ubist" | dict (filter-options 계약 + brand_matched). 400 | `routes/dynamic_market.py:475` | 노출 |
| 10 | GET | `/api/market-filter/atc-options` | `market_filter_atc_options_get` | `brand_name`?, `view`="general"(general/strategic), `source`="ubist"(ubist/iqvia) | `MarketFilterAtcOptionsResponse` (ATC1~4 key/level/parent/flag). 400 invalid | `routes/market_filter.py:16` | 노출 |
| 11 | GET | `/api/market-scope/options` | `options` | `brand`:str(필수), `view_family`="strategy", `source`?(없으면 미필터) | dict {brand, view_family, source, options[], catalog_version}. general은 501 | `routes/market_scope.py:34` | 비노출 |
| 12 | POST | `/api/market-scope/resolve` | `resolve` | body `MarketScopeResolveRequest` {brand, view_family="strategy", source="UBIST", measure="sales", option_ids[≥1]} | dict resolved scope + disjoint 진단. 400 invalid, 409 overlap | `routes/market_scope.py:56` | 비노출 |
| 13 | POST | `/api/market-scope/cause` | `cause` | body `MarketScopeCauseRequest` (resolve + view="market_landscape") | dict `{status:SUCCESS, result}`. 400/409 | `routes/market_scope.py:69` | 비노출 |
| 14 | GET | `/api/brand-activity/csd-presence` | `brand_activity_csd_presence` | `brand`? 또는 `brands`?(쉼표, ≤50). 정확히 하나 필수(else 422) | `CsdPresence` 또는 list | `routes/brand_activity.py:69` | 노출 |
| 15 | GET | `/api/brand-activity/topics` | `brand_activity_topics` | 없음 | dict `{data:[…topic market payloads]}` | `routes/brand_activity.py:96` | 비노출(디버그) |
| 16 | GET | `/api/brand-activity/topics/{scope_id}` | `brand_activity_topic` | path `scope_id` (예: `atc4:A02B2`) | dict `{data}` 또는 scope_not_found | `routes/brand_activity.py:105` | 비노출(디버그) |
| 17 | POST | `/api/brand-activity/topics` | `brand_activity_topic_matrix` | body `BrandActivityTopicsRequest` {view="general", selected_brand(필수), filters(MarketFilter: atc/analysis_level/channel/market_scope), visit_location, specialty, interest, prescription_evolution, start_date/end_date, top_n=5(1~10)} | dict `{data, meta}`. 400/404 market_not_found/500 | `routes/brand_activity.py:117,121` | 노출(+mock 별칭) |
| 17b| POST | `/jw-brand-activity-mock/api/brand-activity/topics` | (동일 핸들러) | 위와 동일 | 동일 | `routes/brand_activity.py:117` | 비노출(mock 별칭) |
| 18 | POST | `/api/brand-activity/csd-timeseries` | `brand_activity_csd_timeseries` | body `CsdTimeseriesRequest` (base + market_id?, csd_market?, mode="absolute"(absolute/share), window{start,end}) | dict `{data}`. 422 invalid_csd_market, no_csd_mapping/ambiguous는 data.available=false, 404 | `routes/brand_activity.py:179` | 노출(+mock 별칭) |
| 18b| POST | `/jw-brand-activity-mock/api/brand-activity/csd-timeseries` | (동일) | 동일 | 동일 | `routes/brand_activity.py:175` | 비노출(mock 별칭) |
| 19 | POST | `/api/brand-activity/csd-activity-series` | `brand_activity_csd_activity_series` | body `CsdActivitySeriesRequest` {view, selected_brand, filters, entity_level="brand"(brand/company), csd_channel="TOTAL", csd_market?, selected_entities[≤6], period{start,end}} | dict `{data}`. 400/422/404 | `routes/brand_activity.py:236` | 노출 |
| 20 | POST | `/api/brand-activity/interest-rx-matrix` | `brand_activity_interest_rx_matrix` | body `BrandActivityInterestRxRequest` (base + visit_location, specialty, period_start/end, weights) | dict `{data}`. 400/404 | `routes/brand_activity.py:278` | 노출(+mock 별칭) |
| 20b| POST | `/jw-brand-activity-mock/api/brand-activity/interest-rx-matrix` | (동일) | 동일 | 동일 | `routes/brand_activity.py:274` | 비노출(mock 별칭) |

정적 마운트(라우트 아님, 참고): `/static` 및 `{prefix}/static` (`main.py:90-92`, StaticFiles).

## 별도 서비스 — ingest_hook (운영 미배포/미활성)

- 코드: `pipeline/scripts/ingest_hook/app.py`. 실행 진입점 `uvicorn --factory pipeline.scripts.ingest_hook.app:build --port 8080`.
- 설계상 jw-market-backend-api와 프로세스/pod/엔드포인트 분리(STOP ①). 운영 미배포 → 실호출 불가.

| 메서드 | 경로 | 핸들러 | 파라미터/바디 | 응답 | 정의 위치 |
|--------|------|--------|---------------|------|-----------|
| GET | `/healthz` | `healthz` | 없음 | `{ok:true}` | `ingest_hook/app.py:89` |
| POST | `/ingest/webhook` | `webhook` | body `{manifest_path:str}` | `{epoch, category, manifest_sha, decision, status, reason, job_name}`. 400/422/409 | `ingest_hook/app.py:93` |
| GET | `/ingest/status` | `status` | query `epoch`, `category`, `manifest_sha` (전부 필수) | `{epoch, category, manifest_sha, status, reason, job_name, uploaded_by, received_at, finished_at}`. 404 | `ingest_hook/app.py:97` |
| POST | `/ingest/reconcile` | `reconcile` | 없음 | `{launched:{category:job_name}}` | `ingest_hook/app.py:114` |
