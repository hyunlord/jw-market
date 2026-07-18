# DOC-1c 개발 문서 — 채팅 에이전트

| 항목 | 값 |
|---|---|
| 기준 코드(develop) SHA | `9c34a7d5` (README·스켈레톤 기준; 작성 시 worktree HEAD `1864e929`, chat 코드 정본 커밋 `0900ed5e` "Isolate jw-chat under a chat top-level folder" → 경로 `chat/jw-chat-agent-poc/`) |
| 운영 리소스 | deployment `jw-chat-agent-poc` (ns `llmops`, 3 replicas, HPA `jw-chat-agent-poc-hpa` min2/max4 mem60%), svc `jw-chat-agent-poc` `8080→8080`; 파일 브리지 = `code-serving-235`(wf301-vdb-bridge) |
| 운영 이미지 | `jw-chat-agent-poc:chat-838-p1-deep-finish-da3fc15-20260718@sha256:efec7f94…` |
| 생성일 | 2026-07-18 |
| 문서 버전 | v1.0 |
| 근거 | `chat/jw-chat-agent-poc/`·`chat/wf301-vdb-bridge/` 실코드 `파일:줄`, 라이브 실측(`evidence/chat_identity_consolidated.md`) |

> **경계.** 본 문서는 **채팅 에이전트 내부**를 다룬다. HTTP EP 상세·오류 계약은 [DOC-3b](DOC-3b_API_채팅.md), 유저 사용법은 [DOC-4d](DOC-4d_사용설명서_채팅.md), 시장분석 백엔드는 DOC-1/DOC-3을 참조한다. 모델명/토큰 등 자격증명 값은 기재하지 않고 env 키명·serving id만 쓴다.

---

## 1. 채팅 에이전트 아키텍처 (질의 처리 흐름)

진입 EP는 `/chat`·`/chat/answer`·`/chat/stream` 3개뿐이며, 답변 생성 본체는 `_answer_question`(app.py:420) → `ChatAgent.answer`(agent.py:80) → (조건부) `ToolUseAgent.answer`(loop.py:39)다.

```
POST /chat/answer  (wf301 경유)
  → limiter.slot()  [동시성 세마포어, 초과 시 503]        app.py:294 / concurrency.py
  → _answer_question                                      app.py:420
      → _delegated_file_context → 235 /search (파일 첨부 시)  app.py:455 / file_search_client.py:18
      → _answer_with_conversation → pending(market_view) 분기   app.py:553
      → 뷰 라우팅 route(EXISTING/GENERAL_ONLY/DUAL)         general_view_routing.py:44
  → ChatAgent.answer  (BQ/tool 라우팅, 브랜드 해소)          agent.py:80
      → should_use_agent_loop ?                            agent.py:102 / agent_loop/routing.py:59
          예 → ToolUseAgent.answer  [step 루프]            loop.py:39
                planner.decide → GenOS serving /chat/completions(tool_choice=auto)   planner.py:42
                tool 실행(metrics·external·news·query_layer)   loop.py:100 (AgentToolFacade)
                결정적 백필(계약상 필수 fact 강제)             loop.py:121,137
          아니오 → 뷰/메트릭 캐시 경로
  → fact → markdown 조립                                   markdown_response.py·answer_facts.py·provenance.py
  → 최종 답변 LLM 스트리밍 (GenOS final serving)           genos_client.py:656 stream_answer
  → compute_final_answer → (SSE는) _sse_events            app.py:988 / :907
  → 대화 기록(우리 MySQL log + GenOS Mongo 프로젝션)       app.py:955 / conversation.py:77
```

### 1.1 일반 질의 vs "deep"(심층분석) 경로 구분

- **`/deep` 엔드포인트·`deep_research` 모드는 존재하지 않는다.** 이미지 태그 `…-deep-finish-…`의 "deep"은 피처 브랜치명이다. 코드상 "deep"은 **동일 agent loop 안에서 planner가 질문 키워드로 선택하는 도구**를 뜻한다.

| 구분 | 트리거(file:line) | 도구·특성 |
|---|---|---|
| 뉴스 심층분석 | `planner.py:428` `_asks_news()` = 질문에 `뉴스/이슈/소식/출시/정책/약가` | `deep_analysis_related_news` — MariaDB 뉴스 코퍼스 + relevance(`tools/deep_analysis/news.py:67`) |
| 웹검색 | `planner.py:462` `_asks_web_search()`, `agent_loop/routing.py:91` = `웹검색/검색해줘/최신 동향/시장동향` | `web_search` — Tavily/Serper/Brave(`tools/external/client.py:248`), 결과는 **미검증** 분리 |
| 이슈+정량 | `agent_loop/routing.py:83` `_issue_question_needs_quant_context()` | agent loop 진입 → 메트릭+뉴스 결합 |

일반 질의는 `should_use_agent_loop`가 False면(routing.py:64) loop를 타지 않고 뷰 라우팅/메트릭 캐시로 처리된다. deep 도구는 별도 경로가 아니라 loop step 예산(`self.max_steps`, loop.py:73) 안에서 수행된다.

## 2. wf301 브리지 · 포탈 연동 경로

"브리지"는 두 개념으로 구분된다.

- **(A) GenOS wf301 워크플로 → 채팅** (이 repo에 소스 없음, GenOS rev_step 설정): wf301 "[market] chat agent" rev_step이 in-mesh로 `http://jw-chat-agent-poc:8080/chat/answer`를 POST 한다(라이브 실측). 본문은 `ChatRequest`(`service/models.py`). in-mesh 호출이라 API 키 불요(DOC-3b §2.2).
- **(B) 채팅 → wf301-vdb-bridge(`code-serving-235`) 파일검색** (이 repo 소스 `chat/wf301-vdb-bridge/src/`): `file_search_client.py:27` base `JW_CHAT_FILE_SEARCH_BASE`(기본 `http://code-serving-235:8080`), workflow_id `301`, `POST {base}/search`. 브리지 `main.py:1425` `@app.post("/search")`. 표준 흐름 `POST /upload → /search → /documents/delete`(`main.py:108`). 브리지는 업로드 파일을 임시 VDB→공용 Weaviate VDB(`settings.py:10` `TARGET_VDB_ID=139`)에 임베딩 등록/검색만 하고 **답변은 생성하지 않는다**.

즉 GenOS wf301 워크플로가 두 서비스를 오케스트레이션하고, 파일 임베딩/검색은 235가, 답변 생성은 채팅 에이전트가 담당한다. (호출 계약 상세: DOC-3b §2.)

## 3. GenOS · serving · 모델 · MCP 연동

**GenOS LLM serving** (`jw_chat_agent_poc/genos_config.py` — 패키지 루트, env 키명만):

| 용도 | resolve 함수 | serving id env | 기본 serving id |
|---|---|---|---|
| 최종 답변 | `genos_config.py:40` | `GENOS_FINAL_SERVING_ID` | `514`(Flash) |
| tool 플래너 | `:50` | `GENOS_PLANNER_SERVING_ID` | `508`(Flash) |
| 공통 fallback | `:27` | `GENOS_SERVING_ID` | `517` |

- base URL 기본 `genos_config.py:19` `https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/{id}`. LLM 호출은 planner·final 모두 `POST {base}/chat/completions`(planner.py:54, `genos_client._stream_chat`).
- **모델명 문자열은 코드에 하드코딩되지 않는다** — serving id로 GenOS 측 배포 모델(Flash 계열)이 결정된다. 코드에 `gemini` 등 모델 리터럴은 없다. 실제 모델은 GenOS serving 배포 설정에 달려 있다(→ 특정 모델 확정은 `[확인 필요]`, GenOS serving 소관). 토큰 env: `GENOS_*_BEARER_TOKEN`(값 미기재).
- **Flowise**: 채팅 코드에서 Flowise 직접 참조는 발견되지 않았다(`[확인 필요]` — 플랫폼 `llmops-flowise-300`은 GenOS 측 구성 요소로 채팅 repo와 직접 연동 근거 없음).

**MCP(외부 의약/통계)** (`tools/external/client.py`): 게이트웨이 base `client.py:39` `http://llmops-gateway-api-service:8080`(env `GENOS_MCP_GATEWAY_BASE`), URL `{base}/mcp/{resource_id}/mcp`(`:246`).

| MCP | env(resource id) | 코드 기본 | 라이브 standby |
|---|---|---|---|
| clinicaltrials | `CLINICAL_TRIALS_MCP_RESOURCE_ID` | 169 | `code-serving-112` |
| openfda | `OPENFDA_MCP_RESOURCE_ID` | 184 | `code-serving-127` |
| nedrug(MFDS) | `NEDRUG_MCP_RESOURCE_ID` | 250 | `code-serving-196` |
| hira | `HIRA_MCP_RESOURCE_ID` | 253 | `code-serving-190` |

(resource id는 env override라 코드 기본값과 라이브 standby 번호가 다르다. MCP 클라이언트 `tools/external/mcp_client.py:27`, JSON-RPC+SSE.) 웹검색 provider는 env `WEB_SEARCH_PROVIDER`(기본 tavily), 키 `TAVILY_API_KEY`/`SERPER_API_KEY`/`BRAVE_SEARCH_API_KEY`.

## 4. 파일 질답 경로

- 포탈 업로드 파일에 대한 질문은 §2-(B) 경로로 처리된다: 채팅이 `conversation_id`를 chat_id/app_session_id로 235 `/search`에 전달(`file_search_client.py:18`), 반환된 `file_context`/`file_sources`만 소비한다. **채팅은 파일 포맷을 직접 파싱하지 않는다**(임베딩/검색은 235·전처리기 소관, VDB 139). 검색 타임아웃 기본 3초, 준비 전/실패 시 `None` → 파일 없이 답변 진행(`file_search_client.py:40-44`).
- 별도로 `document_paths`로 **텍스트 파일을 직접 첨부**하는 로컬 RAG 경로(`rag/local_rag.py`, TF-IDF top_k=2)가 있으나 **정형 통계 파일(`.csv/.tsv/.xlsx/.xls/.parquet/.json`)은 거부**되고(`local_rag.py:28`) 텍스트만 읽는다. 실운영 파일 질답은 235 브리지 경로다. (지원 형식 전수는 235 소관 → `[확인 필요]`.)

## 5. 배포 형태 · 이미지

- 앱 엔트리: `chat/jw-chat-agent-poc/Dockerfile.service` → `uvicorn jw_chat_agent_poc.service.app:app --host 0.0.0.0 --port 8080`(base `python:3.11-slim`). 브리지는 별도 이미지(`chat/wf301-vdb-bridge/Dockerfile`, code-serving-235).
- **repo 내 k8s Deployment/HPA 매니페스트는 없다** — GenOS 플랫폼(code-serving/serving)이 배포를 관리한다(`deploy/`에는 `history_projection.sql`만). 빌드/배포 절차 문서는 repo에 없음 → `[확인 필요]`(GenOS UI 배포 경로 추정).
- 라이브 실측: `jw-chat-agent-poc` 3 replicas(2/2 컨테이너), HPA min2/max4 mem60%(현재 49%), 이미지 `…@sha256:efec7f94…`. 동시성 세마포어 기본 3(concurrency.py:13).

---

## [확인 필요] 목록
1. GenOS serving id(514/508/517)에 배포된 **실제 모델**(Flash 계열의 구체 모델명) — GenOS serving 배포 설정 소관, 채팅 코드엔 리터럴 없음.
2. **Flowise 연동** 실재 여부 — 채팅 repo에 직접 참조 없음.
3. 채팅 서비스 **빌드/배포 절차** 문서 — repo에 없음(GenOS UI 경로 추정).
4. 파일 질답 **지원 형식 전수** — 전처리기/235 소관(DOC-4d와 동일).

## 스크린샷/다이어그램 캡처 리스트
- `[그림: 질의 처리 흐름도 (진입→loop→GenOS→SSE)]`
- `[그림: wf301 (A)워크플로/(B)파일브리지 2경로]`
