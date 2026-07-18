# JW Chat Agent — 아키텍처·wf301·GenOS 연동 근거 (read-only)

코드 루트: `/tmp/jwm-develop-docs/chat/` (develop worktree)
- 에이전트: `jw-chat-agent-poc/jw_chat_agent_poc/`
- 파일/VDB 브리지: `wf301-vdb-bridge/src/` (= code-serving-235)

---

## 1. 질의 처리 흐름 (진입 → 답변)

| 단계 | 파일:line | 핵심 함수/역할 |
|---|---|---|
| HTTP 진입 (동기 답변) | `service/app.py:280` `chat_answer()` | POST `/chat/answer` → `_answer_question` → `compute_final_answer` |
| HTTP 진입 (세션 생성) | `service/app.py:248` `chat()` | POST `/chat` → 결과를 서버 메모리 세션에 저장, session_id 반환 |
| HTTP 진입 (SSE 스트림) | `service/app.py:325` `chat_stream()` | GET `/chat/stream` — session_id면 재생, question이면 즉시 처리 |
| 동시성 게이트 | `service/app.py:259,294` `limiter.slot()` | 세마포어 기반 슬롯 (초과 시 503 `ChatBusyError`) |
| 질의 오케스트레이션 | `service/app.py:420` `_answer_question()` | 대화상태 조회 → 파일컨텍스트 위임 → `_answer_with_conversation` |
| 파일컨텍스트 위임 | `service/app.py:455` `_delegated_file_context()` → `service/file_search_client.py:18` `search_uploaded_files()` | code-serving-235 `/search` 호출로 업로드 파일 검색 |
| pending 처리 | `service/app.py:553` `_answer_with_conversation()` | market_view clarification 응답 상태머신 (유일한 멀티턴 분기) |
| 뷰 라우팅 | `service/app.py:593` `_answer_without_pending()` → `service/general_view_routing.py:44` `route()` | `GeneralRoute.EXISTING / GENERAL_ONLY / DUAL` 결정 |
| 에이전트 진입 | `orchestrator/agent.py:80` `ChatAgent.answer()` | BQ/tool 라우팅(`router.route`) → 브랜드 해소 → agent loop 여부 판정 |
| agent loop 게이트 | `orchestrator/agent.py:102` `should_use_agent_loop()` (`agent_loop/routing.py:59`) | 메트릭/외부/약물 토큰 있으면 `loop.answer(question)` |
| 에이전트 루프 | `agent_loop/loop.py:39` `ToolUseAgent.answer()` | 브랜드/기간 grounding → step 루프(planner.decide → tool 실행 → 관찰) |
| tool 플래너(LLM) | `agent_loop/planner.py:42` `GenosToolPlanner._request_decision()` | GenOS serving에 `/chat/completions` (tool_choice=auto) 호출 |
| tool 실행 | `agent_loop/loop.py:100` `_execute_grounded()` (facade=`AgentToolFacade`) | metrics/external/news/query_layer 도구 실행 |
| 결정적 백필 | `agent_loop/loop.py:121,137` `_strict_query_calls`/`_completion_calls`/`_answer_contract_*` | 계약상 필수 fact 강제 채움 |
| fact→markdown | `agent_loop/loop.py:48` `MarkdownResponseBuilder().build()`, `orchestrator/markdown_response.py`, `orchestrator/answer_facts.py`, `orchestrator/provenance.py` | 확정 fact 집합을 markdown으로 조립 |
| 최종 답변(LLM) | `service/genos_client.py:656` `stream_answer()`, `:881` `_chat_text()` → `_stream_chat` | fact md를 근거로 GenOS final serving에 `/chat/completions` 스트리밍 |
| SSE 변환 | `service/app.py:907` `_sse_events()`, `:988` `compute_final_answer()` | FinalAnswer(text/charts/trace/sources) → SSE |
| 대화 기록 | `service/app.py:955` `_record_conversation_history()` + `service/conversation.py:77` `record_exchange()` | 우리 MySQL log + GenOS Mongo 프로젝션 |

핵심: 진입은 `/chat`·`/chat/answer`·`/chat/stream` 3개뿐. 실제 답변 생성 본체는 `_answer_question`(app.py:420) → `ChatAgent.answer`(agent.py:80) → (조건부) `ToolUseAgent.answer`(loop.py:39).

---

## 2. 일반 vs "deep" 라우팅

**★ 소스에 `/deep` 엔드포인트·`deep_research` 모드는 존재하지 않음.** (`grep "/deep|deep_research|deep_mode|is_deep_"` in service/orchestrator/router = 0건)
배포 이미지 태그 `chat-838-p1-deep-finish-*` 의 "deep"은 피처 브랜치명이며, 코드상 "deep"은 아래 두 도구를 의미:

| 구분 | 트리거 근거 (file:line) | 특성 |
|---|---|---|
| 뉴스 심층분석 tool | `agent_loop/planner.py:428` `_asks_news()` = 질문에 `뉴스/이슈/소식/출시/정책/약가` 포함 | `deep_analysis_related_news` — MariaDB 뉴스 코퍼스 + relevance (`tools/deep_analysis/news.py:67` `MariaDbDeepAnalysisNewsReader`) |
| 웹검색 tool | `agent_loop/planner.py:462` `_asks_web_search()`, `agent_loop/routing.py:91`+ `_external_question_needs_agent_loop` = `웹검색/검색해줘/최신 동향/시장동향` 등 | `web_search` — Tavily/Serper/Brave (`tools/external/client.py:248` `_live_web_search`) |
| 이슈+정량 컨텍스트 | `agent_loop/routing.py:83` `_issue_question_needs_quant_context()` = `최근 이슈/관련 이슈/이슈 뭐/이슈 알려` | agent loop 진입 → 메트릭+뉴스 결합 |

일반(비-deep) 질의는 `should_use_agent_loop`가 False면(`agent_loop/routing.py:64`) agent loop를 타지 않고 뷰 라우팅/메트릭 캐시 경로로 처리. deep 도구는 별도 엔드포인트가 아니라 **동일 agent loop 안에서 질문 키워드로 planner가 선택**하는 tool. 즉 멀티스텝·웹검색·뉴스코퍼스는 loop step 예산(`self.max_steps`, loop.py:73) 내에서 수행.

---

## 3. wf301 브리지 (양방향)

두 개의 "브리지" 개념이 구분됨:

**(A) GenOS wf301 워크플로 → chat-agent** (이 repo에 소스 없음, GenOS rev_step 설정)
- wf301 "[market] chat agent" rev_step이 `http://jw-chat-agent-poc:8080/chat/answer` 를 직접 POST (라이브 실측·메모리 근거). 요청 본문은 `ChatRequest`(`service/models.py`): `question`, `document_paths`, `file_context`, `external_mode`, `conversation_id`.

**(B) chat-agent → wf301-vdb-bridge (code-serving-235) 파일검색** (이 repo 소스)
- `service/file_search_client.py:27` base=`JW_CHAT_FILE_SEARCH_BASE` (기본 `http://code-serving-235:8080`), `:28` workflow_id=`301`, `:37` `POST {base}/search`.
- 브리지 `/search` 수신: `wf301-vdb-bridge/src/main.py:1425` `@app.post("/search")` → `search()` (`:1431`). 표준 흐름 `POST /upload → /search → /documents/delete` (`main.py:108` docstring).
- 브리지 역할 = 파일 업로드→임시VDB→공용 Weaviate VDB 139 임베딩 등록+검색 (`settings.py:10` `TARGET_VDB_ID=139`, `:19` `WEAVIATE_BASE`, `:25` `EMBEDDING_BASE`). **chat 답변 생성은 하지 않음** — 파일 컨텍스트만 제공.

즉 wf301 = 파일 첨부 경로에서 235 브리지가 임베딩/검색을 담당, 채팅 답변 자체는 chat-agent가 생성. GenOS wf301 워크플로가 두 서비스를 오케스트레이션.

---

## 4. GenOS / serving / 모델 / MCP 연동

**GenOS LLM serving** (`genos_config.py`) — env 키명만:
| 용도 | resolve 함수 | serving_id env | 기본 serving id |
|---|---|---|---|
| 최종 답변 | `genos_config.py:40` `resolve_final_genos_base_url` | `GENOS_FINAL_SERVING_ID` | `514` (기본, "Flash") |
| tool 플래너 | `:50` `resolve_planner_genos_base_url` | `GENOS_PLANNER_SERVING_ID` | `508` (기본, "Flash") |
| 공통 fallback | `:27` `resolve_genos_base_url` | `GENOS_SERVING_ID` | `517` |
- base URL 기본값 `genos_config.py:19` `https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/{id}`
- 토큰 env: `GENOS_FINAL_BEARER_TOKEN` / `GENOS_PLANNER_BEARER_TOKEN` / `GENOS_BEARER_TOKEN` / `GENOS_TOKEN` (`:12-15`)
- LLM 호출: 둘 다 `POST {base}/chat/completions` (planner `planner.py:54`, final `genos_client._stream_chat`). ★모델명 문자열은 코드에 하드코딩 없음 — serving_id로 GenOS 측 배포 모델(Flash 계열) 결정. "gemini" 등 실코드 리터럴 미발견.

**MCP (외부 의약/통계)** — `tools/external/client.py`:
- 게이트웨이 base `client.py:39` `DEFAULT_MCP_GATEWAY_BASE = http://llmops-gateway-api-service:8080`, env `GENOS_MCP_GATEWAY_BASE` (`:15`)
- URL 패턴 `client.py:246` `{base}/mcp/{resource_id}/mcp`
- resource_id는 env override (라이브 standby 112/190/196/127과 다름 → env로 주입); 코드 **기본값**은:

| MCP | env (resource id) | 코드 기본 resource | mcp_tool 예시 (client.py) |
|---|---|---|---|
| clinicaltrials | `CLINICAL_TRIALS_MCP_RESOURCE_ID` | `169` (`:43`) | `search_studies` (`:349`) |
| openfda | `OPENFDA_MCP_RESOURCE_ID` | `184` (`:40`) | `search_drug_labels` (`:356`) |
| nedrug(MFDS) | `NEDRUG_MCP_RESOURCE_ID` | `250` (`:41`) | `search_drug_permission_list` 등 (`:360-372`) |
| hira | `HIRA_MCP_RESOURCE_ID` | `253` (`:42`) | `get_disease_stats_*` 등 (`:374-390`) |
- MCP 호출 클라이언트 `tools/external/mcp_client.py:27` `McpJsonClient` (JSON-RPC + SSE, `:47` `_post`)

**웹검색** (`client.py:248`): provider env `WEB_SEARCH_PROVIDER`(기본 tavily). 키: `TAVILY_API_KEY`/`SERPER_API_KEY`/`BRAVE_SEARCH_API_KEY` (`:21-23`). 엔드포인트 리터럴: tavily `api.tavily.com/search`(`:273`), serper `google.serper.dev/search`(`:298`), brave(`:316`+).

---

## 5. 멀티턴 문맥 승계 — ★현행 미지원 (제한적 pending만)

근거:
- 에이전트 진입은 `orchestrator/agent.py:80` `answer(self, question: str, ...)` — **질문 문자열 1개만** 인자. `loop.answer(question)`(agent.py:87,104)도 동일. 이전 턴을 프롬프트에 넣지 않음.
- planner 프롬프트 `agent_loop/planner.py:57` `_messages(question, observations, ...)` — `observations`는 **당해 질의의 tool 관찰**일 뿐 과거 대화 아님. `grep "conversation|history|previous_turn|record_exchange" loop.py planner.py` = 0건.
- 대화 상태 저장은 됨: `service/conversation.py:77` `record_exchange()` → `state.turns` (max 5, `:38 max_turns`). 그러나 `.turns` **소비처는 record_exchange 자기 자신뿐** (`grep ".turns"` = conversation.py:85 한 곳). LLM 플래너/최종답변으로 다시 읽히지 않음.
- 유일한 턴간 상태 = `PendingClarification` (`conversation.py:17`): market_view "어떤 뷰?" 되물음 응답만 이어받음 (`app.py:564` `pending.kind == "market_view"`). 일반 후속질문 문맥(예: "그럼 작년은?")은 승계 안 됨.
- `conversation_id`는 동일 세션 식별·chatlog 프로젝션·pending 연결 용도이지, 이전 답변/필터를 다음 질의 컨텍스트로 주입하는 용도가 아님.

결론: **general Q&A 멀티턴 문맥 승계 미지원** — 각 질문 독립 처리. (메모리 노트 chat-259 정합)

---

## 6. 배포

- 앱 엔트리: `jw-chat-agent-poc/Dockerfile.service` → `CMD uvicorn jw_chat_agent_poc.service.app:app --host 0.0.0.0 --port 8080`, `EXPOSE 8080`, base `python:3.11-slim`.
- 브리지: `wf301-vdb-bridge/Dockerfile` (별도 이미지, code-serving-235).
- **repo 내 k8s 매니페스트(Deployment/HPA yaml) 없음** — GenOS 플랫폼(code-serving/serving) 관리. `deploy/` 디렉터리엔 `history_projection.sql`만.
- 라이브 실측(인용): deployment `jw-chat-agent-poc` replicas 3, image `chat-838-p1-deep-finish-da3fc15-20260718`@sha256:efec7f94…, HPA min2/max4 mem60%, svc 8080→8080.
- 주요 env 키명(값 미수집): `GENOS_FINAL_SERVING_ID`/`GENOS_PLANNER_SERVING_ID`/`GENOS_SERVING_ID`, `GENOS_*_BEARER_TOKEN`, `GENOS_MCP_GATEWAY_BASE`, `*_MCP_RESOURCE_ID`(4종), `WEB_SEARCH_PROVIDER`+키3종, `JW_CHAT_FILE_SEARCH_BASE`/`JW_CHAT_FILE_WORKFLOW_ID`/`JW_CHAT_FILE_SEARCH_ENABLED`, `GENOS_AGENT_TIMEOUT_S`, `CHAT_DEEP_NEWS_*`.
- [확인 필요] 빌드/배포 절차 문서: README에 deploy/serving 섹션 없음. GenOS UI 배포 경로로 추정(메모리 serving-routing 근거).
