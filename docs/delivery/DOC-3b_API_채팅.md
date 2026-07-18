# DOC-3b API 문서 — 채팅

| 항목 | 값 |
|---|---|
| 기준 코드(develop) SHA | `9c34a7d5` (README·스켈레톤 기준; 작성 시 worktree HEAD `1864e929`, chat 코드 정본 커밋 `0900ed5e` "Isolate jw-chat under a chat top-level folder" → 경로 `chat/jw-chat-agent-poc/`) |
| 대상 서비스 | deployment `jw-chat-agent-poc` (ns `llmops`), svc `jw-chat-agent-poc` `8080→8080`, FastAPI `root_path=/jw-chat-agent`(app.py:216), title `JW Chat Agent POC` v`0.2.0` |
| 운영 이미지 | `jw-chat-agent-poc:chat-838-p1-deep-finish-da3fc15-20260718@sha256:efec7f94…` (3 replicas, HPA min2/max4 mem60%) |
| 실호출 캡처일 | 2026-07-18 (라이브 identity·EP는 실측; 응답 본문 예시는 [확인 필요] — §4 참조) |
| 문서 버전 | v1.0 |

> 형식 참고: 기존 `DOC-3_API_명세서.md`. 본 문서는 **채팅 서비스**만 다룬다. 시장분석 백엔드 EP는 DOC-3, 아키텍처는 [DOC-1c](DOC-1c_개발문서_채팅에이전트.md) 참조.
> 모든 파라미터·오류는 `chat/jw-chat-agent-poc/jw_chat_agent_poc/service/` 실코드 `파일:줄`에 근거한다. 자격증명 값은 기재하지 않는다.
>
> **★ 계보 갭 각주.** `파일:줄` 인용은 **develop**(`9c34a7d5` 계열) 기준이나, **운영 실행체는 이미지 `da3fc15`**(피처 브랜치 `codex/p3-file-brief-20260718`, develop 미머지 — merge-base `276a47b5`). develop chat은 구버전이라, 운영에만 있는 기능(딥리서치 `/deep` 접두사 트리거·딥 serving `202`)은 `[운영 이미지 기준]`으로 표기한다. HTTP EP 표(§1.1)는 두 계보 공통(EP 추가 없음)이다. 근거·상세: [DOC-1c](DOC-1c_개발문서_채팅에이전트.md) 계보 각주, `evidence/chat_lineage_gap.md`.

---

## 1. 채팅 EP 명세

### 1.1 엔드포인트 전수 (`service/app.py`)

| # | 메서드 | 경로 | 핸들러 | 요청 | 응답 | 스트리밍 | 근거 |
|---|---|---|---|---|---|---|---|
| 1 | GET | `/healthz` | `healthz` | 없음 | `HealthResponse`(`{status:"ok"}`) | 아니오 | app.py:240 |
| 2 | GET | `/__version` | `version` | 없음 | `version_payload()` dict | 아니오 | app.py:244 |
| 3 | POST | `/chat` | `chat` | `ChatRequest` + (공개 호스트만)`X-API-Key`/`X-Portal-User-Id` | `ChatAccepted` | 아니오 | app.py:248 |
| 4 | **POST** | **`/chat/answer`** | `chat_answer` | `ChatRequest` + 동일 헤더 | **`ChatAnswer`**(완성 JSON) | 아니오 | app.py:280 |
| 5 | GET | `/chat/stream` | `chat_stream` | 쿼리 `session_id`·`question`·`external_mode`(기본 `live`)·`conversation_id` | `text/event-stream`(SSE) | 예 | app.py:325 |
| 6 | GET | `/`·`/index.html`·`/{frontend_path}` | `frontend_*` | 경로 | `FileResponse`(정적 프론트) | 아니오 | app.py:378 |

- **★ 포탈/wf301이 호출하는 것 = `/chat/answer`** (동기 완성 JSON). 스트리밍 UI는 `/chat/stream`(SSE).
- **`/deep`은 URL 엔드포인트가 아니다** — HTTP 라우트 표에 `/deep`은 없다(두 계보 공통). 대신 **`/deep`은 `/chat/answer`·`/chat/stream`의 `question` 본문 맨 앞 접두사 트리거**다: `[운영 이미지 기준]` 운영 이미지 da3fc15에서 질문이 `/deep `로 시작하면 딥리서치 모드가 발동한다(`orchestrator/deep_research.py:10` `^/deep`, 다도구 병렬·보고서형 답변·딥 serving 202). 즉 **"엔드포인트 없음"은 맞으나 "모드 없음"은 오류** — 딥리서치 모드는 운영에 존재하되 URL이 아니라 질문 접두사로 진입한다. (develop 코드엔 `deep_research.py` 부재 = 미머지.) 상세: DOC-1c §1.2.
- (develop 공통) "심층분석" 도구 `deep_analysis_events`(뉴스/이슈 curated)는 위 딥리서치 모드와 별개로 답변 라우팅으로 발동한다(`tools/deep_analysis/news.py:21`).

### 1.2 요청 스키마 `ChatRequest` (`service/models.py:8-30`)

| 필드 | 타입 | 필수 | 기본값 | 의미 |
|---|---|---|---|---|
| `question` | str | 예(빈 문자열 허용) | — | 사용자 질문(파일 전용 업로드 확인 시 빈 문자열 허용) |
| `document_paths` | tuple[str,…] | 아니오 | `()` | 세션 첨부 파일 경로 |
| `file_context` | str \| None | 아니오 | None | 파일 검색 결과 컨텍스트(보통 wf301 bridge가 자동 채움) |
| `external_mode` | str | 아니오 | `"live"` | 외부 API 모드. `"fixture"`=격리 검증용 고정 응답 |
| `conversation_id` | str \| None | 아니오 | None | 대화 식별자(pending clarification 상태 이어감) |

- `question` 공백 **AND** 파일 신호(`document_paths`/`file_context`) 없음 → **400** "질문 또는 파일 업로드가 필요합니다."(app.py:291-292)

### 1.3 응답 스키마 `ChatAnswer` (`/chat/answer`, `models.py:41-49`)

| 필드 | 타입 | 의미 |
|---|---|---|
| `text` | str | 마크다운 답변 본문 |
| `charts` | list[dict] | 근거 기반 차트 스펙 |
| `trace` | dict | 라우팅·도구·타이밍 추적 envelope |
| `sources` | tuple[str,…] | 출처 라벨 |
| `conversation_id` | str \| None | 유지된 대화 id |
| `file_sources` | list[dict] | 업로드 파일 그라운딩 시 `[{file_name, document_id?}]`(기본 `[]`) |

- `/chat` 응답 `ChatAccepted`: `session_id`·`conversation_id`·`sources`(`models.py:33-38`).
- **SSE 이벤트 계약**(`/chat/stream`, app.py:94-113, 941-952): `conversation` → `step`(live 시 stage 진행) → `sources` → `file_sources` → `delta`/`markdown_block` → `charts` → `timing` → `trace` → `done`. `session_id` replay 시 `step` 미발생.

## 2. 포탈 ↔ 채팅 ↔ 백엔드 호출 계약

### 2.1 경로
- **포탈(BFF) → wf301 브리지(`code-serving-235`) → 채팅(`/chat/answer`)**. wf301 브리지가 파일 세션 격리와 `file_context` 주입을 담당하고, 채팅 서비스를 in-mesh(`http://jw-chat-agent-poc:8080`)로 호출한다. (브리지 내부 구조는 DOC-1c §2.)
- 채팅은 답변 조립 중 필요 시 시장분석 백엔드/외부 MCP를 호출한다(DOC-1c §3).

### 2.2 인증 게이트 (`_require_direct_route_api_key`, app.py:180-204)
- 요청 Host/`X-Forwarded-Host`가 `DIRECT_ROUTE_AUTH_HOSTS`(기본 `admin.dev.ai.jwhealthcare.com`·`jwai-dev.jwhealthcare.com`, app.py:75-80)에 속하면 **공개 요청** → `X-API-Key`가 `DIRECT_ROUTE_API_KEY`(env, 값 미기재)와 일치해야 함.
  - 키 미설정 → **503** "direct route API key is not configured"(app.py:189-190)
  - 키 불일치/누락 → **401** "invalid API key"(app.py:191-192)
  - `X-Portal-User-Id` 검증 실패 → **400**(app.py:199-200)
- **그 외 호스트(in-mesh: wf301→`jw-chat-agent-poc:8080`)는 API 키 불요**(`portal_user_id=None` 통과, app.py:186-187). → **실사용(포탈) 경로는 in-mesh이므로 키 없이 통과**; 공개 호스트 직접 호출만 키를 요구한다.
- CORS: `allow_origins=["*"]`, credentials=False(app.py:217-223).

## 3. 오류 응답 · 한계

| 상황 | 코드/동작 | 근거 |
|---|---|---|
| 빈 질문+파일 없음 | 400 "질문 또는 파일 업로드가 필요합니다." | app.py:291-292 |
| 동시성 초과 | **503** `BUSY_MESSAGE`="현재 사용자가 많습니다. 잠시 후 다시 시도해주세요." | app.py:307-308; concurrency.py:8 |
| 스트림 busy | SSE `delta`+`error{type:ServiceBusy}`+`done:error` | app.py:935-938 |
| 알 수 없는 `session_id` | 404 "unknown session_id" | app.py:404-405 |
| 공개 호스트 키 미설정/불일치/portal id 오류 | 503 / 401 / 400 | app.py:189-200 |
| GenOS 답변 생성 실패 | **폴백**: `finalized_fallback_fact_answer`로 확정 fact만 결정적 조립 | app.py:1038-1039; answer_safety.py:152-162 |
| 대화 히스토리 DB 기록 실패 | 삼켜 로깅만, 답변 정상 반환 | app.py:977-978 |

### 3.1 동시성·타임아웃 상수
| 대상 | 값 | 근거 |
|---|---|---|
| 동시 답변 상한 `CHAT_MAX_CONCURRENCY` | 기본 **3**(BoundedSemaphore) | concurrency.py:13 |
| 대기 상한 `CHAT_QUEUE_WAIT_S` | 기본 **10.0초** → 초과 시 503 | concurrency.py:14 |
| 업로드 파일 검색(wf301 bridge) | 기본 **3초** | file_search_client.py:29 |
| 외부 MCP 호출 | 기본 **12초** | tools/external/client.py:71 |
| 웹검색(tavily / serper·brave) | min(timeout, **5초** / **10초**), 최대 결과 **5건** | client.py:37-38,276,302,326 |
| 히스토리 DB(MySQL) | connect 3s / read 5s / write 5s | conversation_history.py:158-160 |
| 세션 저장 LRU `SESSION_STORE_MAX` | 기본 **500** | app.py:128,144 |
| 대화 상태 TTL / max_turns / pending TTL | **600초** / **5** / **180초** | conversation.py:38-45 |
| 딥리서치 `[운영 이미지 기준]` | 합성 타임아웃 **180초** / 총예산 **300초** | da3fc15 `genos_client.py:803-804` |

### 3.2 외부 API 실패 시 부분근거 처리 (`tools/external/client.py`)
- MCP 실패 → `status="error"` 릴레이, **fail-closed**(clinicaltrials는 `external_claim_policy:"fail_closed_error"`, client.py:237-241). 결과 없음 → `status="no_data"` "MCP 조회 결과 없음"(client.py:447-449).
- 웹검색은 **미검증(unverified)** 분리: 성공 시 `external_claim_policy:"web_results_unverified"` + `verification_notice`="웹 검색 결과(미검증): URL과 snippet을 출처로 분리 표시하고 내부 fact로 승격하지 않습니다."(client.py:730-731). 키 없음 → `missing_key`(미실행).
- MCP 리소스 기본값(env override): openFDA 184·NeDrug 250·HIRA 253·ClinicalTrials 169(client.py:40-43).

---

## [확인 필요] 목록
1. `/chat/answer` **실응답 본문 예시**: in-mesh 실호출 캡처가 필요하나, `_require_direct_route_api_key`·wf301 file 세션 의존으로 read-only 단발 캡처는 이번 회차 미수행. 포탈 동등 경로(wf301) 실호출 1건을 evidence로 보존 예정(SSH 슬롯 여유 시). 스키마·오류계약은 코드로 확정됨.
2. deep(딥리서치) **체감 소요시간·비용**: `[운영 이미지 기준]` 타임아웃 180초·총예산 300초는 확정(§3.1). 실제 체감 시간·질의당 비용은 측정치 필요(코드 상수 아님).
3. **계보 정합**: 운영 이미지 `da3fc15` develop 미머지 — 딥리서치 모드·serving 202 등 운영 기능이 develop 코드에 없음(PL 별건). DOC-1c 계보 각주 참조.
