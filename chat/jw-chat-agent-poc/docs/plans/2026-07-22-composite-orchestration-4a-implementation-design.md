# 4A 복합 오케스트레이션 구현 상세 설계

- 작성일: 2026-07-22 KST
- 기준 저장소: `jw-market`
- 기준 커밋: `09e0c55512338f3a87e80cc8311905d4dc5f8eb9`
- 범위: 설계만. 구현, 배포, K8s/DB write 없음
- 선행 설계: `COMPOSITE_ORCHESTRATION_DESIGN.md` (요청 기준 SHA `7c0dc49a...`)

## 1. typed composite plan DAG 구조

### 1.1 현재 자산과 경계

현재 BQ 경로는 이미 하나 이상의 도구를 계획한다. `agent_loop/bq_slots.py:29-98`이
14개 계약의 lexical signature를 결정론으로 식별하고, `bq_planner.py:52-94`가 계약의
도구를 평면 `ToolCallPlan` 목록으로 만든다. A3, C2, C3, D2의 도구와 안전 규칙은
`bq_contracts.py:49-54,82-104`에 고정되어 있다. 이 자산은 폐기하지 않고 DAG template의
입력 정본으로 승격한다.

일반 요청은 `service/app.py:566-640`에서 파일 존재와 시장/파일 scope를 판정한 뒤
`app.py:750-790`의 MIXED, FILE, MARKET 분기로 갈린다. MIXED만
`app.py:845-901`에서 두 future를 평면 병렬 실행한다. 의도 목록, 노드 의존성, 결과에서
후행 인자를 만드는 결속, 의도별 terminal 상태를 공통 타입으로 표현하는 계층은 없다.

`09e0c555`의 실제 claim binding은 별도 `service/evidence_binding.py`가 아니라
`tool_use/routing_v4_execution.py:196-242`의 `claim_evidence_bindings()`다. 이 함수는
`not_applicable|fail|pass`를 반환하고 `routing_v4_runtime.py:266-269`가 성공/부분 응답에서
`pass`가 아니면 fail-closed한다. 의뢰서에 적힌 별도 모듈은 이 기준 커밋에 없으므로,
4A는 현재 인터페이스를 adapter로 감싸고 ④ 배포 후 실제 정본 모듈로 교체한다.

### 1.2 타입 계약

구현 위치는 `jw_chat_agent_poc/tool_use/composite_types.py`로 한다. 기존 단일 호출
`RoutePlan`을 확장하지 않고 다음 별도 계약을 둔다.

```python
CompositePlan
  contract_version: "composite_orchestration_v1"
  plan_id: str
  question_fingerprint: str
  decomposition_source: RULE | LLM
  requested_intents: tuple[RequestedIntent, ...]
  nodes: tuple[PlanNode, ...]
  synthesis_contract: SynthesisContract
  budget: CompositeBudget

RequestedIntent
  intent_id: str
  capability_key: str
  required: bool
  expected_output: str

PlanNode
  ordinal: int
  intent_id: str
  eligible_tools: tuple[str, ...]
  selected_tool: str
  normalized_args: JsonObject
  execution_args: JsonObject
  depends_on: tuple[int, ...]
  bindings: tuple[ResultBinding, ...]
  failure_policy: FailurePolicy

ResultBinding
  from_ordinal: int
  evidence_selector: EvidenceSelector
  target_argument: str
  transform: IDENTITY | CANONICAL_BRAND | CANONICAL_CODE | TOP_RANKED_MEMBER
  cardinality: ONE | MANY_UP_TO_5

NodeOutcome
  ordinal: int
  status: SUCCESS | NO_RECORD_FOUND | UPSTREAM_UNAVAILABLE |
          CAPABILITY_NOT_IMPLEMENTED | FIELD_NOT_EXPOSED |
          INVALID_TOOL_ARGUMENTS | SKIPPED_DEPENDENCY | TRUNCATED_RESULT
  reason_code: str | None
  evidence_fact_ids: tuple[str, ...]
```

불변식은 다음과 같다.

1. ordinal은 1부터 연속이며 모든 `depends_on`은 더 작은 ordinal만 가리킨다.
2. DAG는 acyclic, 최대 깊이 3, authority call 최대 6, fan-out 최대 5다.
3. 동일 `(selected_tool, canonical(normalized_args))` 호출은 한 계획에 한 번만 존재한다.
4. 실행 가능한 도구명은 registry가 산출한 `eligible_tools` 안에 있어야 한다. LLM은 도구명을
   만들 수 없다.
5. binding selector는 tool adapter에 등록된 구조화 필드만 읽는다. markdown, preview,
   자유 서술에서 값을 추출하지 않는다.
6. 모든 required intent가 terminal 상태가 되기 전에는 답변을 닫지 않는다.

### 1.3 엣지와 상태 전이

엣지는 단순 실행 순서가 아니라 "선행 evidence에서 후행 실행 인자를 결속한다"는 뜻이다.
binding이 없는 독립 노드는 같은 ready frontier에서 최대 3개까지 실행하고, binding이 있는
노드는 선행 outcome과 binding 검증이 끝난 뒤에만 ready가 된다.

```text
PLANNED -> READY -> RUNNING -> SUCCESS
                         \-> typed terminal failure
typed terminal failure -> dependent node SKIPPED_DEPENDENCY
independent node        -> 계속 실행
```

재계획은 자유 재분해가 아니라 `FailurePolicy`에 선언된 한 단계 전이만 허용한다. 실패한
SQL 단계를 무시하고 후행 도구를 실행하는 전이는 없다.

### 1.4 BQ template와 미매칭 질문

- BQ 매칭: 기존 `BqContract`와 `BqPlan`을 한 방향 adapter로 DAG에 변환한다. BQ 원본과
  composite template를 이중 관리하지 않는다.
- 명시 접속사: "A와 B", "A 다음 B"처럼 capability가 확정되는 패턴은 결정론으로 분해한다.
- 미매칭: LLM은 `RequestedIntent[]`와 dependency hint만 구조화 출력한다. 각 intent의
  capability와 eligible set은 결정론 registry가 다시 계산한다.
- schema 실패: repair 1회 후 `INVALID_TOOL_ARGUMENTS`로 종료한다. legacy 자유 agent loop로
  조용히 우회하지 않는다.

## 2. 구현 순서와 단위별 RED -> GREEN

각 단위는 별도 커밋과 독립 fixture를 가질 수 있다. 뒤 단위는 앞 단위의 ENFORCE 성공을
기다리지 않고 타입/fixture를 대상으로 진행할 수 있으므로, 한 단위 실패가 전체 설계를
폐기하게 만들지 않는다.

| 단위 | 구현 범위 | RED | GREEN | mode 전이 |
|---|---|---|---|---|
| 4A-0 | 타입, canonical plan signature, DAG validator | cycle, 미래 ordinal, 중복 호출, budget 초과가 통과 | 모두 실행 전 typed 오류 | OFF only |
| 4A-1 | BQ 14종 -> DAG adapter | 기존 BQ plan과 node/tool/args가 불일치 | 14/14 canonical parity | OFF, 진단 fixture만 |
| 4A-2 | deterministic 접속사 분해 | 두 의도 중 하나만 계획 | requested intent와 독립 frontier 모두 생성 | SHADOW plan-only |
| 4A-3 | eligible tool resolver | registry 밖 도구명 또는 capability 0건이 CALL | 내부 후보만 선택, 0건은 typed unavailable | SHADOW plan-only |
| 4A-4 | FILE_SQL eligibility | 파일 없는 질문이 FILE_SQL node 생성 | active context + owned sources 둘 다 있어야 생성 | SHADOW plan-only |
| 4A-5 | frontier executor와 ResultBinding | 후행이 선행보다 먼저 실행, 빈 선행을 무시 | topological 실행, 빈 선행은 dependent skip | SHADOW dry executor |
| 4A-6 | 제한 LLM 분해 | 같은 질문 5회 plan signature 변동, 자유 tool 생성 | schema 고정, eligible 내부 선택, 5/5 동일 | SHADOW plan-only |
| 4A-7 | legacy/composite comparator | BQ legacy와 composite 호출 차이를 통과 | call set, args, dependencies, coverage 차이를 기록 | SHADOW observe |
| 4A-8 | deterministic template 우선 ENFORCE | A3/C2/C3/D2가 LLM 경로 사용 | 규칙 template만 새 executor로 실행 | ENFORCE allowlist |

OFF에서는 기존 응답과 외부 호출을 전혀 바꾸지 않는다. SHADOW plan-only는 실제 추가 도구를
호출하지 않고 plan signature와 legacy 비교만 기록한다. SHADOW dry executor는 fixture/fake
adapter에서만 사용한다. ENFORCE는 4A-8의 명시 allowlist부터 시작하며 미매칭 LLM 질문은
4B의 부분 실패 계약이 준비되기 전까지 SHADOW에 남긴다.

구현 파일 예상:

```text
신규  tool_use/composite_types.py
      tool_use/composite_templates.py
      tool_use/composite_planner.py
      tool_use/composite_executor.py
      tool_use/composite_diagnostics.py
수정  agent_loop/bq_planner.py (adapter 호출만)
      tool_use/integration.py (OFF/SHADOW/ENFORCE 배선)
      service/app.py (계획 입력 context 전달)
테스트 tests/test_composite_{types,templates,planner,executor,integration}.py
```

## 3. FILE_SQL eligible 평가 지점

현재 SQL 실행은 `service/file_search_client.py:180-194`에서 search 응답의
`sql_available`과 파싱된 `sql_sources`가 모두 참일 때만 일어난다. bridge 응답 계약은
`wf301-vdb-bridge/src/models.py:433-440`에서 `sql_sources`를 "세션 소유 논리 테이블"로
정의한다. 4A는 이 조건을 계획 compile 전에 다음 typed capability로 승격한다.

```text
FileSqlEligibility
  has_active_file_context: bool
  owned_sql_sources: tuple[FileSqlSource, ...]
  eligible = has_active_file_context and len(owned_sql_sources) > 0
```

평가 순서는 `app.py:566-640`의 active file probe와 scope resolution 후, LLM에 후보 도구를
보이기 전이다. `eligible=false`이면 FILE_SQL은 후보 집합에 들어가지 않으며, LLM이
FILE_SQL을 선택해도 validator가 `CAPABILITY_NOT_IMPLEMENTED`가 아닌
`FILE_CONTEXT_REQUIRED`로 거부한다. 사용자 파일 없이 자동 체이닝으로 SQL에 진입하는
예외는 두지 않는다.

실행 직전에는 소유권을 다시 확인한다. 계획 시점 source ID를 그대로 신뢰하지 않고 현재
conversation ID의 owned source 목록과 exact match해야 한다. 실패하면 SQL 노드는
`SKIPPED_DEPENDENCY`가 아니라 `FILE_SOURCE_OWNERSHIP_CHANGED`로 fail-closed한다.

## 4. claim binding 재사용 인터페이스

4A는 claim verifier를 새로 만들지 않는다. adapter 계약은 다음 하나다.

```text
bind_node_outcome(node, tool_result) -> BindingDisposition
BindingDisposition = answered | partial | unavailable
  answered: required claims 전부가 evidence fact id에 결속
  partial: 일부 claim만 결속, 누락 intent 명시
  unavailable: 결속 가능한 authority evidence 없음
```

`09e0c555`에서는 `claim_evidence_bindings()`의 `pass|fail|not_applicable`을 임시로
`answered|unavailable|unavailable`에 매핑하되 **SHADOW 검출 전용**으로만 사용한다.
`not_applicable`을 answered로 올리지 않는다. ④의 3-way 정본이 배포된 뒤 adapter 내부만
교체하고 `CompositePlan`과 executor는 바꾸지 않는다.

노드 outcome에는 binding이 반환한 evidence ID만 저장한다. ResultBinding과 derived fact는
선행 node의 disposition이 answered일 때만 생성한다. partial은 독립 사실을 답에 남길 수
있지만, 누락 operand가 필요한 산술이나 인과 claim은 만들지 않는다.

## 5. M-01 ~ M-10 실행 판정 기준

아래 질문 문자열은 fixture의 정본 입력이다. 각 gate는 answer 문구가 아니라 canonical plan,
executed trace, binding disposition을 판정한다.

| ID | 입력 질문 | 기대 하위 의도 | 기대 도구/의존 | PASS 기준 |
|---|---|---|---|---|
| M-01 | `상병코드 E11을 확인하고 2024년 환자수를 알려줘` | code resolve, patient count | code resolver -> disease stats | node 2가 node 1 code fact에 결속; 자유 default 0 |
| M-02 | `리바로 시장 상위 1위 브랜드의 진료과 구성을 알려줘` | top rank, specialty | top brands -> specialty | TOP_RANKED_MEMBER binding; rank 1 exact; fan-out 1 |
| M-03 | `리바로 매출 추이와 최근 관련 뉴스 알려줘` | market trend, news | brand series || search news | 두 노드 같은 frontier; required intent 2/2 terminal |
| M-04 | `리바로와 크레스토 매출을 비교해줘` | brand A series, brand B series, compare | series x2 -> deterministic compare | 동일 기간/단위 확인 후만 차이; 브랜드 혼입 0 |
| M-05 | `업로드한 파일의 2026년 1월 매출 합계와 시장 매출을 비교해줘` | file aggregate, market metric | FILE_SQL || market tool | active+owned source 없으면 FILE_SQL 비eligible; 서로 다른 source 합산 0 |
| M-06 | `리바로 2025년과 2026년 점유율을 비교해줘` | period A share, period B share | share x2 -> compare | 동일 tool 다른 normalized args 허용; 같은 frontier; 기간 tag 보존 |
| M-07 | `리바로 허가 정보와 FDA 안전성 근거를 같이 알려줘` | domestic approval, FDA safety | MFDS || openFDA | eligible registry 내부 2개; authority 실패를 웹이 덮지 않음 |
| M-08 | `리바로 시장 규모, 경쟁 구도, 우리 위치를 정리해줘` | size, competitors, position | series || top brands -> position | required intent 3/3 coverage; 경쟁군 evidence 재사용 |
| M-09 | `리바로의 최근 활동과 매출 변화가 같이 움직였는지 알려줘` | activity, sales, alignment | CSD || series -> alignment | D2 template; 동시발생만, 인과 claim 0; source 합산 0 |
| M-10 | `시장 상위 브랜드를 찾고 각 브랜드의 성장률을 비교해줘` | top members, member growth | top brands -> series fan-out | MANY_UP_TO_5, 호출 총합 <=6, 누락 member를 조용히 제거하지 않음 |

기존 BQ 필수 회귀는 별도로 다음처럼 판정한다.

| BQ | 기대 계획 |
|---|---|
| A3 | `get_disease_stats`, `get_brand_sales`, `get_brand_share`; source 합산 금지 |
| C2 | axis에 맞는 breakdown + `get_brand_series`; UBIST only |
| C3 | source별 `get_brand_series`; UBIST/IQVIA 병기, 합산 0 |
| D2 | `csd_activity_trend` + source별 series; temporal overlap를 인과로 승격 0 |

각 gate의 acceptance 출력은 다음 공통 형식을 쓴다.

```text
gate=M-xx classification=census checked=<n> population=<N>
missing=fail tolerance=exact failures=<n> exit_code=<n> environment=local
```

## 6. 비결정성 대응과 층3 반복 검증

결정론 template는 1회 canonical fixture로 충분하지만, LLM이 개입하는 미매칭 질문은 각
질문을 **5회** 반복한다. 이는 기존
`tests/contracts/external_tool_routing_v4/high_risk_repeat_manifest.json`의
`repeat_count=5`와 맞춘 값이다.

동일성 기준은 answer byte가 아니라 다음 canonical plan signature다.

```text
contract_version
decomposition_source
sorted requested_intents(capability, required, expected_output)
nodes in ordinal order:
  intent_id, eligible_tools(sorted), selected_tool,
  canonical normalized_args, depends_on, binding kind/cardinality
synthesis safety rules
budget limits
```

volatile `plan_id`, latency, raw LLM wording은 제외한다. 5회 중 한 번이라도 intent, edge,
selected tool, normalized args, coverage가 달라지면 실패다. 과거 B-05처럼 40% 다른 경로를
타는 결함은 5회 gate에서 최소 한 번 드러날 가능성만 기대하지 않고, fixture provider로
두 상이한 LLM 계획을 주입해 comparator가 반드시 exit 1인지도 별도 검증한다.

3층 검증:

1. 층1: pure type/validator 및 template parity 전수.
2. 층2: fake tool adapter로 DAG 순서, binding, dependent skip, budget 전수.
3. 층3: 고위험 미매칭 질문 5회 반복, legacy/composite signature 비교, 실패 주입.

## 7. 착수 전 확인 목록

4A 구현 시작 직전에 다음을 모두 다시 확인한다.

1. ② brand universe 커밋이 라이브 커밋의 조상이며 resolver가 전체 브랜드를 반환한다.
2. ③ routing/tool input 커밋이 라이브 커밋의 조상이며 capability matrix와 normalized args
   계약이 실제 runtime에 배선되어 있다.
3. live `CHAT_TOOL_ROUTING_MODE`의 spec/effective 값과 planned 4A 기본 mode가 일치한다.
4. ④ claim binding 배포 여부를 확인한다. 미배포면 4A binding은 SHADOW 검출만 허용한다.
5. FILE_SQL은 active file context와 owned `sql_sources`를 둘 다 증명하며, 둘 중 하나라도
   없으면 registry 후보에서 제외된다.
6. BQ 14종 current plan signature를 snapshot하고 A3/C2/C3/D2를 필수 golden으로 둔다.
7. legacy와 composite가 동일 외부 도구를 이중 호출하지 않도록 SHADOW는 plan-only로 시작한다.
8. 전체 repo pytest 기준 실패 수를 기록하고 각 단위에서 기존 실패 유지, 신규 실패 0을
   증명한다.

이 문서는 관측과 목표를 구분한다. `09e0c555`에 있는 것은 현재 관측이며, DAG 타입과
4A-0~4A-8은 구현 목표다. ②, ③, ④의 실제 배포 계보가 확인되지 않으면 ENFORCE로 전환하지
않는다.
