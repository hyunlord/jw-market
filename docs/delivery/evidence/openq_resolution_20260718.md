# [확인 필요] 해소 실측 근거 — jw market 소관 (2026-07-18)

의뢰서(CODEX SI [확인 필요] 해소) Q-1~Q-8 조사 산출. 전 항목 read-only(코드 grep·in-mesh kubectl get·site head 문자열). DB write·배포·코드 수정 0.

기준 develop HEAD: `e812dd35`. 운영 backend gen 302 계열.

---

## Q-1 · 브랜드활동 화면 정렬/lookback end-to-end 배선 (DOC-4c #1)

**결론: `phase29_events.py`의 cut-A 정렬/lookback 정책은 브랜드활동 탭이 아니라 심층분석 이벤트 카드 + 동적시장 응답 캐시 경로가 소비한다.**

- `phase29_events.py`(`build_events_for_cache`, cut_a)의 소비처 = `pipeline/scripts/etl/build_cache_deep_analysis.py:36`(`from phase29_events import build_events_for_cache, ensure_events_raw_synced`), `:181-192`(`_dedup_cut_a_events`, `payload["cut_a"]`). → 심층분석 캐시(`cache_deep_analysis`) 빌드.
- `event_brand_scores`/`events_raw`/`cut_a`/`build_events_for_cache` 서빙 소비처(grep `pipeline/scripts/api/`): `deep_analysis_runtime.py`, `dynamic_market/response_cache.py`. **`routes/brand_activity.py`에는 없음.**
- 브랜드활동 탭 서빙 라우트 `pipeline/scripts/api/routes/brand_activity.py`의 엔드포인트(`@router` 실측): `/api/brand-activity/topics`, `/topics/{scope_id}`, `/csd-presence`, `/csd-timeseries`, `/csd-activity-series`, `/interest-rx-matrix`, `/topics`(POST 공유). **이벤트/뉴스 리스트 엔드포인트 없음** — 토픽(`mart_brand_activity_topics`)·CSD·토픽매트릭스만 서빙.

→ 즉 DOC-4c §4의 정렬(점수 우선)·adaptive lookback 정책은 **심층분석 이벤트 카드** 전용이며, 브랜드활동 탭 1차 서빙(토픽/CSD)에는 적용되지 않는다. 마커 가설("vs 심층분석 이벤트 카드 전용")이 실측으로 확인됨.

## Q-2 · 상위 N 절삭 기준 (DOC-4c #2)

**결론: 절삭은 주로 서빙단 토픽 매트릭스 `top_n`(기본 5·clamp 1..10). 저장(빌드)단 브랜드 캡은 기본 무제한.**

- 서빙: `pipeline/scripts/api/brand_activity_topic_matrix.py:149` `top_n = _integer(payload.get("top_n") or 5)`, `:161` `"top_n": max(1, min(top_n, 10))`, `:257` `axis_topics[:top_n]`. → 요청 파라미터 `top_n` 기본 5, 1~10로 클램프, 상위 top_n개만 표출.
- 저장/빌드: `pipeline/scripts/analysis/brand_activity/auto_topic/sampling.py:25` `DEFAULT_BRANDS_PER_MARKET: Final[int | None] = None`(무제한), `run_auto_topic.py:108` `--brands-per-market ... Omit to include every keyword-bearing brand`. 라이브 월간잡 env `TOPIC_BRANDS_PER_MARKET` 기본 10000(사실상 전량; `topic_monthly_job.py` JobConfig `brands_per_market=10000`).
- 토픽 카드 리스트(`brand_activity_topics.py`)는 저장된 `brand_specific_topics`를 그대로 서빙(추가 per-brand 절삭 없음); `etc_pct = max(0, 100 - 표시 top_n share 합)`.

→ 사용자가 보는 "상위 N"은 대부분 **토픽 매트릭스 top_n(기본 5·최대 10)** 서빙 절삭이며, 저장단은 기본 무제한.

## Q-3 · CSD 활동 지표(`product_details`) 정의 (DOC-4c #3)

**결론: `product_details` = CSD 원천 워크북 "Product Details" 헤더 열의 정수 측정값 = (채널×제품×월 grain, Region=TOTAL 행) 제품 디테일링 활동 건수.**

- `pipeline/scripts/etl/brand_activity/csd_core.py:13` `EXPECTED_HEADERS = ("Market","JW Channel","Region","Master product","Representing Company","Product Details", ...)`.
- `:52-62` `CsdRow(... master_product, representing_company, product_details: int)`, grain_key = (period_ym, market, jw_channel, master_product, representing_company).
- `:111 parse_product_details` — 콤마 제거 후 정수 파싱(정수 아니면 예외). `:161 is_total_region` — **Region == "TOTAL" 행만 stage 적재**(리전별 합계 행).
- `:83 product_details_total_region`, `:209 total_sum += product_details` — 시트 내 리전 합.
- 원천은 ChannelDynamics(CSD) 영업활동 지표이며 매출 아님(DOC-4c §4-5). JW_CHANNELS = {TOTAL, GH, SHPPI, GH+SHPPI, CPPI}.

## Q-4 · DOC-2b "DOC-2 참조" 컬럼 타입 상호참조 (DOC-2b #1)

**결론: DOC-2가 실제로 크롤/BA 테이블을 §2.11 및 개별 소절로 담고 있어 참조 유효. 정확 앵커:**

`docs/delivery/DOC-2_DB_스키마정의서.md` 내:
- 크롤 계열: `#### news_raw`(L724), `#### events_raw`(L750), `#### events`(L765), `#### event_brand_scores`(L789).
- BA stage: `### 2.11 브랜드활동 stage DB (jw_brand_activity_stage)`(L876) 하위 — `csd_channel_dynamics_stage`(L880), `km_keyword_event_stage`(L896), `mart_brand_activity_topics`(L922), `mart_brand_activity_topic_runs`(L936), `row_topic_assignment`(L957), `row_topic_assignment_status`(L971), `row_topic_assignment_share_view`(VIEW, L986).

## Q-5 · km_keyword_event_stage 적재 스크립트 (DOC-2b #2)

**결론: 적재 주체 = `pipeline/scripts/etl/brand_activity/ingest_keyword.py`(워크북 파싱) → `load_raw_staging.py`(raw insert + stage 적재). DDL = `ingest_keyword_stage.py`.**

- `ingest_keyword.py:1` "Read JW Keyword workbooks into append-preserving stage events." — KEYWORD_HEADERS(Related date·VISIT LOCATION·SPECIALTY NAME·REP# CO·PRODUCT NAME·THERAPEUTIC CLASS·KEYWORDS…) 파싱.
- `load_raw_staging.py:229-231` `truncate_targets.append("km_keyword_event_stage")`, `raw_insert_targets.append("raw_keyword_events")`, `expected_stage_rows["km_keyword_event_stage"] = len(keyword_rows)` — raw_keyword_events 적재 + km_keyword_event_stage stage 적재.
- `ingest_keyword_stage.py:10,23-` — stage DDL 헬퍼(CREATE TABLE km_keyword_event_stage: period_ym·visit_location·specialty·representing_company·product_name·therapeutic_class·keyword_text·interest·prescription_frequency·prescription_evolution·abstract_lit…).
- ★ 소비처와 구분: `auto_topic/data_source.py:19 KEYWORD_TABLE = "km_keyword_event_stage"`는 **읽기(토픽 생성 소비)** 지점이지 적재 주체 아님. (DOC-2b §2 표 "생성 주체" 열의 data_source.py:19 표기는 소비처를 가리킴 — 교정 필요, 본 라운드 무수정·보고만.)

## Q-6 · 백엔드 운영 승격 스크립트 실체 (DOC-1 §5.2)

**결론: repo 워크트리에 백엔드 이미지 승격 자동화 스크립트 없음(확인). 존재하는 deploy 스크립트는 데이터/캐시 승격용뿐.**

- `pipeline/scripts/deploy/` 실측: `analysis_cache_blue_green.py`, `analysis_cache_blue_green_validation.py`(캐시 blue-green), `pipeline/etl/io/mart/filter_dimension_promote.py`(mart dimension 승격). — 전부 **데이터/캐시** 승격.
- 백엔드 이미지 승격(generation CAS) 자동화 스크립트: repo grep(promote/deploy.sh/deploy.py, generation/set image/rollout) 무결과. → 백엔드 승격은 **GenOS 운영 UI/플랫폼 경로**(코드 밖). RUNBOOK도 관행만 기술.

→ 부분 해소(repo에 없음 확정) + GenOS 정확 커맨드는 플랫폼 소관(OPEN_QUESTIONS).

## Q-7 · dynamic-market-cache-warm 치환 실이미지 (DOC-1 §2.2)

**결론(2026-07-18 in-mesh kubectl 실측):**
```
cronjob dynamic-market-cache-warm image =
  asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/
  jw-market-backend-api@sha256:8e2501cdf1e80982a78ffba575f520ed111f2419f109905d1d3c20482d568bee
deploy jw-market-backend-api image =
  ...jw-market-backend-api@sha256:aec14a907b3a9d9577e3eb4a08c2c917aa7dc5db00c17ee07c81a7d43c8830bd
```
- 치환 대상 placeholder `JW_MARKET_API_IMAGE` = **jw-market-backend-api 이미지**(cronjob이 `python -m pipeline.scripts.api.dynamic_market.cache_maintenance` 실행하므로 backend 이미지 공유).
- ★ 현재 cronjob(@8e2501cd)과 live backend deploy(@aec14a90) digest가 **드리프트** — cronjob은 별도(이전) 백엔드 이미지로 배포됨. 동일 이미지 계열이나 digest 불일치.

## Q-8 · 사이트 배포 VERSION 괴리 (DOC-1 §5.4)

**결론: `web/deploy.sh`·`web/cloudbuild.yaml`는 이 repo(pipeline) 밖 — 별도 사이트 repo(Gitea `jw-data-input`/`jw-market`). 본 repo에서 `web/` 부재.**

- 본 워크트리 `web/` 디렉토리 없음(ls·find deploy.sh 무결과). 사이트 코드 정본 = Gitea HEAD `8ca9d987`(로컬 체크아웃 `/tmp/site-head`).
- 운영 실배포 태그 `v0.6.0-8ca9d98`(evidence/dataportal_env_v060.txt) = `v0.6.0` + 커밋 SHA 접미. 스크립트 기본 `VERSION=v0.2.9`는 사이트 repo 소관 상수. → 사이트 repo(별도 소관) 확인 항목. 본 repo 관점 부분 해소(위치 확정).

## DOC-5 §6 · Grafana/Alertmanager 연동 여부 (line 194)

**결론(2026-07-18 실측): 클러스터 전역 kube-prometheus-stack은 존재하나 jw-market 서비스는 미배선.**

- `kubectl get pods -A | grep -iE grafana|alertmanager|prometheus`: `monitoring/alertmanager-prom-0`(2/2), `monitoring/prom-grafana-0`(3/3), `monitoring/prom-prometheus-node-exporter-*`(다수) 실행 중.
- `kubectl get servicemonitors -A` 총 9개(전역), **`llmops` 네임스페이스(jw-market backend/cronjob 소재) = 0개**. `kubectl -n llmops get servicemonitor,prometheusrule` = "No resources found". CRD 조회 가능(9개 확인)이므로 부재는 실측 음성.
- → 인프라(Prometheus/Grafana/Alertmanager) 스택은 있으나 jw-market 서비스별 ServiceMonitor/알림룰 미구성(노드/클러스터 메트릭만). app-level 대시보드/알림 = 플랫폼 소관(OPEN_QUESTIONS).

## DOC-4b #1 · 미인가(unauthorized) 화면 실제 문구

**결론(site head `/tmp/site-head/web/src/app/unauthorized/page.tsx:27-73` 실측):**
- 상단 라벨: "Access Restricted"
- 제목: "접근 권한이 없습니다"
- 본문: "현재 로그인한 계정은 이 포털에 허용되지 않았거나, 요청한 화면에 대한 권한이 없습니다."
- 정보 카드: 로그인 계정(email)·현재 역할(role)·요청 경로(requestPath) — 미상 폴백 "알 수 없는 계정"/"없음"/"알 수 없음".
- 안내: "관리자에게 접근 권한을 요청한 뒤 다시 로그인하세요." + CTA "다른 계정으로 로그인".

## PL 판단 사안 (조사로 해소 불가 — OPEN_QUESTIONS 등재)

- **BA 서빙 계정 grant**(DOC-2b #3): writer 계정(`jw_mart_d2_writer`) 권한 밖 → BA CronJob·실측 root(secret `galera-mariadb-galera`/`mariadb-root-password`) 사용. writer grant 부여 vs root 현행 = 권한 정책(PL/플랫폼).
- **mart DB 백업 정책**(DOC-5 §5): repo/실측 범위서 mysqldump/PITR 스케줄 미확인. Galera 3-노드 복제는 HA일 뿐 백업 아님. 정기 논리 백업 정책 = 플랫폼팀.
- **채팅 정본 이원화**: 운영 채팅 = 피처 브랜치 이미지 `da3fc153`(`codex/p3-file-brief-20260718`), develop 미머지. merge-base `276a47b5`(2026-07-11), develop +370 / 피처 +350 발산(evidence/chat_lineage_gap.md). chat 3종 문서 [운영 이미지 기준] 이중 표기 상태 — 머지 여부·시점 PL 판단.
- **훅 리허설→실적재 전환 시점**(DOC-1 항목6): `INGEST_REHEARSAL_ROOT` 격리 모드 기동 중, 실 mart 적재 전환(변수 해제) = 남은 PL 게이트.
- **shortlong(Agent2) 실전 비용**(DOC-1 항목5): 첫 staging 실행 전 미생성 — 데이터 부재(측정 대기).
