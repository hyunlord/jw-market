# G_gates — [확인 필요] 해소 라운드 (2026-07-18)

의뢰서(CODEX SI [확인 필요] 해소, jw market 소관) 게이트 결과. read-only 조사 + 문서 갱신만(코드·배포·DB write 0).
base: develop `e812dd35`(e14d9eea 이후). 근거: `evidence/openq_resolution_20260718.md`.

## G-1 · [확인 필요] 마커 카운트 (전/후)

- "확인 필요" 문자열 전체(docs/delivery): **56 → 44**(−12). (44에는 OPEN_QUESTIONS.md의 개념 참조 8·README 규칙/맵 3 등 메타 참조 포함 — 실 open 마커 아님)
- **in-scope 실 open 마커(질문 단위) 19건 → 처리 완료**: 해소 10 · PL 등재 5 · 데이터 대기 1 · 타 세션 회신 3.

## G-2 · 채운 항목 실측 근거 병기 (추측 0)

전 해소 항목이 `evidence/openq_resolution_20260718.md`에 코드 `파일:줄`·in-mesh `kubectl` 캡처·site head 문자열로 근거 병기됨. 근거 없는 항목은 채우지 않고 OPEN_QUESTIONS로 분리. **PASS**

## G-3 · jw agent 기고분(DOC-2b·4c) 마커 자리 외 무수정

`git diff` 확인: DOC-4c·DOC-2b 변경은 [확인 필요] 목록 섹션 + 인라인 [확인 필요] 태그 자리로 한정. 표·§1~§4 서술 등 타 프로즈 무접촉. (DOC-2b §2 표의 생성주체 오기는 무수정·D-1로 보고만) **PASS**

## G-4 · 기존 5종·README 무회귀 (add-only/marker-only)

| 문서 | diff(+/−) | 성격 |
|---|---|---|
| DOC-1 | +13 / −11 | 마커 5자리(§2.2·§5.2·§5.4·머리·잔여 섹션)만 |
| DOC-2 | 0 | 무접촉 |
| DOC-3 | 0 | 무접촉('해당 항목 없음' 유지) |
| DOC-4a | +5 / −5 | 부록 표에 처리열·OQ 링크 add |
| DOC-4b | +11 / −4 | 부록 #1 해소 문구 추가 |
| DOC-5 | +2 / −2 | §5·§6 마커 2자리만 |
| README | add-only | 문서맵 2행 add(OPEN_QUESTIONS·evidence) |

**PASS** (본문 회귀 없음)

## G-5 · 전 스위트 신규 실패 0

`git status` 실측: 변경 파일 **전부 `.md`**(코드 `.py` 0). 테스트가 참조하는 delivery-gate 스크립트 없음(grep 무결과). 코드 표면 0 → 신규 테스트 실패 0(구성상 보장). **PASS**

---

## 항목별 해소/미해소 표 (질문 단위 19건)

| # | 항목 | 문서 | 처리 | 근거/등재 |
|---|---|---|---|---|
| 1 | 정렬/lookback end-to-end 배선 | DOC-4c | ✅ 해소 | Q-1: 심층분석 카드 전용(build_cache_deep_analysis→deep_analysis_runtime, dynamic_market/response_cache); BA 라우트는 토픽/CSD만 |
| 2 | 상위 N 절삭 기준 | DOC-4c | ✅ 해소 | Q-2: 서빙 top_n 기본5·1~10(topic_matrix:149,161,257), 저장단 무제한(sampling:25) |
| 3 | CSD product_details 정의 | DOC-4c | ✅ 해소 | Q-3: 원천 "Product Details" 열 정수(csd_core:13,62,111), TOTAL 행만 |
| 4 | DOC-2 컬럼타입 상호참조 | DOC-2b | ✅ 해소 | Q-4: DOC-2 §2.11+크롤 소절 앵커 확정 |
| 5 | km_keyword 적재 스크립트 | DOC-2b | ✅ 해소 | Q-5: ingest_keyword.py→load_raw_staging.py:229-231 |
| 6 | BA 서빙 계정 grant | DOC-2b | ⏳ PL | OQ **A-1**(writer grant vs root) |
| 7 | envsubst 치환 실이미지 | DOC-1 §2.2 | ✅ 해소 | Q-7: jw-market-backend-api@sha256:8e2501cd(실측), digest 드리프트 명시 |
| 8 | 백엔드 승격 스크립트 경로 | DOC-1 §5.2 | ✅ 해소(부분)+⏳PL | Q-6: repo無 확정; GenOS 커맨드 OQ **A-5** |
| 9 | 사이트 VERSION 괴리 | DOC-1 §5.4 | ✅ 해소 | Q-8: web/는 사이트 repo(Gitea) 소관 확정 |
| 10 | 사이트 SHA 36aa856b | DOC-1 머리 | ✅ 해소 | 미발견 재확인, HEAD 8ca9d987 사용 |
| 11 | shortlong 실전 비용 | DOC-1 #5 | ⏳ 데이터 대기 | OQ **B-1**(첫 staging 미실행) |
| 12 | 훅 리허설→실적재 전환 | DOC-1 #6 | ⏳ PL | OQ **A-4** |
| 13 | Grafana/Alertmanager 연동 | DOC-5 §6 | ✅ 해소 | 실측: monitoring 스택 존재·llmops 미배선(SM/Rule 0) |
| 14 | mart DB 백업 정책 | DOC-5 §5 | ⏳ PL | OQ **A-2** |
| 15 | 하이라이트 min5/max15 | DOC-4a #1 | ↗ 타 세션(SI) | OQ **C-1** |
| 16 | 심층분석 섹션 문안 | DOC-4a #2 | ↗ 타 세션(SI) | OQ **C-2** |
| 17 | unauthorized 화면 문구 | DOC-4b #1 | ✅ 해소 | site head page.tsx:27-73 실측 |
| 18 | ★채팅 정본 이원화 | (chat 3종) | ⏳ PL | OQ **A-3**(da3fc15 develop 미머지)·게이트 이중기준 규칙 |
| 19 | DOC-2b §2 생성주체 오기 | DOC-2b §2 | ↗ 타 세션(jw agent) | OQ **D-1**(무수정·보고) |

**요약: 해소 10 · PL 등재 5(A-1~A-5) · 데이터 대기 1(B-1) · 타 세션 회신 3(C-1·C-2·D-1).**
