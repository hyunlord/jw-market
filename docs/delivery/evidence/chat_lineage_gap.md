# chat 계보 갭 실측 — develop 코드 ≠ 운영 이미지 소스 (2026-07-18)

## 근본 사실 (merge-base)
- 운영 이미지: `jw-chat-agent-poc:chat-838-p1-deep-finish-da3fc15-20260718@sha256:efec7f94…`
- 이미지 소스 커밋 = **`da3fc153`** "Keep deep reports clean at the evidence boundary" (2026-07-18 13:48 KST)
- 소속 피처 브랜치: `codex/p3-file-brief-20260718` (origin·jw-private)
- **da3fc15는 develop의 조상이 아님** (merge-base `276a47b5`, 2026-07-11). develop +370 / 피처 +350 커밋 분기.
- 원인: chat 상시 규칙 "❌ develop push" → 오늘까지 chat 작업분 전부 피처 브랜치. develop chat = 구버전.
- 문서 정본 인용 기준 = develop(9c34a7d5 계열); 운영 실행체 = da3fc15. **file:line 인용은 develop 기준이라 검증 가능하되, 운영에만 있는 기능은 `[운영 이미지 기준]` 표기.**

## 운영 이미지(da3fc15)에만 있고 develop에 없는 기능 (교정 3건 근거)

### ① 딥리서치 모드 `/deep` — 존재 (develop ABSENT)
- develop: `orchestrator/deep_research.py` **없음**(`git cat-file -e origin/develop:…/deep_research.py` = ABSENT). → 내 초판의 "deep 모드 없음" 서술은 **develop 기준으론 맞으나 운영 기준으론 오류**.
- da3fc15: `orchestrator/deep_research.py:10` `_DEEP_TRIGGER = re.compile(r"^/deep(?:[ \t]+|\n|$)")` — **질문 텍스트 맨 앞 `/deep ` 접두사 트리거**(URL 엔드포인트 아님). `parse_deep_research_request`가 접두사 제거 후 활성.
- `DeepResearchToolPlanner.decide`(:31) = 한 번의 **광범위·독립 근거 배치**(get_metric series·get_market_scope·get_brand_series 24pt 등 다도구 병렬).
- 딥 전용 진행표시: `common/timing.py:69-77` "딥리서치 전체/질문 분석/조사 설계/자료 수집/근거 정리/종합 분석" 스테이지(loop.py:89,169,212,262,441 `deep_research_*`).
- 보고서형 답변 강제: `service/answer_safety.py:1245` `ensure_deep_research_structure` — "## 핵심 요약 / ## 종합 분석" 구조.

### ② 딥 전용 serving 202 — 존재 (develop ABSENT), + 라이브 serving 실측
- da3fc15 `genos_config.py:12,21` `GENOS_DEEP_SERVING_ID`(기본 **202**), `GENOS_DEEP_BEARER_TOKEN`; `genos_client.py:803-804` 딥 타임아웃 `GENOS_DEEP_TIMEOUT_S`=180s / `GENOS_DEEP_TOTAL_BUDGET_S`=300s.
- develop genos_config: DEEP serving 정의 **없음**(final 514/planner 508/fallback 517만).
- ★ **라이브 env 실측**(deployment jw-chat-agent-poc, 자격값 아님·id만):
  | env | 라이브 값 | 코드 기본값 |
  |---|---|---|
  | GENOS_FINAL_SERVING_ID | **190** | 514 |
  | GENOS_PLANNER_SERVING_ID | **190** | 508 |
  | GENOS_SERVING_ID(fallback) | **190** | 517 |
  | GENOS_DEEP_SERVING_ID | **202** | 202 |
  → 초판이 쓴 514/508/517은 **코드 기본값**이며 라이브는 전부 **190**(딥만 202). 딥 202 = gemini-3.1-pro-preview(PL 확인·메모리 serving202 근거).
- 라이브 기타 실측: MCP resource id 169/184/250/253(=코드 기본), `JW_CHAT_FILE_SEARCH_BASE=http://code-serving-235:8080`, `WEB_SEARCH_PROVIDER=tavily`, `CHAT_DEEP_NEWS_MODE=cache`.
  (주의: MCP standby 배포명 112/190/196/127은 k8s deployment 이름이지 GENOS resource id가 아님 — 초판 표의 "라이브 standby" 열은 이 둘을 혼동시킬 수 있어 교정.)

### ③ 정형 파일 질답 (235 브리지 SQL 경로) — 존재 (develop ABSENT)
- da3fc15 `wf301-vdb-bridge/src/` 신규: `file_sql/`(service·registry·policy·models·config), `xlsx_sql_route.py`(550줄), `xlsx_preprocessor.py`(+452), `pdf_vlm.py`(361), `upload_status.py`·`upload_ownership.py`·`upload_machine_card.py`, CHSO 멀티시트 SQL 테스트(`test_chso_multisheet_sql.py` 632줄).
- 즉 운영 235 경로는 **XLSX/CSV 정형 파일을 SQL 라우팅으로 질답**(스키마 카드·SQL 집계). PDF는 VLM.
- develop 235(구버전)엔 이 경로 없음 → 내 DOC-4d의 "정형 파일 거부"는 **로컬 RAG 경로 한정 사실**이나 "엑셀 업로드 질답 불가"로 읽히는 오해. 운영 파일질답(235 경로)은 정형·비정형 모두 지원(현행 실측 기준).
- ★ 형식별 상세 매트릭스는 여전히 235 소관 문서로 [확인 필요](chat repo 밖) — "전부 지원" 단정은 금지.

## 교정 방침 (지시 §1~3)
- 3종 머리에 계보 갭 각주(jw market DOC-1 선례 형식) + `[운영 이미지 기준]` 표기 규약.
- ① deep "모드 없음"→"/deep 접두사 트리거 모드, 운영 이미지(da3fc15) 포함·develop 미머지". EP 없음은 유지.
- ② serving 표에 딥 202 추가 + 514/508/517은 코드기본·라이브 전부 190 명시.
- ③ 정형파일: 로컬 RAG 거부는 유지하되 운영 235 경로 정형 지원을 명확히(오해 문장 교정), 형식 전수는 [확인 필요].
- 기존 5종 무수정. 근본(develop 미머지)은 PL 별건(지시 §5).
