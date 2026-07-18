# DOC-1b 개발 문서 — 크롤 · brand_activity 파이프라인

> **스켈레톤 (골격). 담당: jw agent 세션 · 상태: ⏳ 기고 대기.**
> 작성 전 [README §3 작성 규칙](README.md) 필독. 각 섹션의 `[기고 필요]` 안내대로 채운다.
> 추측 금지 — 확인 불가는 `[확인 필요]` 표기 후 말미 목록.

| 항목 | 값 |
|---|---|
| 기준 코드(develop) SHA | `[기고 필요: 기고 시점 develop SHA]` |
| 운영 리소스 | `[기고 필요: 크롤/orchestrator CronJob·이미지 generation 등]` |
| 생성일 | `[기고 필요]` |
| 문서 버전 | v0.1 (스켈레톤) |

---

## 1. 크롤 파이프라인 아키텍처

`[기고 필요]` tier1/tier2 크롤의 구조·데이터 계보·컨테이너 이미지·실행 경로(CronJob 실명·스케줄·args)를 서술. 근거: `deploy/k8s/crawler/*`·`crawl/`·실측 CronJob. tier1↔tier2 관계, canonical(강등) 사본과의 구분 포함.

## 2. brand_activity 생성 파이프라인 (Agent1~4)

`[기고 필요]` Agent1~4 각 역할·입력/출력·데이터 흐름·산출 테이블을 서술. 근거: 관련 스크립트 `파일:줄`·CronJob(`brand-activity-*`). topic/CSD 산출물이 서빙(DOC-2 브랜드활동)과 어떻게 연결되는지.

## 3. orchestrator 내부 구조

`[기고 필요]` stage 구성·full/incremental 모드·각 stage 게이트·epoch 기반 no-op(멱등) 동작을 서술. 근거: `pipeline/orchestrator/*`·`deploy/k8s/orchestrator/*`. 증분 훅(DOC-5 §3)이 부르는 refresh 경로와의 관계.

## 4. 이미지 · 배포

`[기고 필요]` 빌드 경로·digest 관리·이미지 동기 프로토콜(예: agent3 rev env 계약)·배포 절차를 서술. 근거: 배포 매니페스트·빌드 스크립트. 자격증명 값 금지.

---

## [확인 필요] 목록
`[기고 필요: 확인 불가 항목 정리]`

## 스크린샷/다이어그램 캡처 리스트
`[기고 필요: [화면/그림: ...] 플레이스홀더 목록]`
