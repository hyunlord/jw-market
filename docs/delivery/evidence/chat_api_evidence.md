# JW Chat Agent — HTTP API·오류/한계·파일질답 근거표

소스: develop worktree `/tmp/jwm-develop-docs/chat/jw-chat-agent-poc/`
루트 경로(root_path): `/jw-chat-agent` (`service/app.py:216`), FastAPI title=`JW Chat Agent POC` version=`0.2.0`

---

## 1. 엔드포인트 전수 (service/app.py)

| # | 메서드 | 경로 | 핸들러 | 요청 | 응답 모델 | 스트리밍 | 근거 |
|---|---|---|---|---|---|---|---|
| 1 | GET | `/healthz` | `healthz` | 없음 | `HealthResponse` (`{status:"ok"}`) | 아니오 | app.py:240-242 |
| 2 | GET | `/__version` | `version` | 없음 | dict `version_payload()` | 아니오 | app.py:244-246 |
| 3 | POST | `/chat` | `chat` | `ChatRequest` (body) + `X-API-Key`/`X-Portal-User-Id` 헤더(공개 호스트만) | `ChatAccepted` | 아니오 | app.py:248-278 |
| 4 | POST | `/chat/answer` | `chat_answer` | `ChatRequest` (body) + 동일 헤더 | `ChatAnswer` | 아니오(완성 JSON) | app.py:280-323 |
| 5 | GET | `/chat/stream` | `chat_stream` | 쿼리: `session_id`,`question`,`external_mode`(기본 live),`conversation_id` + 동일 헤더 | `text/event-stream` (SSE) | 예 | app.py:325-376 |
| 6 | GET | `/`, `/index.html`, `/{frontend_path:path}` | `frontend_index`/`frontend_file` | 경로 | `FileResponse` (정적 프론트) | 아니오 | app.py:378-386 |

- **★ 포탈/wf301이 호출하는 것 = `/chat/answer`** (동기 완성 JSON). 메모리 노트의 wf301 bridge가 `POST /chat/answer` 호출.
- `/deep` 라는 별도 엔드포인트는 **존재하지 않음**. "심층분석"은 내부 `deep_analysis_events`(뉴스/이슈 curated) 도구이며 답변 라우팅으로만 발동(별도 EP 아님). 근거: app.py 전체 라우트에 `/deep` 없음; `tools/deep_analysis/news.py:21` `DEEP_ANALYSIS_EVENTS_SOURCE="deep_analysis_events"`.

### 인증/라우팅 게이트 (`_require_direct_route_api_key`, app.py:180-204)
- 요청 Host/X-Forwarded-Host가 `DIRECT_ROUTE_AUTH_HOSTS`(기본 `admin.dev.ai.jwhealthcare.com`, `jwai-dev.jwhealthcare.com`, app.py:75-80)에 속하면 **공개 요청**으로 판정 → `X-API-Key`가 `DIRECT_ROUTE_API_KEY` env와 일치해야 함.
  - key env 미설정 시 **503** "direct route API key is not configured" (app.py:189-190)
  - key 불일치/누락 시 **401** "invalid API key" (app.py:191-192)
  - portal_user_id 검증 실패 시 **400** (app.py:199-200)
- 그 외 호스트(내부 in-mesh: wf301→`jw-chat-agent-poc:8080`)는 API 키 불요 → `portal_user_id=None`로 통과 (app.py:186-187).
- CORS: `allow_origins=["*"]`, credentials=False (app.py:217-223).

---

## 2. 요청 스키마 `ChatRequest` (service/models.py:8-30)

| 필드 | 타입 | 필수 | 기본값 | 의미 | 근거 |
|---|---|---|---|---|---|
| `question` | str | 예(min_length=0, 빈 문자열 허용) | — | 사용자 질문. 파일 전용 업로드 확인 시 빈 문자열 허용 | models.py:11-14 |
| `document_paths` | tuple[str,...] | 아니오 | `()` | 세션 첨부 파일 경로 목록 | models.py:15-18 |
| `file_context` | str \| None | 아니오 | None | 파일 검색 결과 컨텍스트 문자열(보통 235 bridge가 자동 채움) | models.py:19-22 |
| `external_mode` | str | 아니오 | `"live"` | 외부 API 모드. `"fixture"`=격리 검증용 고정 응답 | models.py:23-26 |
| `conversation_id` | str \| None | 아니오 | None | 세션/대화 식별자. 이전 pending clarification 상태 이어감 | models.py:27-30 |

- `question` 공백 AND 파일 신호(document_paths/file_context) 없음 → **400** "질문 또는 파일 업로드가 필요합니다." (app.py:256-257, 291-292)

## 응답 스키마

### `ChatAnswer` (`/chat/answer`, models.py:41-49)
| 필드 | 타입 | 의미 |
|---|---|---|
| `text` | str | 마크다운 답변 본문 |
| `charts` | list[dict] | 근거 기반 차트 스펙 배열 |
| `trace` | dict | 라우팅·도구·타이밍 추적 envelope (`trace_envelope`) |
| `sources` | tuple[str,...] | 출처 라벨 |
| `conversation_id` | str \| None | 유지된 대화 id |
| `file_sources` | list[dict] | 업로드 파일 그라운딩 시 `[{file_name, document_id?}]` (기본 `[]`) |

### `ChatAccepted` (`/chat`, models.py:33-38): `session_id`, `conversation_id`, `sources`
### SSE 이벤트 계약 (`/chat/stream`, app.py:94-113 및 `_sse_events_from_final_answer` app.py:941-952)
순서: `conversation`(0-1) → `step`(0-N, live 질문 처리 시 stage 진행) → `sources`(1) → `file_sources`(0-1) → `delta`(prose) / `markdown_block`(table) → `charts`(0-1) → `timing`(1) → `trace`(1) → `done`(ok). `session_id` replay 시 `step` 미발생.

---

## 3. 오류 응답·한계

| 상황 | 코드/동작 | 근거 |
|---|---|---|
| 빈 질문+파일 없음 | HTTP 400 "질문 또는 파일 업로드가 필요합니다." | app.py:256-257,291-292 |
| 동시성 초과(슬롯 확보 실패) | HTTP 503, `BUSY_MESSAGE`="현재 사용자가 많습니다. 잠시 후 다시 시도해주세요." | app.py:271-272,307-308; concurrency.py:8 |
| 스트림에서 busy | SSE `delta`+`error{type:ServiceBusy}`+`done:error` | app.py:935-938 |
| 알 수 없는 session_id | HTTP 404 "unknown session_id" | app.py:404-405 |
| session_id·question 둘 다 없음 | HTTP 400 | app.py:360-361,417 |
| 공개 호스트 API 키 미설정/불일치/portal id 오류 | 503 / 401 / 400 | app.py:189-200 |
| 프론트 자산 없음 | 404 | app.py:895-903 |
| GenOS 답변 생성 요청 실패(`requests.RequestException`) | **폴백**: `finalized_fallback_fact_answer`로 확정 fact만 결정적 조립 | app.py:1038-1039; answer_safety.py:152-162 |
| 스트림 워커 예외 | SSE `error{type,message}`+`done:error` | app.py:823-825,858-867 |
| 대화 히스토리 DB 기록 실패 | 삼켜서 로깅만(`LOGGER.exception`), 답변 정상 반환 | app.py:977-978 |

### 동시성 한계 (service/concurrency.py)
- `CHAT_MAX_CONCURRENCY` 기본 **3**, `CHAT_QUEUE_WAIT_S` 기본 **10.0초** (concurrency.py:13-14). BoundedSemaphore로 in-flight 답변 캡; 대기 초과 시 busy. (메모리: 운영은 세마포어 3 유지)

### 타임아웃/크기 상수
| 대상 | 값 | 근거 |
|---|---|---|
| 업로드 파일 검색(wf301 bridge) timeout | `JW_CHAT_FILE_SEARCH_TIMEOUT_S` 기본 **3초** | file_search_client.py:29 |
| 외부 MCP 호출 timeout | `ExternalApiClient` 기본 **12초** | tools/external/client.py:71 |
| 웹검색 tavily cap | min(timeout, **5초**) | client.py:38,276 |
| 웹검색 serper/brave | min(timeout, **10초**) | client.py:302,326 |
| 웹검색 최대 결과 | **5건** (`WEB_SEARCH_MAX_RESULTS`) | client.py:37,679-680 |
| 히스토리 DB(MySQL) | connect 3s / read 5s / write 5s | conversation_history.py:158-160 |
| 세션 저장 최대 | `SESSION_STORE_MAX` 기본 **500** (LRU evict) | app.py:128,144-145 |
| 대화 상태 TTL / max_turns | ttl **600초**, max_turns **5**, pending TTL **180초** | conversation.py:38-45 |

### 외부 API 실패 시 부분근거 처리 (tools/external/client.py)
- MCP 실패(`McpClientError`) → `status="error"`, 에러 메시지 relay, **fail-closed**. clinicaltrials는 전용 `external_claim_policy:"fail_closed_error"` (client.py:237-241,461-476).
- MCP 결과 없음 → `status="no_data"`, "MCP 조회 결과 없음" (client.py:447-449,510-530).
- 웹검색: **미검증(unverified)** 분리 정책 일관.
  - 성공: `external_claim_policy:"web_results_unverified"` + `verification_notice`="웹 검색 결과(미검증): URL과 snippet을 출처로 분리 표시하고 내부 fact로 승격하지 않습니다." (client.py:730-731)
  - API 키 없음 → `status="missing_key"`, 검색 미실행 (client.py:683-697)
  - 실패 → `status="error"` + 동일 unverified 정책 (client.py:700-715)
  - 미지원 provider → `status="unsupported"` (client.py:256-263)
- MCP 리소스 기본값(env override): openFDA 184, NeDrug 250, HIRA 253, ClinicalTrials 169 (client.py:40-43).

---

## 4. 파일 질답 실동작

### 두 경로가 존재하지만 실운영은 wf301 bridge 경로
1. **wf301 bridge 경로(실운영)** — `search_uploaded_files` (file_search_client.py:18-68)
   - `conversation_id`를 chat_id/app_session_id로 `POST {JW_CHAT_FILE_SEARCH_BASE}/search`(기본 `http://code-serving-235:8080`, workflow_id 기본 301)에 전달, 반환된 `file_context`/`file_sources`만 소비.
   - **파일 파싱·형식 처리는 이 repo 밖(code-serving-235/preprocessor)에서 수행.** 주석: "The bridge owns file-session isolation ... wiki-first inside 235, VDB fallback there" (file_search_client.py:19-24). 즉 chat-agent는 **파일 포맷을 직접 판별/파싱하지 않음** → 지원 형식 목록은 이 코드에 없음(235/preprocessor 소관). 메모리 노트와 일치: 파일=Weaviate 임베딩 RAG 단일경로, PDF=상위 preprocessor VLM.
   - 실패/타임아웃(3초)·빈 컨텍스트 → `None` 반환(조용히 무시), 답변은 파일 없이 진행 (file_search_client.py:40-44).
   - 활성 토글: `JW_CHAT_FILE_SEARCH_ENABLED` 기본 true (file_search_client.py:25).
2. **로컬 RAG 경로(`document_paths` 직접 첨부 시)** — `LocalDocumentRag` (rag/local_rag.py:21-45)
   - TF-IDF + cosine 유사도로 top_k=2 청크 선택(임베딩/VLM 아님, chunk_chars=480).
   - **정형 통계 파일 거부**: `.csv/.tsv/.xlsx/.xls/.parquet/.json` 확장자면 `ValueError("정형 통계 업로드는 거부됩니다: ...")` (local_rag.py:11,28-29).
   - `doc.read_text(encoding="utf-8")`로 **텍스트만** 읽음 → 바이너리(PDF 등) 직접 지원 안 함(local_rag는 텍스트 전제). PDF 처리는 wf301 경로에서만.

- **★ "28종 매트릭스" 같은 내부 검증 목록은 이 repo(chat-agent)의 코드/docs에 없음** (grep `docs/`·`28종`·`matrix`·`supported format` 무결과, docs 디렉터리 없음). 지원 형식 단정 금지: 런타임 실제 지원 형식은 235/preprocessor 소관이며 여기서는 확인 불가 → **[확인 필요]** (235 측 코드).
- 파일 전용(질문 없이 업로드만) → 즉시 "파일 N개 저장 완료..." 확인 답변, `sources=["file_upload"]` (app.py:476-492).

---

## 5. 멀티턴/문맥

- 요청에 `conversation_id` 필드 존재(models.py:27-30). `ConversationStore`가 대화별 상태 유지(get_or_create, TTL 600s).
- **실제로 프롬프트에 반영되는 유일한 크로스턴 컨텍스트 = pending clarification(market_view)** 뿐:
  - 직전 턴이 "시장 기준 선택" 요청이면 다음 답변에서 view_type 매칭 (app.py:564-590, conversation.py get/set/clear_pending).
- **★ 이전 질문/답변 turns 는 저장만 되고 LLM 프롬프트에 주입되지 않음.**
  - `record_exchange`가 `ConversationTurn`(question/answer/applied_filters)을 append·최근 5개 유지 (conversation.py:77-87)하지만, **`state.turns`를 읽어 프롬프트를 구성하는 코드는 없음** (grep 결과 conversation.py:85 write만 존재, reader 부재). orchestrator/agent_loop의 `applied_filters`는 별개(도구 render_data 필드, tools/metrics/*).
  - 즉 일반적인 멀티턴 대화 메모리(직전 답변 참조)는 **미지원**. 각 질문은 사실상 독립 처리되며, 유지되는 것은 (a) pending 시장기준 선택, (b) wf301 파일 세션(conversation_id로 235가 격리)뿐.
- MySQL `jw_chat_agent_conversation_log`에 턴 로깅(turn_index 증가)은 감사/사이드바용이지 프롬프트 컨텍스트 아님 (conversation_history.py:66-146).

---

## 6. "deep"(심층분석) 특성

- 별도 엔드포인트/모드 스위치 없음. `deep_analysis_related_news`/`search_news` 도구가 질문 라우팅으로 발동(뉴스/이슈 curated 코퍼스, `cache_deep_analysis` 테이블/fixture). 근거: agent_loop/loop.py:347,799,1827; tools/deep_analysis/news.py:50,104; schemas.py:14.
- **웹검색 결과 "미검증" 표기는 코드에 명시** (client.py:730-731 verification_notice). 이것이 유일한 실시간 웹 경로이며 내부 fact 승격 금지 정책.
- 소요시간/비용: 이 repo 코드에는 deep 전용 시간/비용 상수 없음. (메모리 노트 기준 딥리서치 비용·106초 임베딩 등은 별도 측정치, 코드 상수 아님) → 코드 근거로는 **[확인 필요]**.
