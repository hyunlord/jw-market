# jw-market

JW 시장분석 AI 플랫폼의 모노레포 — 시장 데이터 ETL·AI 분석 파이프라인, 채팅 에이전트,
크롤/브랜드활동 파이프라인, 데이터 입력 사이트를 한 저장소에서 관리한다.

> 정본은 GitHub `hyunlord/jw-market`(push=jw-private), 사내 Gitea `jw-market/jw-market`는
> 시간당 동기화되는 read-only 미러다. (정본 전환은 별도 게이트 — `BRANCH_POLICY.md` 및 이전 절차 참조.)

## 1. 시스템 개요

- **데이터 → 마트 (ETL)**: UBIST·IQVIA·내부 데이터를 수집·정규화해 서빙 마트로 적재
  (`pipeline/etl` 스테이지 s0~s7). 월간 운영은 `RUNBOOK_MONTHLY.md`.
- **AI 분석 (오케스트레이터)**: 마트 위에서 원인/전략/심층 분석 번들을 생성하는 6-스테이지
  파이프라인 (`pipeline/scripts/ai_analysis`, `pipeline/orchestrator`).
- **크롤 · 브랜드활동**: 뉴스/이벤트 크롤과 토픽·브랜드활동 집계 (`crawl/`,
  `pipeline/scripts/analysis/brand_activity`).
- **채팅 에이전트**: 시장/일반 질의에 마트·툴을 사용해 답하는 에이전트
  (`chat/jw-chat-agent-poc`).
- **데이터 입력 사이트**: 시장 데이터 업로드·확정·인입 훅 연동 포털
  (`pipeline/scripts/ingest_hook` 백엔드 훅 + 별도 Gitea repo `jw-data-input` 프론트).
- **배포**: `deploy/k8s`(GKE `llmops` 네임스페이스) — 이미지는 Artifact Registry digest 핀.

## 2. 저장소 구조

```
api/        서빙 API
chat/       채팅 에이전트(jw-chat-agent-poc) + wf301 브리지
crawl/      크롤러 + 재점수(ops)
pipeline/   ETL·오케스트레이터·분석·인입 훅·배포 스크립트
deploy/     k8s 매니페스트·Dockerfile
data/       입력 데이터 루트(VM)
docs/       설계·인수·운영 문서 (아래)
tests/      파이썬 테스트 스위트
```

## 3. 문서 위치 안내

| 목적 | 위치 |
|---|---|
| 브랜치 정책(금지 lineage·정본) | `BRANCH_POLICY.md` |
| 월간 운영 런북 | `RUNBOOK_MONTHLY.md` |
| 일간 트랙 운영 정본 | `docs/OPS_TRACKS_READINESS.md` |
| 시스템 아키텍처 | `docs/delivery/DOC-1_개발문서_시스템아키텍처.md` |
| 크롤·BA 파이프라인 | `docs/delivery/DOC-1b_개발문서_크롤_BA파이프라인.md` |
| 채팅 에이전트 | `docs/delivery/DOC-1c_개발문서_채팅에이전트.md` |
| DB 스키마 정의 | `docs/delivery/DOC-2_DB_스키마정의서.md` (+ `DOC-2b` 크롤/BA 테이블) |
| API 명세 | `docs/delivery/DOC-3_API_명세서.md` (+ `DOC-3b` 채팅 API) |
| 사용설명서 | `docs/delivery/DOC-4a_사용설명서_시장분석.md` · `DOC-4b_사용설명서_jw-data-input.md` |
| 설계·계획·연구·런북 | `docs/design/` · `docs/plans/` · `docs/research/` · `docs/runbooks/` |

## 4. 개발 참고

- 정본 회귀: `python3 pipeline/scripts/gates/canonical_regression.py`.
  정확한 범위·baseline 갱신 규약은
  `pipeline/scripts/gates/CANONICAL_REGRESSION.md`를 따른다.
- `chat/jw-chat-agent-poc`와 `chat/wf301-vdb-bridge`는 독립 컴포넌트로 각 디렉터리의
  테스트 명령을 별도로 실행한다.
- 작업 브랜치는 `develop`. 금지 lineage(`BRANCH_POLICY.md`)는 병합하지 않는다.
- 로컬 경로를 코드에 하드코딩하지 않는다. 백업 등 사용자별 경로는 환경변수로
  (예: 브랜드활동 audit 백업은 `JW_BACKUP_DIR`, 미설정 시 `~/jw_backups`).
