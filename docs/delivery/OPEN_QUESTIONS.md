# OPEN QUESTIONS — 조사로 해소 불가한 [확인 필요] 잔여 항목

| 항목 | 값 |
|---|---|
| 목적 | SI 납품 문서의 [확인 필요] 중 **조사로 해소 불가**한 항목(정책·판단·타 세션·데이터 대기)을 담당·질문 내용과 함께 분리 등재 |
| 기준(develop) | `e14d9eea` 이후 최신 |
| 작성 | jw market 세션 · 2026-07-18 |
| 근거 | 각 항목 실측 근거 = `evidence/openq_resolution_20260718.md` |

> **규칙.** 여기 등재된 항목은 코드/실측으로 채울 수 없다(정책 결정·타 세션 소관·미생성 데이터). 각 문서의 해당 자리는 이 파일로 링크된다. 실측으로 해소된 항목은 각 문서 본문에 직접 채워졌다(이 파일에 없음).

---

## A. PL / 플랫폼 판단 사안 (정책 — 조사로 해소 불가)

### A-1. BA 테이블 권한 — writer grant 부재로 root 사용 중
- **현황(실측)**: 서빙 backend는 `jw_brand_activity_stage`를 config 기본으로 읽으나, 실측 계정 `jw_mart_d2_writer`는 이 스키마 권한 부재. 그 결과 BA CronJob·운영 조회·본 문서 실측이 전부 **root 계정**(secret `galera-mariadb-galera` / `mariadb-root-password`)으로 수행 중.
- **질문(PL/플랫폼)**: writer 계정에 `jw_brand_activity_stage`/`_raw_stage` grant를 부여할 것인가, 아니면 root 사용을 현행 유지할 것인가? (최소권한 원칙 vs 현행 운용)
- **담당**: PL / 플랫폼(임호성)
- **관련 문서**: DOC-2b [확인 필요] #3, DOC-5 §7 · 근거 openq_resolution Q(PL), 의뢰서 W-3(ii)

### A-2. mart DB(MariaDB Galera) 백업 정책
- **현황(실측)**: 이 repo/실측 범위에서 DB 스냅샷·PITR·mysqldump 스케줄 미확인(코드·매니페스트 부재). Galera 3-노드 복제는 HA일 뿐 백업 아님.
- **질문(PL/플랫폼)**: 정기 논리 백업(mysqldump/PITR) 정책이 존재하는가? 없다면 도입 여부·주기·보존 정책은?
- **담당**: 플랫폼팀
- **관련 문서**: DOC-5 §5 · 의뢰서 Q-8 계열

### A-3. ★ 채팅 정본 이원화 — 운영 이미지 vs develop 발산
- **현황(실측, `evidence/chat_lineage_gap.md`)**: 운영 채팅 = 피처 브랜치 이미지 `da3fc153`("Keep deep reports clean at the evidence boundary", 브랜치 `codex/p3-file-brief-20260718`). **`da3fc15`는 develop의 조상이 아님** — merge-base `276a47b5`(2026-07-11), develop **+370** / 피처 **+350** 커밋 발산. 원인: chat 상시 규칙 "develop push 금지".
- **영향**: chat 3종 문서(DOC-1c·3b·4d)는 [운영 이미지 기준] 이중 표기 상태. develop에는 없고 운영(da3fc15)에만 있는 기능(예: `/deep` 모드·serving 202)이 존재.
- **질문(PL)**: 피처 브랜치(da3fc15 계열)를 develop에 머지할 것인가? 머지 시점·정본 일원화 방침은?
- **★ 통합 정합 게이트 규칙**: 통합 게이트는 이 **이중 기준**을 적용해 판정한다 — chat 문서가 인용한 `[운영 이미지 기준]` 코드는 develop에 없을 수 있으므로 "develop 미존재 코드 인용 = 오판"으로 처리 금지. develop 기준 file:line은 검증하되, 운영 전용 기능은 `[운영 이미지 기준]` 표기를 정상으로 인정.
- **담당**: PL(머지 판단) · jw chat 세션(정본 일원화 실행)
- **관련 문서**: DOC-1c·DOC-3b·DOC-4d, `evidence/chat_lineage_gap.md` · 의뢰서 W-3(i)

### A-4. 인입 훅 리허설 → 실적재 전환 시점
- **현황(실측)**: 증분 인입 훅 스택이 `INGEST_REHEARSAL_ROOT` 설정으로 **격리(리허설) 모드** 기동 중 — 실 mart 적재/refresh는 스킵됨. 실적재 전환 = 변수 해제(+ pyarrow 보강 등 선행).
- **질문(PL)**: 리허설→실적재 전환을 언제·어떤 게이트로 승인할 것인가?
- **담당**: PL(입회 게이트)
- **관련 문서**: DOC-1 §2.8 / [확인 필요] #6, DOC-5 §6

### A-5. GenOS 백엔드 이미지 승격 정확 커맨드
- **현황(실측)**: repo에 백엔드 이미지 승격 자동화 스크립트 없음 확인(캐시 blue-green·mart dimension promote 스크립트만 존재). 백엔드 승격은 GenOS 운영 UI/gen CAS(플랫폼 경로)로 수행.
- **질문(플랫폼)**: GenOS 상 백엔드 이미지 승격의 정확한 커맨드/절차(문서화된 런북)는? (본 문서는 관행만 기술 가능)
- **담당**: 플랫폼(GenOS 운영)
- **관련 문서**: DOC-1 §5.2 / [확인 필요] #3 · 근거 openq_resolution Q-6

---

## B. 데이터/측정 대기 (미생성 — 현재 채울 값 없음)

### B-1. shortlong(Agent2) 실전 비용
- **현황**: 첫 staging 실행 전이라 wf217 호출량·비용 데이터 미생성. RUNBOOK §6 비용표 기입란 공란.
- **해소 조건**: 첫 staging 실행 시 실측 기록 → 그 시점에 문서 채움(조사 아님, 실행 산출물 대기).
- **담당**: jw market(실행 시 기록)
- **관련 문서**: DOC-1 [확인 필요] #5, RUNBOOK §6

---

## C. 타 세션(SI/포탈) 회신 항목 (백엔드 조사로 해소 불가)

### C-1. 하이라이트 min5/max15 규칙
- **현황(실측)**: 시장분석 백엔드(`dynamic_market/`·`competitor_ranking.py`)에 min5/max15 리터럴 미확인. 시장분석 노출 상한은 경쟁 5개(+선택 브랜드) 계약으로만 확정.
- **질문(SI)**: min5/max15 규칙이 실제 화면에 적용되는가? 적용된다면 포탈 프론트/브랜드활동 트래커 어느 계층인가?
- **담당**: 포탈(SI) 프론트
- **관련 문서**: DOC-4a 부록 #1

### C-2. 심층분석 화면 섹션 문안 목록
- **현황(실측)**: 백엔드는 사전 계산 블록을 읽어 전달하는 서빙 어댑터(`deep_analysis_serving.py`). 화면 표시 섹션 문안(리포트 소제목 등)은 포탈 렌더 계층 또는 블록 생성 파이프라인 소관.
- **질문(SI)**: 심층분석 화면에 표시되는 정확한 섹션 문안 목록은?
- **담당**: 포탈(SI) 렌더 계층
- **관련 문서**: DOC-4a 부록 #2

---

## D. 타 세션(jw agent) 회신 항목 — 기고 문서 교정 필요 (본 라운드 무수정·보고만)

### D-1. DOC-2b §2 표 "생성 주체" 표기 교정
- **발견(실측)**: DOC-2b §2 표에서 `km_keyword_event_stage`의 생성 주체를 `auto_topic 계열 적재(data_source.py:19)`로 표기했으나, `data_source.py:19`는 **소비처**(토픽 생성 읽기)이지 적재 주체가 아니다. 실제 적재 주체 = `ingest_keyword.py` → `load_raw_staging.py:229-231`(본 라운드 [확인 필요] #2 자리에 기재).
- **처리**: jw agent 기고분이므로 §2 표 본문은 무수정. jw agent 세션이 표의 "생성 주체" 열을 교정할 것을 회신 요청.
- **담당**: jw agent 세션
- **관련 문서**: DOC-2b §2, [확인 필요]→확인 결과 #2
