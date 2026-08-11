from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from jw_chat_agent_poc.service.v4.contracts import (
    Citation,
    EvidenceEnvelope,
    PlannerOutput,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.runtime import V4Runtime
from jw_chat_agent_poc.service.v4.shadow import build_canonical_ledger, build_grounding_shadow
from jw_chat_agent_poc.service.v4.synthesizer import _synthesis_messages


def _plan(question: str) -> PlannerOutput:
    queries = ToolQueries(**{source: (question,) for source in (
        "mart", "nedrug", "hira", "openfda", "clinicaltrials", "web", "patent"
    )})
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        answer_sources=("mart",),
        tool_queries=queries,
        linking_plan="single wave",
    )


def _mart(payload: dict[str, object]) -> SourceResult:
    return SourceResult(
        source="mart",
        query="리바로 매출이 왜 올랐어?",
        status="ok",
        payload={"calls": payload.get("calls", [payload])},
        evidence=EvidenceEnvelope(
            kind="mart",
            entity_match="EXACT",
            source_scope="KR",
            time_match="MATCH",
            subject_grain="brand",
            eligible_attributions=("observed_association",),
        ),
        citations=(Citation(
            source="UBIST",
            query="리바로 매출이 왜 올랐어?",
            retrieved_at=datetime.now(UTC),
        ),),
    )


def test_r10_numeric_copy_repairs_only_display_token_and_keeps_cause_shape() -> None:
    result = _mart({
        "tool": "cause_card_data",
        "summary_text": "리바로 원인 분해",
        "render_data": {
            "sales_delta_억원": 13.7349,
            "market_growth_pct": 4.321,
            "period_start": "2025-09",
            "period_end": "2026-06",
        },
    })
    answer = (
        "## 핵심 답\n리바로 매출은 13.7349억원 증가했습니다.\n\n"
        "## 근거와 맥락\n관측: 시장 성장률은 4.321%였습니다.\n\n"
        "## 종합 인사이트\n제품 믹스 이동과 일치하는 모습입니다.\n\n"
        "## 미확인 요소\n인과관계는 확인되지 않았습니다."
    )

    gated = apply_v4_gates("리바로 매출이 왜 올랐어?", answer, (result,))

    assert "## 핵심 답" in gated.text
    assert "## 근거와 맥락" in gated.text
    assert "## 종합 인사이트" in gated.text
    assert "13.73억원" in gated.text
    assert "4.32%" in gated.text
    assert "13.7349" not in gated.text
    assert gated.trace["mart_numeric_copy_only"]["full_fallback"] is False


def test_r10_numeric_copy_accepts_payload_value_already_rounded_for_display() -> None:
    result = _mart({"brand": "리바로", "sales_delta_억원": 13.7349})

    gated = apply_v4_gates(
        "리바로 매출이 왜 올랐어?",
        "리바로 매출은 13.73억원 증가했습니다.",
        (result,),
    )

    assert "13.73억원" in gated.text
    assert "확인된 수치" not in gated.text
    assert gated.trace["mart_numeric_copy_only"]["blocked"] is False


def test_r10_numeric_copy_redacts_only_invented_value_without_replacing_answer() -> None:
    result = _mart({"brand": "리바로", "sales_eok": 85.87})
    answer = (
        "## 핵심 답\n리바로 매출은 99.99억원입니다.\n\n"
        "## 근거와 맥락\n확인된 내부 지표를 기준으로 봤습니다.\n\n"
        "## 종합 인사이트\n제품 믹스 변화 가능성을 함께 살펴야 합니다.\n\n"
        "## 미확인 요소\n인과관계는 확인되지 않았습니다."
    )

    gated = apply_v4_gates("리바로 매출 알려줘", answer, (result,))

    assert "99.99" not in gated.text
    assert "85.87" in gated.text
    assert "## 근거와 맥락" in gated.text
    assert "제품 믹스 변화 가능성" in gated.text
    assert gated.trace["mart_numeric_copy_only"]["blocked"] is True
    assert gated.trace["mart_numeric_copy_only"]["full_fallback"] is False


def test_r10_prose_normalizes_full_precision_rx() -> None:
    result = _mart({
        "tool": "get_dimension_breakdown",
        "summary_text": "진료과 처방량 6730094.74 Rx",
        "render_data": {"prescription_volume": 6_730_094.74},
    })

    gated = apply_v4_gates(
        "리바로 진료과별 처방량 알려줘",
        "## 핵심 답\n순환기내과 처방량은 6730094.74 Rx입니다.",
        (result,),
    )

    assert "약 673만 Rx" in gated.text
    assert "6730094.74" not in gated.text


def test_r10_synthesis_prompt_exposes_entity_bundle_comparison_contract() -> None:
    result = _mart({
        "entity_bundle": {
            "anchor": "리바로",
            "period_start": "2025-09",
            "period_end": "2026-06",
            "members": [
                {"brand": "리바로", "role": "target"},
                {"brand": "리바로젯", "role": "family"},
                {"brand": "크레스토", "role": "competitor"},
            ],
        }
    })

    prompt = json.loads(_synthesis_messages(_plan("리바로 요즘 어때"), (result,), ())[-1]["content"])

    contract = prompt["entity_bundle_contract"]
    assert contract["same_period_and_denominator_only"] is True
    assert contract["adverse_signal_must_be_explicit"] is True
    assert "entity_bundle" in prompt["internal_datamart"][0]
    assert tuple(prompt)[-1] == "session_state"


def test_r10_absence_context_separates_official_absence_from_web_reporting() -> None:
    from jw_chat_agent_poc.service.v4.runtime import _absence_context_request, _tag_absence_context

    official = SourceResult(
        source="hira",
        query="마운자로 급여기준",
        status="empty",
        notice="no_data",
    )
    plan = _plan("마운자로 급여기준").model_copy(update={"answer_sources": ("hira",)})
    request = _absence_context_request(plan, (official,))
    assert request == {
        "source": "hira",
        "document": "reimbursement",
        "query": "마운자로 급여기준",
    }

    web = SourceResult(
        source="web",
        query="마운자로 급여기준 부재 경과",
        status="ok",
        payload={"items": [{"url": "https://www.yna.co.kr/view/example", "title": "약가 협상 결렬"}]},
    )
    tagged = _tag_absence_context(web, request)
    item = tagged.payload["items"][0]
    assert item["trust_tier"] == "TIER2"
    assert tagged.payload["absence_context"]["official_absence"] is True
    assert tagged.payload["absence_context"]["reported_context_only"] is True

    synth_prompt = json.loads(
        _synthesis_messages(plan, (official, tagged), ())[-1]["content"]
    )
    assert synth_prompt["absence_context_contract"]["official_absence_is_confirmed"] is True
    assert synth_prompt["absence_context_contract"]["web_context_uses_reported_language"] is True
    assert tuple(synth_prompt)[-1] == "session_state"

    untrusted = SourceResult(
        source="web",
        query="마운자로 급여기준 블로그",
        status="ok",
        payload={"items": [{"url": "https://example.com/post", "title": "비공식 글"}]},
    )
    assert _tag_absence_context(untrusted, request).status == "empty"


@pytest.mark.parametrize("status", ["ok", "error", "timeout"])
def test_r10_absence_context_requires_confirmed_empty_official_result(status: str) -> None:
    from jw_chat_agent_poc.service.v4.runtime import _absence_context_request

    plan = _plan("리바로 급여기준").model_copy(update={"answer_sources": ("hira",)})
    result = SourceResult(source="hira", query="리바로 급여기준", status=status)

    assert _absence_context_request(plan, (result,)) is None


def test_r10_shadow_treats_year_as_period_not_ungrounded_number() -> None:
    result = _mart({"period": "2026-06", "sales_억원": 85.87})

    shadow = build_grounding_shadow("2026년 매출은 85.87억원입니다.", (result,))

    assert shadow["counts"]["period"] == 1
    assert shadow["counts"]["ungrounded"] == 0


def test_r10_shadow_treats_month_quarter_and_range_tokens_as_periods() -> None:
    result = _mart({"period_start": "2023-01", "period_end": "2026-05"})

    shadow = build_grounding_shadow(
        "2023~2026년 중 2026년 1분기와 5월을 비교했습니다.",
        (result,),
    )

    assert shadow["counts"]["period"] == 5
    assert shadow["counts"]["ungrounded"] == 0
    assert shadow["ledger"]["by_kind"]["period"] >= 2


def test_r10_shadow_reports_actual_ledger_truncation() -> None:
    payload = {f"metric_{index}": index + 0.125 for index in range(1_205)}
    result = _mart(payload)
    ledger = build_canonical_ledger((result,))

    shadow = build_grounding_shadow("값은 1.125입니다.", (result,), ledger=ledger)

    assert shadow["ledger"]["fact_count"] > 1_200
    assert shadow["ledger"]["truncated"] is False


def test_r10_shadow_keeps_duplicate_numeric_paths_for_grain_matching() -> None:
    market = _mart({"market_size_억원": 85.87}).model_copy(
        update={"evidence": _mart({}).evidence.model_copy(update={"subject_grain": "market"})}
    )
    brand = _mart({"brand": "리바로", "sales_억원": 85.87})

    shadow = build_grounding_shadow("리바로 매출은 85.87억원입니다.", (market, brand))

    assert shadow["counts"]["grounded"] == 1
    assert shadow["counts"]["grain_mismatch"] == 0


def test_r10_session_state_store_is_seven_day_lazy_and_fail_open() -> None:
    from jw_chat_agent_poc.service.v4.session_state import SessionState, SessionStateStore

    class BrokenStore(SessionStateStore):
        def _connect(self):
            raise OSError("db unavailable")

    store = BrokenStore(config=object())
    assert store.load("conversation-1") is None
    store.save("conversation-1", SessionState(canonical_entities=("리바로",)))


def test_r10_session_state_store_uses_approved_schema_and_upsert_contract() -> None:
    from jw_chat_agent_poc.service.v4.session_state import (
        SCHEMA_SQL,
        SessionState,
        SessionStateStore,
    )

    assert SCHEMA_SQL == """CREATE TABLE agent_session_state (
  session_id VARCHAR(64) PRIMARY KEY,
  state_json JSON NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_updated (updated_at)
)"""

    class Cursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params):
            self.calls.append((" ".join(sql.split()), params))

        def fetchone(self):
            return (json.dumps({"canonical_entities": ["리바로"]}),)

    class Connection:
        def __init__(self) -> None:
            self.cursor_value = Cursor()
            self.commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return self.cursor_value

        def commit(self):
            self.commits += 1

    connection = Connection()

    class Store(SessionStateStore):
        def _connect(self):
            return connection

    store = Store(config=object())
    loaded = store.load("conversation-1")
    store.save("conversation-1", SessionState(canonical_entities=("리바로",)))

    assert loaded is not None and loaded.canonical_entities == ("리바로",)
    load_sql, load_params = connection.cursor_value.calls[0]
    save_sql, save_params = connection.cursor_value.calls[1]
    assert "updated_at >= UTC_TIMESTAMP() - INTERVAL 7 DAY" in load_sql
    assert load_params == ("conversation-1",)
    assert "ON DUPLICATE KEY UPDATE state_json = VALUES(state_json)" in save_sql
    assert save_params[0] == "conversation-1"
    assert connection.commits == 1


def test_r10_session_state_prompt_injection_is_structured() -> None:
    from jw_chat_agent_poc.service.v4.planner import _planner_messages
    from jw_chat_agent_poc.service.v4.session_state import SessionState

    state = SessionState(
        canonical_entities=("리바로", "리바로젯"),
        referenced_entity_set=("리바로", "리바로젯"),
        requested_grain="brand",
        comparison_anchor="리바로",
    )
    planner_payload = json.loads(_planner_messages("그 중에 매출 제일 큰 게", (), state=state)[-1]["content"])
    synth_payload = json.loads(
        _synthesis_messages(_plan("그 중에 매출 제일 큰 게"), (_mart({"sales_억원": 85.87}),), (), state=state)[-1]["content"]
    )

    assert planner_payload["session_state"]["referenced_entity_set"] == ["리바로", "리바로젯"]
    assert synth_payload["session_state"]["comparison_anchor"] == "리바로"
    assert tuple(synth_payload)[-1] == "session_state"


def test_r10_strategic_mart_builds_bounded_entity_bundle_from_member_series() -> None:
    from jw_chat_agent_poc.service.v4.adapters import _strategic_mart_calls

    class Layer:
        def __init__(self) -> None:
            self.members: list[str] = []
            self.top_limit: int | None = None

        def market_scope(self, brand):
            return {"source": "UBIST", "render_data": {"market_id": "ml_006", "anchor_brand": brand}}

        def brand_metric(self, brand, metric, period, market=None, history_points=10):
            return {"source": "UBIST", "tool": "get_brand_metric", "render_data": {"brand": brand, "metric": metric}}

        def top_brands(self, brand, limit=5, market=None, metric="sales"):
            self.top_limit = limit
            return {
                "source": "UBIST",
                "tool": "get_top_brands",
                "render_data": {
                    "period": "2026-06",
                    "level_top5_trend_series": [
                        {
                            "brand": "리바로",
                            "company": "JW",
                            "rank": 2,
                            "from_ms_pct": 9.5,
                            "to_ms_pct": 8.8,
                            "share_delta_pctp": -0.7,
                        },
                        {"brand": "리바로젯", "company": "JW", "rank": 3},
                        {"brand": "크레스토", "company": "AZ", "rank": 1},
                        {"brand": "아토젯", "company": "MSD", "rank": 4},
                        {"brand": "로수젯", "company": "HK", "rank": 5},
                        {"brand": "바이토린", "company": "MSD", "rank": 6},
                        {"brand": "리피토", "company": "PF", "rank": 7},
                        {"brand": "심바스트", "company": "MSD", "rank": 8},
                    ],
                },
            }

        def market_member_metric(self, anchor_brand, member_brand, market=None, metric="series"):
            self.members.append(member_brand)
            return {
                "source": "UBIST",
                "tool": "get_brand_metric",
                "render_data": {
                    "brand": member_brand,
                    "market_id": market,
                    "brand_value_series_10pt": [
                        {"period": "2025-09", "value_억원": 10.0},
                        {"period": "2026-06", "value_억원": 12.0},
                    ],
                },
            }

        def cause_card_data(self, brand, market):
            return {}

    layer = Layer()
    calls = _strategic_mart_calls(layer, "리바로", "리바로 요즘 어때")
    bundle = next(call["entity_bundle"] for call in calls if "entity_bundle" in call)

    assert layer.top_limit == 8
    assert set(layer.members) == {
        "리바로", "리바로젯", "크레스토", "아토젯", "로수젯", "바이토린", "리피토"
    }
    assert bundle["period_start"] == "2025-09"
    assert bundle["period_end"] == "2026-06"
    assert [member["role"] for member in bundle["members"][:2]] == ["target", "family"]
    assert len(bundle["members"]) == 7
    assert sum(member["role"] == "competitor" for member in bundle["members"]) == 5
    assert bundle["members"][0]["share_delta_pctp"] == -0.7


def test_r10_adverse_share_signal_is_appended_when_model_omits_it() -> None:
    from jw_chat_agent_poc.service.v4.synthesizer import _append_required_adverse_signal

    result = _mart(
        {
            "entity_bundle": {
                "anchor": "아일리아",
                "same_period_and_denominator": True,
                "members": [
                    {
                        "brand": "아일리아",
                        "role": "target",
                        "from_ms_pct": 42.1,
                        "to_ms_pct": 39.8,
                        "share_delta_pctp": -2.3,
                    }
                ],
            }
        }
    )

    answer = _append_required_adverse_signal(
        "아일리아 매출과 시장 변화를 확인했습니다.",
        (result,),
    )

    assert "점유율은 2.3%p 하락" in answer


def test_r10_synthesis_fallback_preserves_entity_bundle_members() -> None:
    from jw_chat_agent_poc.service.v4.synthesizer import _evidence_fallback

    bundle = {
        "calls": [
            {
                "entity_bundle": {
                    "anchor": "리바로",
                    "period_start": "2025-09",
                    "period_end": "2026-06",
                    "same_period_and_denominator": True,
                    "members": [
                        {
                            "brand": "리바로",
                            "company": "JW",
                            "rank": 2,
                            "role": "target",
                            "render_data": {
                                "brand_value_series_10pt": [
                                    {"period": "2025-09", "value_억원": 10.0},
                                    {"period": "2026-06", "value_억원": 12.0},
                                ]
                            },
                        },
                        {
                            "brand": "크레스토",
                            "company": "AZ",
                            "rank": 1,
                            "role": "competitor",
                            "render_data": {
                                "brand_value_series_10pt": [
                                    {"period": "2025-09", "value_억원": 14.0},
                                    {"period": "2026-06", "value_억원": 15.0},
                                ]
                            },
                        },
                    ],
                }
            }
        ]
    }

    answer = _evidence_fallback((_mart(bundle),), question="리바로 요즘 어때")

    assert "리바로" in answer
    assert "크레스토" in answer
    assert "2025-09" in answer and "2026-06" in answer
    assert "10.0" in answer and "15.0" in answer


def test_r10_runtime_loads_injects_and_saves_structured_session_state() -> None:
    from jw_chat_agent_poc.service.v4.session_state import SessionState
    from jw_chat_agent_poc.service.v4.synthesizer import SynthesisOutcome

    initial = SessionState(
        canonical_entities=("리바로", "리바로젯"),
        referenced_entity_set=("리바로", "리바로젯"),
        comparison_anchor="리바로",
    )

    class Store:
        def __init__(self) -> None:
            self.saved: SessionState | None = None

        def load(self, session_id):
            assert session_id == "state-1"
            return initial

        def save(self, session_id, state):
            assert session_id == "state-1"
            self.saved = state

    class Planner:
        def plan_with_trace(self, question, turns, *, budget_s, state):
            assert state is initial
            return SimpleNamespace(plan=_plan(question), trace={"elapsed_ms": 1.0, "usage": {}})

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute_with_trace(self, plan, **_kwargs):
            return SimpleNamespace(
                results=(_mart({"brand": "리바로", "sales_억원": 85.87}),),
                trace={"elapsed_ms": 1.0, "tools": [], "session_result_reused": False},
            )

    class Synthesizer:
        def synthesize_with_trace(self, plan, results, turns, *, budget_s, state):
            assert state is initial
            return SynthesisOutcome(text="리바로 매출은 85.87억원입니다.", trace={"elapsed_ms": 1.0})

    store = Store()
    runtime = V4Runtime(
        planner=Planner(),
        executor=Executor(),
        synthesizer=Synthesizer(),
        state_store=store,
    )

    runtime.answer("아까 그 순위", conversation_id="state-1", turns=())

    assert store.saved is not None
    assert "리바로" in store.saved.canonical_entities
    assert store.saved.last_numeric_facts


def test_r10_cross_runtime_prior_numeric_reference_reuses_state_without_query() -> None:
    from jw_chat_agent_poc.service.v4.session_state import SessionState
    from jw_chat_agent_poc.service.v4.synthesizer import SynthesisOutcome

    class Store:
        state = SessionState(
            canonical_entities=("리바로", "리바로젯"),
            referenced_entity_set=("리바로", "리바로젯"),
            comparison_anchor="리바로",
            last_numeric_facts=(
                {"source": "mart", "path": "calls[0].render_data.rank", "value": 2},
            ),
            last_source_record_ids=("qr-42",),
        )

        def load(self, _session_id):
            return self.state

        def save(self, _session_id, state):
            self.state = state

    class Planner:
        def plan_with_trace(self, question, turns, *, budget_s, state):
            return SimpleNamespace(plan=_plan(question), trace={"elapsed_ms": 1.0, "usage": {}})

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        calls = 0

        def execute_with_trace(self, _plan, **_kwargs):
            self.calls += 1
            raise AssertionError("prior numeric state must not requery")

    class Synthesizer:
        def synthesize_with_trace(self, _plan, results, _turns, *, budget_s, state):
            assert results[0].payload["session_state_reuse"] is True
            assert results[0].payload["last_numeric_facts"][0]["value"] == 2
            return SynthesisOutcome(text="아까 확인한 순위는 2위입니다.", trace={"elapsed_ms": 1.0})

    executor = Executor()
    runtime = V4Runtime(
        planner=Planner(), executor=executor, synthesizer=Synthesizer(), state_store=Store()
    )

    answer = runtime.answer("아까 그 순위", conversation_id="state-cross-pod", turns=())

    assert executor.calls == 0
    assert "2위" in answer.text


def test_r10_runtime_runs_one_web_wave_after_confirmed_official_absence() -> None:
    from jw_chat_agent_poc.service.v4.synthesizer import SynthesisOutcome

    plan = _plan("마운자로 급여기준").model_copy(update={"answer_sources": ("hira",)})

    class Planner:
        def plan_with_trace(self, question, turns, *, budget_s):
            return SimpleNamespace(plan=plan, trace={"elapsed_ms": 1.0, "usage": {}})

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def __init__(self) -> None:
            self.filters: list[tuple[str, ...] | None] = []

        def execute_with_trace(self, _plan, **kwargs):
            source_filter = kwargs.get("source_filter")
            self.filters.append(source_filter)
            if source_filter == ("web",):
                results = (
                    SourceResult(
                        source="web",
                        query="마운자로 급여기준 부재 경과",
                        status="ok",
                        payload={"items": [{"url": "https://www.yna.co.kr/view/example", "title": "약가 협상 결렬"}]},
                    ),
                )
            else:
                results = (SourceResult(source="hira", query="마운자로 급여기준", status="empty"),)
            return SimpleNamespace(results=results, trace={"elapsed_ms": 1.0, "tools": []})

    class Synthesizer:
        def synthesize_with_trace(self, _plan, results, _turns, *, budget_s):
            context = next(result.payload["absence_context"] for result in results if result.source == "web")
            assert context["official_absence"] is True
            return SynthesisOutcome(text="현재 급여기준은 없고 협상 결렬 경과가 보도되고 있습니다.", trace={"elapsed_ms": 1.0})

    executor = Executor()
    answer = V4Runtime(planner=Planner(), executor=executor, synthesizer=Synthesizer()).answer(
        "마운자로 급여기준", conversation_id="absence-1", turns=()
    )

    assert executor.filters == [None, ("web",)]
    assert answer.trace["absence_context"]["triggered"] is True
