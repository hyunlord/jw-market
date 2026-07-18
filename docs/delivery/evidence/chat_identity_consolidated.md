# jw chat — 라이브 identity & EP 실측 (2026-07-18)

## 기준
- 문서 기준 develop SHA: `9c34a7d5`(README·스켈레톤 기준), 작성 시 worktree HEAD `1864e929`(9c34a7d5 조상 포함)
- chat 코드 최근 커밋: `0900ed5e` "Isolate jw-chat under a chat top-level folder" → 코드 경로 `chat/jw-chat-agent-poc/`
- 운영 이미지: `jw-chat-agent-poc:chat-838-p1-deep-finish-da3fc15-20260718@sha256:efec7f94881ead9a8290e9e01cefccb997fc634246aa571f13e0cbc55e8c2e77`

## 라이브 리소스 (llmops ns)
- deployment `jw-chat-agent-poc`: 3 replicas(2/2 컨테이너 Running×3 pod: 5zx44·k9snl·p9xjd), HPA `jw-chat-agent-poc-hpa` min2/max4 mem60%(현재 49%/60%), svc `jw-chat-agent-poc` 8080→8080
- wf301 브리지 = `code-serving-235` (image `wf301-vdb-bridge@sha256:c5b371803ca48b5fbe04f82760e237a87acf0ff5ab6fdf74ccb6a61e7d1cc546`), 배포 그룹 226/229/232/235 중 235가 api(라이브)
- MCP standby(외부 API 프록시): clinicaltrials=`code-serving-112`, hira=`code-serving-190`, nedrug=`code-serving-196`, openfda=`code-serving-127` (전부 1/1)

## EP 실측 (service/app.py, develop 1864e929)
| EP | 메서드 | 핸들러(app.py:줄) | 응답모델 | 인증 |
|---|---|---|---|---|
| `/healthz` | GET | app.py:240 | HealthResponse | 없음 |
| `/__version` | GET | app.py:244 | — | 없음 |
| `/chat` | POST | app.py:248 (chat) | ChatAccepted | `_require_direct_route_api_key` |
| `/chat/answer` | POST | app.py:280 (chat_answer) | ChatAnswer | `_require_direct_route_api_key`(ProjectionRequestContext) |
| `/chat/stream` | GET | app.py:325 (chat_stream) | SSE | — |
| `/`·/index.html·/{frontend_path} | GET | app.py:378-383 | 정적 프론트 | 없음 |

- `/chat`·`/chat/answer` 둘 다 direct-route API 키 의존. 포탈 경로(wf301)는 이 키를 실어 `/chat/answer` 호출(브리지 상세는 chat_arch_evidence.md).
- 질문 공백 + 파일 신호 없음 → 400 "질문 또는 파일 업로드가 필요합니다"(app.py:256, 292).
- 동시성: `limiter.slot()` 세마포어(app.py:259,296) — 메모리 보호(세마포어 3 계약, MEMORY 참조).
