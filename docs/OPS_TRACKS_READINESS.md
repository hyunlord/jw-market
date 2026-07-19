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

*본 문서는 상태 스냅샷(2026-07-19)이다. 스케줄/suspend/Alertmanager 상태 변경 시 §2·§3·§6 을 라이브 재확인 후 갱신할 것.*
