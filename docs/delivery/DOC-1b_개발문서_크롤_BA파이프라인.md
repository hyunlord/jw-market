# DOC-1b 개발 문서 — 크롤 · brand_activity 파이프라인

| 항목 | 값 |
|---|---|
| 기준 코드(develop) SHA | `9c34a7d5` (초판 DOC-1 기준 `7ca98403`에서 전진; 아래 각주) |
| 운영 리소스 | 크롤 `jw-market-crawl@sha256:64bb2b9f…`(tier1/2 라이브), 오케스트레이터 `jw-pipeline-orchestrator@sha256:6bffbc53…`(poll, suspend), agent3 `jw-market-backend-api@sha256:dec3ec3c…` |
| 생성일 | 2026-07-18 |
| 문서 버전 | v1.0 |
| 근거 디렉토리 | `docs/delivery/evidence/` (본 문서 실측: `doc1b_capture.md`) + 초판 공유 evidence(`k8s_cron_svc.txt` 등) |

> **기준 SHA 갱신 각주.** 초판(DOC-1 등)은 `7ca98403` 기준이다. 본 문서 작성 시점 develop HEAD는 **`9c34a7d5`**이며 전진분 3커밋은 ① `e984a057` 오케스트레이터 이미지에 pyarrow+duckdb 추가(ETL load 경로 의존성) ② `e3bafccb` 인입 훅이 그 이미지(`v0.2.4-e984a057`) 참조 ③ `9c34a7d5` 납품 문서(README·스켈레톤·DOC-5 §8 자리) — 세 커밋 모두 본 문서 소관(크롤·BA·orchestrator 코드)을 변경하지 않으며 §4 이미지 절에서 digest 차이만 반영한다. 크롤 cutover(§1.4)의 근거 커밋 `ec4f6e04`는 `9c34a7d5`의 조상으로 확인된다.
> **경계.** 본 문서는 **크롤·brand_activity 생성·오케스트레이터 내부**를 다룬다. 서빙 API·backend·mart 스키마·사이트·전체 구성도는 **DOC-1**(아키텍처)이 이미 다루므로 재서술하지 않고 참조한다(DOC-1 §2.1 서빙, §2.3 오케스트레이터 개요, §2.5 크롤 개요, §2.8 인입 훅). 테이블 스키마(컬럼·인덱스)는 **DOC-2b**로 분리한다 — 본 문서는 "무엇을 하는지·행수"만 다룬다.
> 모든 서술은 실코드 `파일:줄`·실 리소스명·실측 캡처(`evidence/doc1b_capture.md`)에 근거한다. 확인 불가는 `[확인 필요]`로 말미에 모은다.

---

## 1. 뉴스 크롤 (tier1 · tier2)

### 1.1 아키텍처 — 수집원·처리 단계

크롤은 두 단(tier1·tier2)으로 나뉜다. 개요는 DOC-1 §2.5 참조. 여기서는 코드 계보와 단계 차이를 상술한다.

```
[뉴스 원천 사이트] → crawl_2tier.py(tier 인자) → news_raw
                                                    │
   tier1: corpus_loader_v2 --tier 1 → events_raw    │  (규칙 기반 tier1 스코어)
   tier2: corpus_loader_v2 --tier 2 → tier2_match_staging
              → tier2_full_scoring_runner sync-events-raw   → events_raw
              → tier2_full_scoring_runner append-live(wf337 LLM) → event_brand_scores
              → tier2_full_scoring_runner refresh-live-categories → events.category
```

| 항목 | 값 | 근거 |
|---|---|---|
| 크롤 진입 | `crawl/crawler/crawl_2tier.py`(이미지 경로), 빌드 소스 `pipeline/scripts/crawler/crawl_2tier.py` | `deploy/docker/crawl.Dockerfile` |
| 적재 | `crawl/agent1/corpus_loader_v2.py`(빌드 소스 `pipeline/scripts/agent_2/corpus_loader.py`) | crawl.Dockerfile COPY |
| tier2 스코어 러너 | `/opt/tier2/tier2_full_scoring_runner.py`(빌드 소스 `pipeline/scripts/crawler/tier2_full_scoring_runner.py`, 49,549 B) | crawl.Dockerfile |
| tier1↔tier2 | tier1 = 광범위 저비용 수집·규칙 스코어(processor `workflow_196_rev5674` 등), tier2 = 브랜드 정밀 LLM 스코어(processor `tier2_llm_v2_rev5671`) | `tier2_full_scoring_runner.py:30-33` |
| tier2 요일 로테이션 | `stable_weekday_slice(brand_key) = int(sha256(brand_key)[:8],16) % 7` — 매일 1/7·주간 전수(카테고리 아님, 요일 7등분) | `tier2_catalog.py:62-63,189` |
| canonical(강등) 사본 | `*-daily-canonical`·`*-daily-slice-canonical` = cutover 완료로 강등된 **삭제 후보**, `suspend: true`(resume 시 이중 기동 위험) | evidence §A, DOC-1 §2.5 |

`crawl_2tier.py`는 `--tier {1,2}`로 분기하는 공통 진입점이다(`crawl_2tier.py:229` args). tier2는 당일 요일 슬라이스 브랜드만 처리하므로 한 사이클이 전체의 약 1/7이다.

### 1.2 실행 — CronJob·이미지·CM 마운트 (2026-07-18 실측)

| 리소스 | 스케줄 | suspend | 이미지 | 근거 |
|---|---|---|---|---|
| jw-news-crawl-tier1-daily | `10 18 * * *` | false | `jw-market-crawl@sha256:64bb2b9f…` | evidence §A,B |
| jw-news-crawl-tier2-daily-slice | `40 18 * * *` | false | `jw-market-crawl@sha256:64bb2b9f…` | 동 |
| jw-news-crawl-retention-daily | `0 19 * * *` | true | — | 동 §A |
| *-canonical 2종 | 동일 | true(강등) | — | 동 §A |

**★ tier2 CM 마운트 실태**: `jw-news-crawl-tier2-daily-slice`는 ConfigMap `tier2-llm-runner-rev5671`을 `/opt/tier2`에 마운트한다(`volumeMounts` 실측). CM 데이터는 **49,549 B**로 정본 runner(`pipeline/scripts/crawler/tier2_full_scoring_runner.py`, 동일 49,549 B)와 동기화된 신판이다. 동기화는 `deploy/k8s/crawler/apply-tier2-llm-schedule.sh`가 정본 runner에서 CM을 재생성하는 방식으로 유지한다(CM이 이미지 내 runner를 가리므로, CM을 최신 정본과 맞추는 것이 cutover 설계의 핵심). 근거: evidence §B.

### 1.3 스코어링 정책 — cutoff·cap·요일 슬라이스 (실값)

| 정책 | 값 | file:line |
|---|---|---|
| tier2 대상 매출 임계 | 3,000,000,000 KRW(30억) | `tier2_catalog.py:18` |
| 신규 브랜드 최근 창 | 6개월 | `tier2_catalog.py:19` |
| 신규 브랜드 최소 매출 | 100,000,000 KRW(1억) | `tier2_catalog.py:20` |
| 요일 슬라이스 modulo | 7 (brand_key sha256 해시) | `tier2_catalog.py:62` |
| tier2 wf337 워크플로 | id 337 · rev 5671 · deployment 1453 | `tier2_full_scoring_runner.py:27-29` |
| 일일 호출 상한 | `--daily-call-limit` 기본 60 | `tier2_full_scoring_runner.py:1346` |
| 비용 상한 | `--max-cost-krw` 기본 203.40 (콜당 3.39원) | `:1347,:37,:843-847` |
| 배치 크기 / 연속 실패 abort / 타임아웃 | 200 / 3 / 420초 | `:36,:34,:35` |

비용 가드는 `daily_call_limit × 3.39 > max_cost_krw`이면 실행 전 예외(`:843-847`)로 cap 초과 설정 자체를 차단한다.

### 1.4 category refresh — 2026-07-18 cutover 추가 스텝

tier2 category 갱신(`events.category`)은 오래 결손이었다. 근인은 `refresh-live-categories`가 **독립 서브커맨드**인데 `append-live`가 이를 호출하지 않아 **완성된 코드가 실행되지 않은** 것이다(호출 부재).

- **수정 커밋**: develop `ec4f6e04` "Invoke refresh-live-categories after append-live in the tier2 crawl". tier2 manifest body에 `python /opt/tier2/tier2_full_scoring_runner.py refresh-live-categories`를 `append-live` 직후로 추가(`set -euo pipefail` 아래라 스킵 불가·자체 실패 시 Job 실패). 계약 테스트 `tests/crawler/test_crawl_cutover_manifests.py`가 스텝 존재·순서를 pin.
- **갱신 로직**(`tier2_full_scoring_runner.py:1095` `update_live_tier2_categories`): tier2-only 이벤트(processor `tier2_llm_v1`/`tier2_llm_v2_rev5671`, tier1 미처리분)의 tag를 `신약/R&D→rd·자본/경영→capital·정책/규제→policy·공급/생산→supply·기타→external`로 매핑해 `events.category`·`category_label`을 UPDATE.
- **실작동 실측(2026-07-18 자연 기동)**: tier2 로그 `updated_event_categories: 13212`, DB 검증에서 tier2-only 이벤트 13,212건 전부 category 채워짐(`processed_by=tier2_llm_v2_rev5671`, `processed_at` 단일 스탬프). cutover 전엔 미실행이던 스텝이 결손을 해소했다. 상세 증적은 크롤 cutover 라운드 산출(초판 외부)이며 본 문서는 커밋·로그 사실만 인용한다.

---

## 2. 데이터 생성 계열 (Agent1~3 + forecast·elements·brand_activity)

> **명명 주의.** 코드에는 **Agent 1**(뉴스 크롤/tier1 — `pipeline/scripts/etl/phase29_events.py:2,4`, `forecast/sentiment_scorer.py:99`), **Agent 2**(short/long 서사 — `ai_analysis/bundle_builder/event_bundle_builder.py:18`), **Agent 3**(브랜드 강도)이 명시된다. 스켈레톤·의뢰서의 "Agent1~4"에서 **"Agent 4"는 코드 상 명시 정의가 없다** → [확인 필요]. 아래는 실재하는 생성 계열로 서술한다.

### 2.1 계열별 산출 테이블·행수 (COUNT(*), 2026-07-18)

| 계열 | 산출 테이블 | COUNT(*) | 생성 주체(스크립트) |
|---|---|---|---|
| 크롤(Agent1) | news_raw / events / events_raw | 35,507 / 35,507 / 35,507 | `crawl_2tier.py`·`corpus_loader.py` |
| 크롤 스코어 | event_brand_scores | 71,318 | `tier2_full_scoring_runner.py` |
| 크롤 staging | tier2_match_staging | 23,964 | 동(정본 아님) |
| cache | cache_deep_analysis_general | 34,378 | `etl/build_cache_deep_analysis_general.py` |
| cache(events) | cache_deep_analysis | 4,695 | `etl/cache_refresh/cache_deep_analysis_events_update.py` |
| forecast | deep_forecast_block / horizon | 43,474 / 3,000 | `etl/ops_forecast_builder.py` |
| forecast(general) | cache_market_forecast_general | 2,880 | `etl/build_cache_deep_analysis_general.py` |
| strength(Agent3) | agent3_brand_strength / _source | 25,153 / 35,521 | `agent3/run_source.py` |
| elements | cache_brand_elements | 26,411 | `etl/cache_brand_elements.py` |

(brand_activity 계열 테이블·행수는 §2.4 및 DOC-2b.) 근거: evidence §C.

### 2.2 forecast — 결정론 계약

forecast는 LLM을 쓰지 않는 **결정론** 산출이다(prophet 미설치 환경 계약). 재실행 시 byte-동일이 목표다.

| 계약 | 내용 | file:line |
|---|---|---|
| 시드 | `_stable_forecast_seed(identity)` → `np.random.Generator(np.random.PCG64(seed))`, identity=(unit, spec.name, variant, steps, phase) | `forecast_runner.py:65,79-81` |
| 모델 dispatch | `data_size_dispatch_v1`: n_history≥60 prophet_basic → ≥40 SARIMAX(event proxy off) → ≥30 SARIMAX base → 이하 linear/mean fallback | `forecast_runner.py:106,109-133` |
| 엔진 | statsmodels(`sm`, `ExponentialSmoothing`) | `forecast_runner.py:30-31` |
| 캐시 격리 | 스코프 간 `_FORECAST_ENTRY_CACHE`·`_MARKET_FORECAST_CACHE` clear(프로세스 처리 이력 무관 산출 보장) | `forecast_runner.py:59` |
| horizon | 전략 5년(`HORIZON_YEARS=5`), 일반 빌더 horizon_years=10 파라미터 | `strategic_forecast_full_generation.py:57,97` / `build_cache_deep_analysis_general.py:523,547` |
| 게이트 | ops 빌더 EXPECTED_BLOCKS=43,474 · EXPECTED_HORIZONS=3,000 · mart epoch 지문 | `ops_forecast_builder.py:33-34,137` |

### 2.3 strength / short-long / elements — 생성 로직·LLM

| 계열 | 로직 요약 | LLM | 게이트 |
|---|---|---|---|
| strength(Agent3) | mart 프로파일+슬라이스 증거로 브랜드당 강도 1행 생성. `run_source.py` | **wf316**(id 316), rev는 env pin(`5692`)·코드 기본값 없음→env 부재 시 `WorkflowRevNotPinnedError`(fail-closed) | input_hash+rev 매치 시 미호출(신규/변경만 과금) `agent3/config.py:6,9,24` |
| short-long(Agent2) | Phase ζ 번들→wf217 서사 생성. `ai_analysis/agent2_regen_orchestrator.py`(904줄판이 정본) | **wf217** rev 3727, formatter `wf217-order2-v10.3` | 기본 dry-run·`--apply` 별도, hash 무변경 skip `agent2_regen_orchestrator.py:52-53` |
| elements | agent3 strength 행 + 브랜드 factor를 **조립만**(재계산). `cache_brand_elements.py` | **없음**(순수 조립) | 신규 브랜드 스코프 증분, `--dry-run`이 전 write 차단(최상위 우선) |

### 2.4 brand_activity — CSD/Keyword 원천 → 토픽 생성

brand_activity는 별도 DB(`jw_brand_activity_stage`·`jw_brand_activity_raw_stage`)에서 동작한다(격리 스키마). 서빙 연결은 DOC-1 §2.6 및 API `routes/brand_activity.py`(DOC-3 §3.14~) 참조. 상세 테이블은 DOC-2b·유저 맥락은 DOC-4c.

| 원천/산출 | 테이블 | COUNT(*) | 생성 주체 |
|---|---|---|---|
| CSD 원천(raw) | raw_csd_channel_dynamics | 324,885 | MinIO CSD xlsx → `etl/brand_activity/ingest_csd.py` |
| Keyword 원천(raw) | raw_keyword_events | 71,603 | 동 계열 |
| CSD stage | csd_channel_dynamics_stage | 49,894 | `ingest_csd.py:47,150` |
| Keyword stage | km_keyword_event_stage | 66,556 | `auto_topic/data_source.py:19` |
| 토픽(서빙) | mart_brand_activity_topics | 11 | `auto_topic/run_auto_topic.py` |
| 토픽 실행 이력 | mart_brand_activity_topic_runs | 4 | 동 |
| row-topic 배정 | row_topic_assignment / _status / _share_view | 172,419 / 119,178 / 1,639 | `auto_topic/row_topic_*` |

- **CSD 원천**: MinIO `jw-market-raw-iqvia/CSD/ChannelDynamics*.xlsx` → `ingest_csd.py`가 격리 스키마에 stage 적재(운영 mart 무접촉, `ingest_csd.py:2` docstring). 토픽 데이터 소스는 Keyword·CSD stage(`data_source.py:19-20`).
- **토픽 생성 LLM**: `run_auto_topic.py`는 GenOS 호출(`--execute`, `--max-real-calls` 기본 86, `token_env=GENOS_BEARER_TOKEN`) `run_auto_topic.py:101-114`. dry-run은 GenOS 미호출.
- **Meeting 원천**: MinIO에 `Meetings/*.xlsx`가 병존하나 현 토픽 파이프라인 stage 적재 대상은 CSD·Keyword다 → Meeting 적재 경로는 [확인 필요].
- **실행 CronJob**: `brand-activity-topic-monthly`(`0 19 4 * *`, false)·`brand-activity-row-topic-monthly`(`0 22 4 * *`, false), 이미지 `jw-market-crawl@sha256:6b05a5ca…`, root 계정.

---

## 3. 오케스트레이터 내부

### 3.1 CLI 사양 (`python -m pipeline.orchestrator run --help` 실측)

```
--mode {full,incremental}
--stages STAGES        comma-separated subset of ['cache','forecast','strength','shortlong','events','elements']
--from-stage FROM_STAGE   start at this stage (upstream must be completed at the current epoch)
--brands BRANDS        comma-separated brand scope (incremental special form)
--dry-run              print the plan only; execute nothing, write nothing
--force-plan           with --dry-run: include full commands even for stages skipped as fresh
--force                override freshness and stale-dependency checks (recorded)
--state-file / --log-file / --run-id
```
근거: `pipeline/orchestrator/cli.py`, 실행 캡처. 빌더 재구현 없이 각 단계를 subprocess로 호출(DOC-1 §2.3 개요).

### 3.2 증분 감지 기준표 (계열별, file:line)

| 단계 | 증분 방식 | 의미 | deps | 근거 |
|---|---|---|---|---|
| cache | `new_brands` | mart universe−cache coverage 차집합 브랜드만 | () | `stages.py:160-163` |
| forecast | `market_epoch` | 브랜드 단위 증분 없음, mart epoch 지문 변경 시 재빌드 | (cache) | `stages.py:171-174` |
| strength | `native_hash` | 빌더가 input_hash로 무변경 skip(신규/변경만 LLM) | (cache) | `stages.py:185-188` |
| shortlong | `native_hash` | 동(hash 무변경 skip) | (cache) | `stages.py:197-200` |
| events | `full_only` | 원자 스왑 단위, 더 작은 증분 없음 | (cache,strength) | `stages.py:211-214` |
| elements | `new_brands` | 신규 브랜드 스코프 | (cache,strength) | `stages.py:225-228` |

4방식 정의는 `stages.py:5-14` docstring. `full_only`/`market_epoch`은 "브랜드 단위 증분 불가"를 **사유와 함께 정직 표기**한 것(추측 아님).

### 3.3 체인 순서·의존·게이트·멱등

- **순서**: cache → forecast → strength → shortlong → events → elements (deps는 §3.2 표).
- **게이트/중단**: 선행 단계 미완(현재 epoch 기준)이면 후속은 **blocked**(실행 거부), `--force`로만 우회하고 우회 사실을 plan·state에 기록(`planner.py`·`executor.py`).
- **멱등(skip_fresh)**: 체크포인트는 mart epoch(`ops_forecast_store.mart_source_epoch:26` = 6개 mart 테이블 지문) 키 JSON. 같은 epoch 재실행 = 전 단계 skip_fresh(no-op). `--dry-run`은 실행 0·state write 0.
- **관측**: JSON 1줄 1이벤트 로그(단계 시작/종료/행수/게이트) → Temporal 이관 시 Activity 경계.

### 3.4 훅 시스템과의 관계 (경계 명시)

- **정본 아키텍처 = jw market 증분 훅 시스템**(JW_Input_Detection_Contract_v2 — jw-data-input → MinIO manifest → webhook → G3 검증 → incremental Job → Σ게이트). 훅의 실행체는 `python -m pipeline.orchestrator run --mode incremental`이다(우리 오케스트레이터가 인터페이스). 훅 시스템 자체는 **jw market 소관**(DOC-1 §2.8, DOC-5 §3).
- **과도기 트리거 3종은 예비**: ETL 성공 kick(`pipeline/etl/kick.py`)·CSD 센서(`jw-csd-sensor`)·daily poll(`jw-pipeline-orchestrator-poll-daily`)은 훅 착지 시 **대체·삭제 예정**이며 현재 **전부 suspend**(과도기 수동 예비, resume 금지). 근거: manifest 헤더 주석, evidence §A.

---

## 4. 이미지 · 배포

| 이미지 | 빌드 소스 | 라이브 digest | 근거 |
|---|---|---|---|
| jw-market-crawl | `crawl/crawler`←`scripts/crawler`, `crawl/agent1`←`scripts/agent_2`, `/opt/tier2`←`scripts/crawler/tier2_*` 재조립 | `@sha256:64bb2b9f…`(tier1/2 라이브) | `deploy/docker/crawl.Dockerfile`, evidence §B |
| jw-pipeline-orchestrator | pipeline 패키지 전체+docs/crawl, AGENT3 rev baked 없음(fail-closed) | poll manifest pin `@sha256:6bffbc53…`(v0.2.0); 인입 훅은 최신 `v0.2.4-e984a057`(pyarrow+duckdb) | `deploy/docker/pipeline-orchestrator.Dockerfile`, evidence §B,D |
| jw-market-backend-api | (DOC-1 §5, 보호 blob) agent3 Job이 재사용 | agent3 `@sha256:dec3ec3c…` | evidence §B |

- **오케스트레이터 이미지 계약**: `AGENT3_WORKFLOW_REV`가 이미지에 baked되지 않아, Job/CronJob manifest env로만 rev를 주입한다(부재 시 즉사 = 정상). manifest는 `MARIADB_DATABASE`·`AGENT3_DB_NAME`·`AGENT3_WORKFLOW_REV`/`AGENT3_EXPECTED_WORKFLOW_REV`(=5692) fail-closed 이중기입 pin(DOC-1 §2.3).
- **digest 차이 주의**: poll CronJob(suspend·과도기 예비)은 구 pin `6bffbc53`(v0.2.0)를 유지하나, 실 인입 훅은 `v0.2.4-e984a057`(ETL load용 pyarrow+duckdb 추가)를 쓴다. poll이 훅으로 대체되면 이 pin은 소멸 대상이다.
- **빌드/배포 절차**: DOC-1 §5(공통 원칙·ops VM amd64 빌드·AR push) 참조. 크롤 이미지 빌드 커밋 기록 원칙은 DOC-1 §5.1.

---

## [확인 필요] 목록

1. **"Agent 4"**: 스켈레톤/의뢰서의 "Agent1~4"에서 Agent 4는 코드 상 명시 정의가 없다(Agent 1/2/3만 grep 확인). 명명 출처 확인 필요(§2 머리).
2. **Meeting 원천 적재 경로**: MinIO `Meetings/*.xlsx`가 병존하나 현 토픽 stage 적재 대상은 CSD·Keyword다. Meeting 적재 스크립트/사용처 확인 필요(§2.4).
3. **brand_activity 접근 권한**: 서빙 backend는 `jw_brand_activity_stage`를 config 기본으로 읽으나(DOC-1 §2.1), 본 문서 실측은 `jw_mart_d2_writer` 권한 부재로 root 계정 사용. 서빙 계정의 BA 스키마 grant 경로 확인 필요(§2.1).
4. **short-long 실전 비용**: RUNBOOK §6 비용표 기입란 미기입(첫 staging 실행 시 wf217 호출량 기록 예정) — DOC-1 [확인 필요] 5와 동일.

## 스크린샷/다이어그램 캡처 리스트

- [그림: 크롤 tier1/tier2 → events → 스코어 → category refresh 흐름] (§1.1 텍스트 다이어그램의 시각화)
- [그림: 오케스트레이터 6단계 체인·의존·epoch 멱등] (§3.3)
- [화면: 브랜드활동 탭 — 토픽/CSD 카드] (서빙, DOC-4c와 공유)

---

## 부록 · 자기 소관 대조표 (DOC-1b ↔ 실체, 누락 0 / 유령 0)

### (a) 실체 → 문서 (누락 0)
| 실체 | 값(실측) | 본 문서 |
|---|---|---|
| CronJob 크롤/BA/orchestrator 13종 | evidence §A | §1.2·§2.4·§3.4 ✓ |
| 크롤 이미지 digest | `64bb2b9f…` | §1.2·§4 ✓ |
| tier2 CM 크기 | 49,549 B | §1.2 ✓ |
| tier2 정책 상수 | `tier2_catalog.py:18-20`·`runner:1346-1347` | §1.3 ✓ |
| category refresh 커밋 | `ec4f6e04` | §1.4 ✓ |
| 행수 크롤 5종 | evidence §C-1 | §2.1 ✓ |
| 행수 생성 8종 | evidence §C-2 | §2.1 ✓ |
| 행수 BA 12종(+raw 2) | evidence §C-3,4 | §2.4 ✓ |
| forecast 결정론 6항 | `forecast_runner.py:65,106,30,59`·`ops:33` | §2.2 ✓ |
| 오케스트레이터 6단계·deps | `stages.py:159-228` | §3.2 ✓ |
| CLI 옵션 | `--help` 실측 | §3.1 ✓ |
| 이미지 3종 digest | evidence §B | §4 ✓ |

### (b) 문서 → 실체 (유령 0)
본 문서의 전 수치·리소스명·file:line은 `evidence/doc1b_capture.md` 또는 인용 파일에 실재. 추측 서술은 [확인 필요] 4건으로 분리(유령 0).

### (c) 경계 준수 (DOC-1/DOC-2 중복 0)
서빙 API·backend·mart 스키마·사이트·전체 구성도는 재서술 없이 DOC-1 §2.1/2.6/2.8·DOC-2b로 참조 처리. 본 문서 신규 서술은 크롤 내부(스코어링 정책·category refresh)·생성 계열 로직·오케스트레이터 내부·이미지 계약에 한정.
