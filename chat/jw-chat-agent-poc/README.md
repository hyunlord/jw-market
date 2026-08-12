# JW Chat Agent

JW중외제약 시장정보 질의를 내부 데이터마트와 외부 공공·웹 근거에 연결해 답변하는 FastAPI 서비스입니다. 현재 v4 경로는 질문과 세션 상태를 해석하고, 7개 소스를 병렬 조회한 뒤, 근거 envelope와 합성·표면 게이트를 거쳐 답변과 추적 정보를 반환합니다. `V4_PLANNER`가 꺼져 있으면 기존 파이프라인을 그대로 사용합니다.

```text
HTTP 입구 / V4 flag
  -> 세션 상태
  -> 플래너
  -> 7종 소스 fan-out
  -> evidence envelope
  -> 합성기
  -> 근거·수치·표면 게이트
  -> shadow / SSE 표면 / 대화 저장
```

## 아키텍처

v4 처리 흐름은 다음 8단계입니다.

1. **입구와 flag**: `/chat`, `/chat/answer`, `/chat/stream`이 요청을 받고 `V4_PLANNER`로 v4 또는 기존 경로를 선택합니다.
2. **세션 상태**: 최근 대화와 저장된 source 결과를 읽어 후속 질문의 대상을 복원합니다.
3. **플래너**: 질문을 정규화하고 확장 의도, 답변 우선 소스, 소스별 쿼리를 만듭니다.
4. **소스 fan-out**: 현재 계약에 정의된 7개 소스 `mart`, `nedrug`, `hira`, `openfda`, `clinicaltrials`, `web`, `patent`를 병렬 실행합니다.
5. **evidence envelope**: entity, 국가 범위, 기간, 지표 단위와 인용 가능 claim을 typed envelope로 정리합니다.
6. **합성**: 내부 데이터마트의 표시값과 외부 근거를 질문 우선순위에 맞춰 답변으로 조립합니다.
7. **게이트**: 숫자 copy-only, 출처 적격성, 내부 식별자, 요청 지표와 렌더 결과를 검사합니다.
8. **shadow·표면·저장**: grounding shadow를 기록하고 SSE 이벤트 또는 JSON 응답으로 내보낸 뒤 대화 상태를 저장합니다.

현재 소스 계약은 7종입니다. 단계 수와 소스 수는 별개이며, 소스를 추가할 때는 `service/v4/contracts.py`의 계약과 adapter, executor, 테스트를 함께 갱신해야 합니다.

상세한 기존 설계와 스트리밍 계약은 [DESIGN.md](DESIGN.md), [PORTAL_COMPLEX_STREAMING_CONTRACT.md](docs/PORTAL_COMPLEX_STREAMING_CONTRACT.md)를 참고하십시오.

## 디렉터리

| 경로 | 역할 |
|---|---|
| `jw_chat_agent_poc/service/app.py` | FastAPI 입구, v4 flag, JSON·SSE 응답 계약 |
| `jw_chat_agent_poc/service/v4/contracts.py` | 플래너·도구 결과·근거 envelope의 strict 타입 계약 |
| `jw_chat_agent_poc/service/v4/planner.py` | 질문 해석, 소스별 쿼리, 연결 계획 |
| `jw_chat_agent_poc/service/v4/executor.py` | 소스 fan-out, deadline, quorum, 실행 추적 |
| `jw_chat_agent_poc/service/v4/adapters.py` | mart·NEDrug·HIRA·OpenFDA·임상·웹·특허 adapter |
| `jw_chat_agent_poc/service/v4/session_state.py` | 대화·도구 결과 기반 세션 상태 |
| `jw_chat_agent_poc/service/v4/synthesizer.py` | 근거 배치, 합성 요청, 결정론 fallback |
| `jw_chat_agent_poc/service/v4/gates.py` | 최종 근거·수치·표면 검사 |
| `jw_chat_agent_poc/service/v4/shadow.py` | canonical fact ledger와 grounding shadow |
| `jw_chat_agent_poc/service/v4/runtime.py` | v4 전체 단계 조율, progress, trace, timing |
| `jw_chat_agent_poc/service/`, `jw_chat_agent_poc/agent_loop/` | flag-off 기존 파이프라인과 공통 서비스 |
| `tests/` | 기존 경로, v4, 회귀·계약 테스트 |
| `eval/`, `scripts/` | 게이트 코퍼스와 검증 도구 |
| `deploy/` | 런타임 식별자와 배포 보조 자산 |

## 로컬 실행

Python 환경과 의존성을 준비한 뒤 저장소 루트에서 실행합니다.

```bash
cd <repository-root>
python -m pip install -r chat/jw-chat-agent-poc/requirements-service.txt
PYTHONPATH=chat/jw-chat-agent-poc \
  uvicorn jw_chat_agent_poc.service.app:app --host 127.0.0.1 --port 8080
```

집중 회귀 예시는 다음과 같습니다.

```bash
cd <repository-root>/chat/jw-chat-agent-poc
python -m compileall -q jw_chat_agent_poc scripts eval
python -m pytest -q tests/test_chat_v4.py tests/test_chat_v4_r9.py tests/test_chat_v4_r10.py
```

## 환경변수

실제 값과 자격증명은 배포 환경에서 주입하며 문서나 이미지에 넣지 않습니다.

| 분류 | 환경변수 | 용도 |
|---|---|---|
| 경로 선택 | `V4_PLANNER` | v4 활성화 여부. 꺼지면 기존 경로 사용 |
| 플래너 | `GENOS_PLANNER_SERVING_ID`, `V4_PLANNER_MODEL`, `V4_PLANNER_TIMEOUT_S`, `V4_PLANNER_BUDGET_S`, `V4_PLANNER_THINKING_LEVEL` | 플래너 serving과 실행 예산 |
| 합성기 | `GENOS_SYNTH_SERVING_ID`, `V4_SYNTHESIZER_SERVING_ID`, `GENOS_SYNTH_BEARER_TOKEN`, `V4_SYNTHESIZER_BEARER_TOKEN`, `V4_SYNTHESIZER_MODEL`, `V4_SYNTHESIZER_TIMEOUT_S`, `V4_SYNTHESIZER_BUDGET_S`, `V4_SYNTHESIZER_THINKING_LEVEL` | 플래너와 분리된 합성기 serving, 인증, 실행 예산 |
| fan-out | `CHAT_V4_MAX_SOURCE_QUERIES` | 소스별 최대 쿼리 수 |
| 세션 저장 | `CHAT_CACHE_DB_HOST`, `CHAT_CACHE_DB_PORT`, `CHAT_CACHE_DB_NAME`, `CHAT_CACHE_DB_USER`, `CHAT_CACHE_DB_PASSWORD` | 대화와 세션 상태 저장소 |
| 웹 검색 | `WEB_SEARCH_PROVIDER` | 웹 검색 provider 선택 |
| 런타임 식별 | `APP_VERSION` | 실행 소스 커밋 식별자 |

`GENOS_SYNTH_BEARER_TOKEN`, `V4_SYNTHESIZER_BEARER_TOKEN`, `CHAT_CACHE_DB_PASSWORD`는 로그, trace, 증적에 출력하지 않습니다. 플래너와 합성기는 endpoint·token scope를 분리해 설정합니다.

## DEV 배포와 롤백

1. 현재 DEV의 generation, `resourceVersion`, `APP_VERSION`, image digest를 롤백 앵커로 기록합니다.
2. 검증된 이미지는 immutable digest로만 지정하고 재빌드나 태그 치환을 하지 않습니다.
3. `resourceVersion` CAS와 server dry-run으로 동시 변경과 허용 범위 밖 diff를 차단합니다.
4. rollout 완료 뒤 `/readyz`뿐 아니라 `/chat/answer` 또는 `/chat/stream`의 실제 답변, source, trace를 확인합니다.
5. 실패 시 `rollout undo`가 아니라 기록한 전체 image digest와 spec 앵커로 되돌립니다.

`deploy/runtime_identity_patch.py`는 `APP_VERSION`과 change-cause를 배포 spec에 결속할 때 사용합니다. 운영 배포 명령이나 내부 클러스터 주소는 이 문서에서 관리하지 않습니다.

## 운영 규약

- **flag-off byte 불변**: v4 변경은 `service/v4/`에 한정하고 기존 경로의 동작과 보호 파일을 바꾸지 않습니다.
- **보호 파일**: `service/evidence_binding.py`, `tool_use/evidence_projection.py`, `service/evidence_binding_rules.py`는 승인된 별도 회차 없이 수정하지 않습니다.
- **모델 스탬프**: 배포 증적은 commit SHA, immutable digest, `APP_VERSION`, planner·synthesizer serving identity를 함께 기록합니다.
- **RED/GREEN**: 결함을 재현하는 실패 증적을 먼저 남기고, 같은 입력과 판정기로 수정 후 통과를 확인합니다.
- **qa_dump**: 라이브 게이트는 질문, 답변, tool arguments, raw payload, trace를 절단 없이 보존하되 secret과 개인정보는 마스킹합니다.
- **판정 경로**: Pod Ready나 출처 블록 존재만으로 답변 성공을 판정하지 않습니다. 질문이 요구한 값이 최종 표면에 결속됐는지 확인합니다.

## 데이터 규칙

- **UBIST와 IQVIA 분리**: UBIST 처방 데이터와 IQVIA 출하 데이터는 분모와 측정 대상이 다르므로 서로 대체하거나 합산하지 않습니다.
- **요청 소스 보존**: 사용자가 지정한 소스가 없으면 다른 소스의 값을 같은 출처인 것처럼 제시하지 않습니다.
- **display verbatim**: mart payload의 표시값, 단위, 반올림은 그대로 사용합니다. 합성기가 환산, 재반올림, 합산하지 않습니다.
- **전략뷰와 일반뷰**: 전략뷰의 시장 정의·분모와 일반뷰의 브랜드·성분 범위를 섞지 않습니다. 답변과 출처에 실제 조회 view와 기간을 표시합니다.
- **수치 copy-only**: 매출, 점유율, 순위, 성장률, HHI 등 정량값은 허용된 payload 값에서만 렌더합니다. 산출이 필요하면 코드가 명시적으로 계산하고 근거·공식을 trace에 남깁니다.
