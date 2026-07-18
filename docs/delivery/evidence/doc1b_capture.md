# DOC-1b 실측 캡처 (크롤·BA·파이프라인)

- 캡처일: 2026-07-18 · 기준 develop SHA `9c34a7d5` · 클러스터 ns `llmops`
- 방식: read-only. kubectl get/exec, `COUNT(*)`(information_schema.TABLE_ROWS 미사용), 로컬 `git`/`grep`/`--help`. DB write 0.

## A. CronJob (kubectl get cronjob, 2026-07-18)

| CronJob | schedule | suspend |
|---|---|---|
| brand-activity-row-topic-monthly | `0 22 4 * *` | false |
| brand-activity-topic-monthly | `0 19 4 * *` | false |
| iqvia-general-sidecar-quarterly | `0 3 5 1,4,7,10 *` | true |
| jw-agent3-refresh-daily | `0 21 * * *` | false |
| jw-brand-activity-run | `0 0 30 2 *` | true |
| jw-cache-refresh-daily | `0 20 * * *` | false |
| jw-csd-sensor | `*/10 * * * *` | true |
| jw-news-crawl-retention-daily | `0 19 * * *` | true |
| jw-news-crawl-tier1-daily | `10 18 * * *` | false |
| jw-news-crawl-tier1-daily-canonical | `10 18 * * *` | true |
| jw-news-crawl-tier2-daily-slice | `40 18 * * *` | false |
| jw-news-crawl-tier2-daily-slice-canonical | `40 18 * * *` | true |
| jw-pipeline-orchestrator-poll-daily | `0 16 * * *` | true |

## B. 이미지 digest (kubectl jsonpath)

| 리소스 | 이미지 |
|---|---|
| jw-news-crawl-tier1-daily | `jw-market-crawl@sha256:64bb2b9f2ad213a06392d5caf9ea4191615d265ecdcfb52b64bba59ae9171268` |
| jw-news-crawl-tier2-daily-slice | `jw-market-crawl@sha256:64bb2b9f…` (동일) |
| jw-pipeline-orchestrator-poll-daily | `jw-pipeline-orchestrator@sha256:6bffbc5350aadd302124c500eb91b16415b0dcfe42c635774fd0abd763441996` |
| jw-agent3-refresh-daily | `jw-market-backend-api@sha256:dec3ec3ca788faf517441c784a727f3f3ba251a50a2764f0dd1419fde52e60c6` |
| brand-activity-topic-monthly | `jw-market-crawl@sha256:6b05a5cafae62388f2e2f6806df97c527ce91011e92b23ef408c6e805cc7acb9` |
| jw-ingest-hook (Deployment, 참고) | `jw-pipeline-orchestrator:v0.2.4-e984a057` |

- tier2 CM `tier2-llm-runner-rev5671` 마운트: `volumeMounts /opt/tier2` (name tier2-llm-runner), 데이터 크기 **49,549 bytes**(정본 runner 동기화판).

## C. 행수 (COUNT(*), 2026-07-18)

### C-1 크롤/이벤트 (`jw_mart_d2_stage_20260630_r2`)
| 테이블 | COUNT(*) |
|---|---|
| news_raw | 35,507 |
| events | 35,507 |
| events_raw | 35,507 |
| event_brand_scores | 71,318 |
| tier2_match_staging | 23,964 |

### C-2 생성 계열 (`jw_mart_d2_stage_20260630_r2`)
| 테이블 | COUNT(*) |
|---|---|
| cache_deep_analysis_general | 34,378 |
| cache_deep_analysis | 4,695 |
| deep_forecast_block | 43,474 |
| deep_forecast_horizon | 3,000 |
| cache_market_forecast_general | 2,880 |
| agent3_brand_strength | 25,153 |
| agent3_brand_strength_source | 35,521 |
| cache_brand_elements | 26,411 |

### C-3 brand_activity (`jw_brand_activity_stage` — root 자격 조회)
| 테이블 | COUNT(*) |
|---|---|
| csd_channel_dynamics_stage | 49,894 |
| csd_channel_dynamics_stage_bak_20260705_151611 | 44,025 (백업) |
| km_keyword_event_stage | 66,556 |
| mart_brand_activity_topics | 11 |
| mart_brand_activity_topics_staging | 11 |
| mart_brand_activity_topic_runs | 4 |
| mart_brand_activity_topic_runs_staging | 1 |
| row_topic_assignment | 172,419 |
| row_topic_assignment_share_view | 1,639 |
| row_topic_assignment_status | 119,178 |
| stg_master_mapping_table | 5,956 |
| stg_master_market_definition | 16 |

### C-4 brand_activity raw (`jw_brand_activity_raw_stage`)
| 테이블 | COUNT(*) |
|---|---|
| raw_csd_channel_dynamics | 324,885 |
| raw_keyword_events | 71,603 |

- 접근 권한: `jw_mart_d2_writer` 계정은 `jw_brand_activity_stage` 접근 불가(1044). BA 계열은 root 계정(secret `galera-mariadb-galera`/`mariadb-root-password`)으로 조회. BA CronJob도 root 사용(env `MARIADB_USER=root`).

## D. 커밋 근거

- category refresh 스텝 추가 = develop `ec4f6e04` "Invoke refresh-live-categories after append-live in the tier2 crawl" (9c34a7d5 조상 확인).
- crawl 재설계 스케줄 = `7aacd49f` "Restore canonical crawl scheduling…".
- orchestrator 이미지 pyarrow+duckdb = `e984a057`, ingest hook가 그 이미지(v0.2.4) 참조 = `e3bafccb`.
