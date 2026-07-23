# 운영 트랙 준비도 (OPS TRACKS READINESS)

> 대상: jw-market **일간 자동 데이터 트랙**의 운영 현행 상태 — "무엇이 언제 돌고, 실패하면 어떻게 아는가".
> 성격: 월간 ETL 적재 중심의 기존 문서와 분리된 **일간 트랙 운영 정본**. 코드·CronJob·DB 무접촉 문서.
>
> **근거(provenance)**: ⓐ 라이브 `kubectl`/시크릿 read-only 실측(2026-07-19, ns `llmops`·`monitoring`) — 스케줄·suspend·Alertmanager·PrometheusRule. ⓑ develop 매니페스트(`deploy/k8s/**`)·스크립트 실측 — 이름·backoffLimit·경로. ⓒ ops_readiness audit `98c0282c`(jw agent) — events 누적행수·ingest-sweep 409 상세.
> 표기 규칙: 실측 확인분만 서술. 미확정은 `[확인필요]` 유지.

---

## §1. 서빙 DB (판정 기준)

- 트랙 산출물이 서빙되는 정본 DB = **`jw_mart_d2_stage_20260630_r2`** (develop `chat/jw-chat-agent-poc/deploy/d2-database-env-patch.yaml`·goldens에서 확인).
- 구본 `jw_mart` / `*_d1_stage_*` 는 **현행 판정 기준으로 쓰지 말 것** — 최신성/정합 판정은 반드시 `d2_stage_20260630_r2` 기준.

---

## §2. 5트랙 현행 (라이브 실측)

스케줄은 CronJob spec(UTC) → KST 환산. `자동/수동`·`재시도`는 매니페스트 `suspend`·`backoffLimit` 실측.

| 트랙 | 실행체 (live cronjob) | 스케줄 UTC (→KST) | 자동/수동 | 갱신 구동 | 재시도(backoffLimit) |
|---|---|---|---|---|---|
| ① 크롤 | `jw-news-crawl-tier1-daily` + `jw-news-crawl-tier2-daily-slice` | `10 18`→**03:10** / `40 18`→**03:40** | 자동 | 일 2회 | **1** |
| ② 뉴스분류 | tier2 체인 **내부** (전용 CronJob 없음 = 설계상 정상) | — (tier2 연동) | 자동 | tier2 연동 | tier2 준용 |
| ③ 토픽 | `jw-news-...topic-monthly`(월간) + `...row-topic-monthly`(월간) + **CSD 원천 수동** `pipeline/scripts/etl/brand_activity/load_raw_staging.py` | `0 19 4`→매월 4일 04:00 (외) + 수동 | **반자동** | 월간 + 수동 | 0 |
| ④ 요소분석 | `jw-agent3-refresh-daily` | `0 21`→**06:00** | 자동(입력 구동) | **mart 변경 시에만**(hash-skip §4) | **0** |
| ⑤ AI인사이트 | `jw-cache-refresh-daily` (원자 스왑 체인) | `0 20`→**05:00** | 자동 | events 반영(전일 크롤→익일) | **0** |

- ①의 `*-canonical` 재설계판(tier1/tier2)은 **suspend**(§6) — 현재 서빙 구동은 비-canonical 2개.
- 라이브 lastScheduleTime 확인(2026-07-18): tier1 18:10 · tier2 18:40 · cache-refresh 20:00 · agent3 21:00 — 정상 구동 중.

---

## §3. ★ "실패하면 어떻게 아는가" — 현실 (라이브 실측)

- PrometheusRule **`KubeJobFailed`·`KubeJobNotCompleted` 존재**(실측). 그러나 **Alertmanager 라우트 receiver = `"null"`**(`monitoring/alertmanager-prom` 시크릿 `alertmanager.yaml` 실측: default route `receiver: "null"`, receivers 목록에 `name: "null"`만).
- ∴ **전 트랙 Job 실패 알림이 어디에도 전달되지 않음** (룰은 발화하나 수신처가 null).
- 현행 유일 감지 수단 = **Grafana 수동 확인**:
  - 접속: `monitoring` ns `prom-grafana`(pod `prom-grafana-0`) — 포트포워딩 후 브라우저.
  - 볼 것: Kubernetes / CronJob·Job 대시보드에서 대상 Job 최근 실행 성공/실패, 또는 `kube_job_failed`·`kube_job_status_failed` 쿼리.
- receiver 를 실 수신처(Slack/webhook 등)로 바꾸는 것은 **플랫폼(모니터링 스택) 소관** — 본 트랙 코드/매니페스트 밖. 조치 주체·경로 `[확인필요]`. **현재 미해결(무통지) 상태임을 명시.**

---

## §4. ★ hash-skip 동작 (④ 요소분석 오해 방지)

- `jw-agent3-refresh-daily` 의 **daily 성공 ≠ daily 갱신**. agent3 산출물에는 `skipped_same_hash` 카운트가 있으며(스크립트 실측: `run_full.py`·`stages.py`), **입력(mart) 해시 불변이면 no-op(skip)이 설계된 정상 동작**.
- ∴ "strength/elements 가 오래됐다"고 판단하기 **전에**:
  1. mart 최종 재적재일 확인(§1 `d2_stage_20260630_r2` 적재 시각).
  2. mart 가 그날 안 바뀌었다면 agent3 skip 은 **정상**이며 결함 아님.
  3. mart 는 갱신됐는데 agent3 산출이 안 바뀐 경우에만 이상으로 취급.

---

## §5. 일상 장애 대응 (일간 트랙)

재시도 0(agent3·cache-refresh)·1(crawl) — 자동 재시도가 빈약하므로 실패 시 **다음 스케줄까지 공백**. 수동 개입 절차:

- 실패 확인: `kubectl get jobs -n llmops | grep <track>` → 실패 Job 의 `kubectl logs job/<job>`.
- 수동 재실행(예): `kubectl create job --from=cronjob/<cronjob-name> <name>-manual-$(date+%s) -n llmops`.
- 재실행 판단 기준:
  - **crawl 실패** → 그날 events 미수집 → ⑤ cache-refresh 입력 공백 → 재실행 권장(다음 03:10까지 공백).
  - **agent3 실패** → strength/elements 미갱신, 단 §4 hash-skip 이면 재실행 불필요.
  - **cache-refresh 실패** → AI 인사이트/동적 캐시 stale → 재실행 권장(원자 스왑이라 부분오염 위험 낮음).
- ★ 재시도 0 트랙은 실패 시 **자동 복구 없음** — 위 수동 절차가 유일.

---

## §6. Suspended CronJob 인벤토리 (라이브 실측: 19개 중 **8 suspended** / 11 active)

| # | suspended cronjob | 스케줄 | 성격 / 상태 |
|---|---|---|---|
| 1 | `jw-news-crawl-tier1-daily-canonical` | `10 18` | 재설계판 — 훅/비-canonical 대체 전제 (정상 미가동) |
| 2 | `jw-news-crawl-tier2-daily-slice-canonical` | `40 18` | 동상 |
| 3 | `jw-csd-sensor` | `*/10` | 훅 대체 전제 |
| 4 | `jw-ingest-sweep-daily` | `30 19` | 훅 대체 전제 · **마지막 실행 2026-07-17 실패(HTTP 409, audit 98c0282c)** |
| 5 | `jw-pipeline-orchestrator-poll-daily` | `0 16` | 훅 대체 전제 |
| 6 | `jw-brand-activity-run` | `0 0 30 2 *` | **스케줄 자체가 영구 미실행**(2월 30일 = 존재 안 함); sensor-created Job 전용 |
| 7 | `jw-news-crawl-retention-daily` | `0 19` | SUSPEND → events `expire_at` 미집행 누적(**약 35,573행**, audit 98c0282c) |
| 8 | `iqvia-general-sidecar-quarterly` | `0 3 5 1,4,7,10 *` | 분기 사이드카 · 목적/대체 `[확인필요]` |

- ★ 매니페스트-라이브 정합 주의: develop `deploy/k8s`에는 `jw-news-tier2-category-refresh`·`jw-brand-elements-refresh-weekly` 도 `suspend: true`로 존재하나 **라이브 클러스터에 미배포**(19개 목록에 없음) → 라이브 suspended 는 위 8개가 정확. 두 매니페스트의 배포 의도 `[확인필요]`.
- 훅 대체 전제(1~5)는 증분 인입 훅 스택으로 대체된 과도기 예비이며, **활성화는 PL 게이트**.

---

## §7. 기존 문서와의 관계

- `RUNBOOK_MONTHLY.md`(리포 루트) = **월간 ETL 적재** 중심 런북. 본 문서 = **일간 자동 트랙** 운영 정본. 역할 분리 — 중복 서술 금지, 상호 링크만.
- 의뢰서가 지목한 `operation.md`·`market_pipeline_runbook.md` 는 develop 에서 미발견 `[확인필요]`(경로/이름 상이 또는 미머지 가능) — 확인되면 본 절 링크 갱신.

---

## §8. 크롤 4단계 체인과 cache cutoff 계약 (PL 승인 후 cutover)

### §8.1 실행 순서

tracked target은 `jw-crawl-chain-daily` 하나이며 매일 03:10 KST에 다음 순서로 실행한다.

1. `tier1_collect` (timeout 180분)
2. `tier1_classify_incremental` (timeout 15분)
3. `tier2_collect_exact` (timeout 480분)
4. `tier2_classify_v2_and_refresh` (timeout 30분)

runner `pipeline/scripts/crawler/crawl_chain.py`가 각 subprocess rc를 확인한다. non-zero 또는 timeout이면 `CHAIN_STAGE_FAILED` JSON marker와 실패 receipt를 기록하고 즉시 비제로 종료하므로 후속 단계는 실행되지 않는다. 전체 Job은 `activeDeadlineSeconds=43200`(12시간), `backoffLimit=0`, `concurrencyPolicy=Forbid`, `startingDeadlineSeconds=900`이다. 관측 중앙값 약 8시간 33분, 상단 약 8시간 58분이므로 03:10 시작 시 11:43~12:08 완료를 예상한다.

`Forbid`는 같은 CronJob의 전일 실행이 남은 경우 새 Job 생성을 건너뛴다. runner의 PVC flock도 수동 resume Job과 정규 Job의 동시 실행을 거부하고 `CHAIN_SCHEDULE_SKIPPED_ACTIVE`를 남긴다. skip을 성공으로 간주하지 말고 운영 확인 대상으로 취급한다.

### §8.2 durable receipt와 멱등 경계

PVC `jw-crawl-chain-state`의 `/var/lib/jw-crawl-chain/runs/<run-id>/`에 단계별 산출물과 receipt를 보존한다. receipt 필드는 `run_id`, `stage`, `attempt`, `status`, `started_at`, `finished_at`, `command_revision`, `input_sha256`, `output_sha256`, `exit_code`, `error_code`다. 첫 단계의 input은 `root`, 이후 단계의 input은 직전 단계 `output_sha256`이므로 단계 간 계보도 끊어지지 않는다. 각 시도는 `attempts/<stage>/attempt-<n>`에 격리하고, rc=0인 시도만 `outputs/<stage>`로 원자 rename한 뒤 receipt를 기록한다. 실패한 부분 산출물은 최종 output으로 승격되지 않는다.

resume은 앞 단계의 `status=complete`, `command_revision`, `input_sha256`, `output_sha256`가 모두 일치할 때만 허용한다. 하나라도 없거나 달라지면 fail closed한다. 이미 완료되고 SHA가 맞는 단계는 `CHAIN_STAGE_SKIPPED_COMPLETE`로 건너뛰므로 같은 run-id의 멱등 replay가 DB 적재를 반복하지 않는다. 실제 loader의 기존 `news_id` duplicate gate, target processor existence skip, `sync-events-raw` missing-only insert도 그대로 유지한다.

### §8.3 PL-gated cutover와 rollback

tracked manifest는 안전을 위해 chain 자체도 `suspend: true`로 저장한다. 승인 후에만 다음 명령을 사용한다.

```bash
# API dry-run only
deploy/k8s/crawler/apply-crawl-chain.sh --dry-run

# active legacy Job 0을 확인하고 old 2개 suspend -> chain activate
deploy/k8s/crawler/apply-crawl-chain.sh --execute-cutover

# chain suspend -> old schedules restore
deploy/k8s/crawler/apply-crawl-chain.sh --rollback
```

helper는 새 chain을 suspended 상태로 먼저 적용하고 old tier1/tier2를 suspend한 뒤 마지막에 chain만 activate한다. old CronJob은 삭제하지 않는다. active old Job이 하나라도 있으면 cutover를 거부한다. 이번 코드 라운드에서는 어떤 cluster object도 변경하지 않는다.

### §8.4 실패 조회와 수동 재개

Alertmanager receiver가 `null`인 동안 receipt와 구조화 로그가 자체 확인 수단이다. status Job은 완료 단계와 최초 실패 단계를 JSON으로 반환하며 실패 run이면 rc=1이다.

```bash
RUN_ID='2026-07-21T03-10-00+09-00'

# 조회 Job 렌더링 후 명시적으로 실행
deploy/k8s/crawler/render-crawl-chain-control-job.sh status "$RUN_ID" \
  | kubectl -n llmops apply -f -

# 실패 단계부터 재개. 이전 receipt/SHA가 다르면 실행 전 거부된다.
deploy/k8s/crawler/render-crawl-chain-control-job.sh resume "$RUN_ID" tier2_collect_exact \
  | kubectl -n llmops apply -f -
```

운영 폴링 SLA는 매일 12:30 KST까지 latest chain status 확인으로 둔다. 아래 marker 중 하나가 있으면 실패로 처리한다: `CHAIN_STAGE_FAILED`, `CHAIN_SCHEDULE_SKIPPED_ACTIVE`, 12:30까지 `CHAIN_RUN_COMPLETE` 부재. 외부 paging은 플랫폼 receiver가 연결된 뒤 별도 추가하며, receiver가 없는 상태를 알림 완료로 보고하지 않는다.

### §8.5 stage 4 용량과 backlog gate

2026-07-21 실측 pending v2 74건은 순서 결함이 아니라
`append-live --daily-call-limit 60` 제한이었다. 2026-07-23 PL 승인으로 일일
한도는 100콜, 비용 상한은 339.00원으로 조정됐다. 승인된 과거 10일 유입량
(55·57·29·100·99·92·91·18·44·50) 재생에서 current-run hard-gate 실패는
60콜에서 4/10, 100콜과 120콜에서 각각 0/10이었다. 120콜은 같은 입력에서
추가 처리량이 없고 최대 예산만 406.80원으로 늘어 제외했다.

실행 성공의 hard gate는 누적 backlog가 0인지가 아니라 실행 전후 pending
pair 집합을 비교해 이번 실행이 만든 미해결 pair가 0이고 총 pending이
증가하지 않았는지를 본다. 누적 backlog는 별도 SLO로 관리한다:
oldest age 2일 warning/4일 failure, 연속 비감소 2회 warning/4회 failure.
warning은 실행을 막지 않지만 failure는 stage gate를 실패시킨다. baseline과
평가 receipt는 Temporal 14일 history 밖의 crawl state PVC에 content-addressed
snapshot과 run별 receipt로 보존한다.

### §8.6 cache = 전일 완결 snapshot

`jw-cache-refresh-daily`는 **05:00 KST 고정**이며 crawl chain의 5단계가 아니다. tier2가 10:31까지 실행된 실측이 있어 같은 날 chain 완료를 기다리면 cache 가용 시점이 정오 이후로 밀린다. 따라서 cache 계약은 다음과 같다.

> 05:00 cache는 refresh 시작 전에 DB에 완결 적재된 마지막 crawl 결과를 publish한다. 당일 03:10에 시작한 crawl 결과는 같은 날 cache에 부분 반영한다고 보장하지 않으며, 익일 05:00 snapshot에서 완결 반영한다. 최대 약 24시간 freshness 지연은 정상 계약이고, 부분 swap이나 DB 손상으로 판정하지 않는다.

2026-07-21 예시: cache는 05:10:22 완료, tier1 loader는 05:16:47 시작해 `news_raw/events/event_brand_scores=38/38/16`을 뒤에 적재했다. 05:00 cache가 이를 포함하지 않은 것은 이 계약에서는 정상이며 익일 반영 대상이다.

cutoff runtime metadata는 응답 계약 승인 전 임의로 추가하지 않는다. 저장안은 live cache 행마다 복제하지 않고 sidecar 테이블 `cache_publication_meta` 1행/run으로 둔다.

| 필드 | 타입/의미 |
|---|---|
| `cache_name`, `refresh_run_id` | PK 구성; `cache_deep_analysis`, refresh run identity |
| `snapshot_policy` | `previous_complete_snapshot` 고정 |
| `source_cutoff_at` | staging build 직전 `events`에서 실제 읽을 수 있던 최대 source timestamp |
| `source_max_news_id` | 동률 timestamp 검증용 deterministic high-water mark |
| `published_at` | 원자 apply/post-verify 완료 시각 |
| `source_row_count`, `payload_row_count` | cutoff 재검증 및 급락 gate |

`cache_deep_analysis` 자체에는 `updated_at`만 있고 source cutoff lineage가 없으므로 이 sidecar가 기존 응답 payload와 cache PK를 흔들지 않는 최소안이다. writer는 staging build 직전 source high-water mark를 캡처하고 post-verify 성공과 같은 publication 경계에서 insert한다. 실패 swap에서는 publication row를 만들지 않는다. 이 DDL/writer는 별도 승인 전 구현하지 않는다.

소비자 노출 옵션:

| 옵션 | 형태 | 호환성/판정 |
|---|---|---|
| A (권고) | 기존 `data` 아래 additive `cache_snapshot_meta={policy,source_cutoff_at,published_at}` | deep-analysis OpenAPI·BFF·portal 소비 확인 후 도입. JSON consumer가 unknown field를 허용하면 가장 명시적 |
| B | `X-JW-Cache-Source-Cutoff`, `X-JW-Cache-Published-At` 응답 헤더 | payload 무변경이나 BFF/ingress header 전달 보장 필요 |
| C | 운영 로그/대시보드만 | 소비자 계약 무변경이나 사용자가 freshness를 알 수 없어 최종안으로 부적합 |

이번 patch는 문서·manifest 주석까지만 반영하고 API response, cache schema, refresh swap 명령은 변경하지 않는다. A/B 선택과 consumer 영향 확인 뒤 별도 계약 patch로 진행한다.

---

*§1~§7은 상태 스냅샷(2026-07-19)이고 §8은 PL-gated 구현 계약이다. cutover 후 §2·§3·§6의 라이브 상태와 commit/image digest를 재확인해 갱신할 것.*
