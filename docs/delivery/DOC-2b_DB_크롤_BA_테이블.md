# DOC-2b DB 문서 — 크롤 · brand_activity 테이블

> **스켈레톤 (골격). 담당: jw agent 세션 · 상태: ⏳ 기고 대기.**
> 작성 전 [README §3 작성 규칙](README.md) 필독. 실스키마(SHOW CREATE) 기반으로 채운다.
> 추측 금지 — 확인 불가는 `[확인 필요]` 표기 후 말미 목록.

| 항목 | 값 |
|---|---|
| 기준 코드(develop) SHA | `[기고 필요]` |
| 대상 DB | `[기고 필요: 예 jw_mart_d2_stage_... / jw_brand_activity_stage — 실측 확정]` |
| 캡처일 | `[기고 필요]` |
| 문서 버전 | v0.1 (스켈레톤) |

> 형식 참고: 기존 `DOC-2_DB_스키마정의서.md`(컬럼표=SHOW CREATE 원문, 인덱스, 행수, 생성 주체). 중복 테이블은 상호 참조하고 여기서는 크롤/BA 소관만 다룬다.

---

## 1. 크롤 계열 테이블

`[기고 필요]` news_raw·events·events_raw·event_brand_scores 등 크롤 산출 테이블의 스키마·생성 주체(스크립트 `파일:줄`)·갱신 주기를 서술. 각 테이블 SHOW CREATE·행수(캡처 시점).

## 2. 토픽 / brand_activity 계열 테이블

`[기고 필요]` `jw_brand_activity_stage`의 mart_brand_activity_topics·row_topic_assignment(및 share_view/status)·csd_channel_dynamics_stage·km_keyword_event_stage 등. 서빙(DOC-3 브랜드활동 EP)이 읽는 테이블과 집계 테이블의 구분.

## 3. staging · 중간 산출 테이블

`[기고 필요]` tier2_match_staging·`*_stage_*`·`*_mig_stg_*` 등 작업/중간 테이블을 "정본 아님"으로 구분해 목록화. 어느 단계가 만들고 언제 정리되는지.

## 4. 테이블 관계 · 데이터 흐름

`[기고 필요]` 텍스트 ERD(크롤 raw → events → scores → BA 집계 → 서빙)와 재적재·dedup 지점. 실컬럼 기반으로만.

---

## [확인 필요] 목록
`[기고 필요]`
