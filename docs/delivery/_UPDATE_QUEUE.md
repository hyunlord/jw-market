# SI 문서 반영 큐 (jw agent 소관) — _UPDATE_QUEUE

| 항목 | 값 |
|---|---|
| 목적 | 결정·구조 변경 시 SI 문서 반영 누락 방지(정본화 원칙). 세션 밖 소실 방지를 위해 repo 등재 |
| 범위 | jw agent 소관 4종: `DOC-1b`·`DOC-2b`·`DOC-4c`·`DOC-5 §8` |
| 작성 | jw agent 세션 · 2026-07-19 · 기준 develop `f2eca6a1` |
| 구성 | **A**(반영 완료 — 위치 기록) / **B**(대기 — 트리거·대상) |
| 갱신 규칙 | B 3건 이상 누적 **또는** D-3(훅 실적재 전환) 착지 시 일괄 갱신 라운드. 갱신 시 대상 문서 머리 기준 SHA·생성일 갱신 + 해당 문서 자기 대조표(누락 0) 재확인 |
| 관련 | 정책·타 세션 대기분(=[확인 필요])은 jw market의 [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md)와 교차(B8) |

---

## A. 반영 완료 (2026-07-19 라운드 — ETL 스코프 조사 audit `6a61feb8`)

전부 **현 시점 코드 사실**이며, 향후 스코프 축소·S3 직독화 시 갱신 대상(→ B7).

| # | 반영 내용 | 반영 위치 | 근거(file:line, develop `f2eca6a1`) |
|---|---|---|---|
| A1 | 증분은 s1에서 종료 · s2~s7 전량 재계산(파일 1개 추가 → s4~s7 전 브랜드·전 기간) | DOC-1b §3.5(스테이지 표) | `s1_load.py:57,133,25,29`·`s4_mart.py:160`·`general_compute.py:41-42,58-59`·`s5_mart.py:179`·`s6_cache.py:38`·`s7_bridge.py:33`·`s2_catalog.py:57-90`·`s3_enrich.py:35-44` |
| A2 | s0 verify 4그룹 전수 요구 → 단일 소스 격리 차단 · 우회 `--stage s1`(--file 존중) | DOC-1b §3.5 · DOC-5 §8 주석 | `s0_verify.py:39-49,64-65,69-72`·`ubist_loader.py:283-285` |
| A3 | MinIO 직독 불가(로컬 rglob·다운로드 모델·storage 미사용·boto3 부재) | DOC-1b §3.6 | `ubist_loader.py:36,289`·`storage.py:3-4,48,94-101,117`·`v0.2.5-51e2c687` boto3 ABSENT 실측 |
| A4 | ETL s0~s7 전용 CronJob 부재(훅 job_runner/수동) | DOC-1b §3.5 · DOC-5 §8 주석 | `deploy/k8s` `pipeline.etl.run` grep 0 |
| A5 | `mart_general_brand_metric.metric_history`=브랜드당 전기간 → period-only 축소 불가(최소 단위 영향 브랜드) | DOC-2b §5 | `general_rows.py:57,82-85,105,149` |

## B. 대기 (트리거 도달 시 반영 — 현재 본문에 박지 않음)

| # | 대기 내용 | 트리거 | 대상 문서 |
|---|---|---|---|
| B1 | 소스 제공 방식(PVC 마운트 등, ⓐ 변형) 확정 | jw market 확정 + 초기 적재 | DOC-1b §3.6 · DOC-5 |
| B2 | 기존 소스 트리(5년치) 물리 위치 실측 | 실측 라운드 | DOC-1b §3.6 · DOC-5 |
| B3 | ★ 훅 활성화 후 갱신 주기 변경(월간 배치 → 업로드 시 반영) | D-3(실적재 전환) 이후 | DOC-4c(사용자 대상 §2) |
| B4 | CronJob 3종 삭제(poll-daily·csd-sensor·brand-activity-run) | 훅 착지 | DOC-5 §8 |
| B5 | `-canonical` crawl 2종 삭제 | 안정 1주 후 PL | DOC-5 §8 |
| B6 | 동시성 계약(target_db run_id 접미·work_dir 분리·승격 직렬화) | jw market 구현 | DOC-2b · DOC-5 |
| B7 | S3 직독화(로더 개선) — 성사 시 A3·B1 무효화 | R-1/D-3 착지 후 별도 라운드 | DOC-1b §3.6 전면 |
| B8 | [확인 필요] 해소분(화면 배선·top-N·CSD 지표·km_keyword 적재·BA grant·컬럼 타입) — 상당수 jw market OPEN_QUESTIONS/openq_resolution으로 해소됨(DOC-2b·4c 반영). 잔여는 그쪽 회신 | jw market 회신 | DOC-2b · DOC-4c |
| B9 | `v0.2.5-51e2c687` 이미지 빌드 주체·인가 확정 (2026-07-19 이미지 정합 라운드 — 계보·deps·코드 델타·digest `a362ceb8`는 실측 완료·DOC-1b §4·footnote·[확인 필요]5 반영; **잔여** = 빌드/push actor·jw market "base=v0.2.4 고정" 진술 유효성·재빌드 통지 프로토콜) | jw market 회신 | DOC-1b §4·footnote |

> B8 주의: 2026-07-18 이후 jw market이 openq_resolution으로 화면 배선·top-N·CSD 지표·km_keyword 적재 경로를 해소해 DOC-2b·DOC-4c 본문에 반영됨. BA grant·컬럼 타입 원문은 [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) 잔여.
