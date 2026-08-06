from __future__ import annotations

import importlib.util
import json


GENERAL_HELP = (
    "시장, 브랜드, 기간, 지표를 포함해 질문하면 확인 가능한 근거를 조회해 답합니다. "
    "필수 정보가 모호하면 부족한 항목만 다시 확인합니다."
)


def _legacy_no_tool() -> dict[str, object]:
    return {
        "answer": GENERAL_HELP,
        "sources": [],
        "tool_calls": [],
        "router_diagnostics": {
            "routing_v4": {
                "proposed_routing_signature": {
                    "routing_decision": {"route_outcome": "NO_TOOL"}
                },
                "runtime_status": "typed_stop",
            }
        },
    }


def test_v3_cutover_module_exists() -> None:
    assert importlib.util.find_spec("jw_chat_agent_poc.tool_use.v3_cutover") is not None


def test_config_is_disabled_by_default(monkeypatch) -> None:
    from jw_chat_agent_poc.tool_use.v3_cutover import V3CutoverConfig

    monkeypatch.delenv("JW_CHAT_V3_CUTOVER_ENABLED", raising=False)

    assert V3CutoverConfig.from_env().enabled is False


def test_disabled_and_non_target_paths_do_not_build_pipeline(monkeypatch) -> None:
    from jw_chat_agent_poc.tool_use.v3_cutover import V3CutoverConfig, apply_v3_cutover

    calls: list[str] = []
    legacy = _legacy_no_tool()
    disabled = apply_v3_cutover(
        "리바로 원인분석 좀 뽑아줘",
        legacy,
        config=V3CutoverConfig(enabled=False),
        pipeline_factory=lambda: calls.append("built"),
    )
    answered = apply_v3_cutover(
        "아일리아 매출 알려줘",
        {"answer": "80.39억원", "sources": ["market"], "tool_calls": [{"status": "ok"}]},
        config=V3CutoverConfig(enabled=True),
        pipeline_factory=lambda: calls.append("built"),
    )

    assert disabled is legacy
    assert json.dumps(disabled, ensure_ascii=False, sort_keys=True) == json.dumps(
        legacy, ensure_ascii=False, sort_keys=True
    )
    assert answered["answer"] == "80.39억원"
    assert calls == []


def test_uncovered_result_is_replaced_only_for_enabled_domain() -> None:
    from jw_chat_agent_poc.tool_use.v3_cutover import (
        V3CutoverConfig,
        V3ServingResult,
        apply_v3_cutover,
    )

    class Pipeline:
        def run(self, question: str) -> V3ServingResult:
            assert question == "리바로 원인분석 좀 뽑아줘"
            return V3ServingResult(
                domain="market",
                answer="근거가 확인된 원인분석입니다.",
                limitations=("영업활동 자료는 확인하지 못했습니다.",),
                sources=("market.get_deep_analysis",),
                charts=(),
                trace={"accepted_claim_count": 1},
                tool_calls=(),
            )

    legacy = _legacy_no_tool()
    allowed = apply_v3_cutover(
        "리바로 원인분석 좀 뽑아줘",
        legacy,
        config=V3CutoverConfig(enabled=True, domains=frozenset({"market"})),
        pipeline_factory=Pipeline,
    )
    blocked = apply_v3_cutover(
        "리바로 원인분석 좀 뽑아줘",
        legacy,
        config=V3CutoverConfig(enabled=True, domains=frozenset({"regulatory"})),
        pipeline_factory=Pipeline,
    )

    assert allowed["v3_cutover_ready"] is True
    assert "근거가 확인된 원인분석입니다." in allowed["answer"]
    assert "영업활동 자료는 확인하지 못했습니다." in allowed["answer"]
    assert blocked is legacy


def test_pipeline_failure_fails_open_and_records_reason(caplog) -> None:
    from jw_chat_agent_poc.tool_use.v3_cutover import V3CutoverConfig, apply_v3_cutover

    class BrokenPipeline:
        def run(self, question: str) -> object:
            del question
            raise RuntimeError("provider unavailable")

    legacy = _legacy_no_tool()
    result = apply_v3_cutover(
        "리바로 원인분석 좀 뽑아줘",
        legacy,
        config=V3CutoverConfig(enabled=True),
        pipeline_factory=BrokenPipeline,
    )

    assert result is legacy
    assert "v3_cutover_failed_open" in caplog.text


def test_mixed_domain_requires_every_selected_domain_to_be_enabled() -> None:
    from jw_chat_agent_poc.tool_use.v3_cutover import (
        V3CutoverConfig,
        V3ServingResult,
        apply_v3_cutover,
    )

    class MixedPipeline:
        def run(self, question: str) -> V3ServingResult:
            del question
            return V3ServingResult(
                "market+web", "검증된 답변", (), (), (), {}, ()
            )

    legacy = _legacy_no_tool()

    assert apply_v3_cutover(
        "질문",
        legacy,
        config=V3CutoverConfig(enabled=True, domains=frozenset({"market"})),
        pipeline_factory=MixedPipeline,
    ) is legacy
    assert apply_v3_cutover(
        "질문",
        legacy,
        config=V3CutoverConfig(
            enabled=True,
            domains=frozenset({"market", "web"}),
        ),
        pipeline_factory=MixedPipeline,
    )["v3_cutover_ready"] is True


def test_chart_numbers_require_cited_fact_literals() -> None:
    from jw_chat_agent_poc.tool_use.v3_cutover import grounded_chart_specs

    facts = {
        "v3-shadow:market.get_timeseries:abc": {
            "periods": ["2025-Q4", "2026-Q1"],
            "values": [79.1, 80.39],
        }
    }
    charts = (
        {
            "type": "line",
            "title": "매출 추이",
            "labels": ["2025-Q4", "2026-Q1"],
            "datasets": [{"label": "매출", "data": [79.1, 80.39]}],
            "evidence_refs": ["v3-shadow:market.get_timeseries:abc"],
        },
        {
            "type": "line",
            "title": "근거 없는 추이",
            "labels": ["2025-Q4"],
            "datasets": [{"label": "매출", "data": [999.0]}],
            "evidence_refs": ["v3-shadow:market.get_timeseries:abc"],
        },
        {
            "type": "line",
            "title": "근거 없는 기간",
            "labels": ["2027-Q1"],
            "datasets": [{"label": "매출", "data": [80.39]}],
            "evidence_refs": ["v3-shadow:market.get_timeseries:abc"],
        },
    )

    assert grounded_chart_specs(charts, facts) == (charts[0],)


def test_app_flag_off_is_object_identical_and_does_not_import_cutover(
    monkeypatch,
) -> None:
    import builtins

    from jw_chat_agent_poc.service.app import _apply_v3_cutover_if_enabled

    monkeypatch.delenv("JW_CHAT_V3_CUTOVER_ENABLED", raising=False)
    legacy = _legacy_no_tool()
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "jw_chat_agent_poc.tool_use.v3_cutover":
            raise AssertionError("flag-off path imported the V3 cutover module")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert _apply_v3_cutover_if_enabled("질문", legacy) is legacy


def test_v3_final_answer_uses_validated_text_and_charts_without_generation() -> None:
    from jw_chat_agent_poc.service.app import _compute_final_answer

    chart = {
        "type": "line",
        "title": "매출 추이",
        "labels": ["2025-Q4", "2026-Q1"],
        "datasets": [{"label": "매출", "data": [79.1, 80.39]}],
        "evidence_refs": ["v3-shadow:market.get_timeseries:abc"],
    }
    result = {
        "answer": "검증된 claim입니다.",
        "sources": ["market.get_timeseries"],
        "tool_calls": [],
        "charts": [chart],
        "v3_cutover_ready": True,
    }

    final = _compute_final_answer("매출 추이", result, "conversation")

    assert final.text == "검증된 claim입니다."
    assert final.charts == [chart]
    assert final.sources == ("market.get_timeseries",)


def test_v3_chart_is_emitted_by_existing_sse_presenter() -> None:
    from jw_chat_agent_poc.service.sse_presenter import iter_final_answer_events

    chart = {
        "type": "line",
        "title": "매출 추이",
        "labels": ["2026-Q1"],
        "datasets": [{"label": "매출", "data": [80.39]}],
        "evidence_refs": ["v3-shadow:market.get_timeseries:abc"],
    }
    events = "".join(
        iter_final_answer_events(
            conversation_id="c1",
            source_labels=(),
            file_sources=(),
            text="검증된 답변",
            charts=(chart,),
            timing={},
            trace={},
        )
    )

    assert "event: charts" in events
    assert json.dumps([chart], ensure_ascii=False, separators=(",", ":")) in events


def test_chart_merge_deduplicates_equal_data_even_when_titles_differ() -> None:
    from jw_chat_agent_poc.tool_use.v3_cutover import _dedupe_charts_by_data

    charts = (
        {
            "type": "line",
            "title": "시장 매출 추이",
            "labels": ["2026-04", "2026-05"],
            "datasets": [{"label": "시장 매출", "data": [100.0, 104.0]}],
            "evidence_refs": ["v3-shadow:market.get_brand_metric:a"],
        },
        {
            "type": "line",
            "title": "시장 규모 추이",
            "labels": ["2026-04", "2026-05"],
            "datasets": [{"label": "시장 규모", "data": [100.0, 104.0]}],
            "evidence_refs": ["v3-shadow:market.get_brand_metric:b"],
        },
        {
            "type": "line",
            "title": "리바로 매출 추이",
            "labels": ["2026-04", "2026-05"],
            "datasets": [{"label": "리바로 매출", "data": [70.0, 71.0]}],
            "evidence_refs": ["v3-shadow:market.get_brand_metric:c"],
        },
    )

    result = _dedupe_charts_by_data(charts)

    assert [chart["title"] for chart in result] == [
        "시장 매출 추이",
        "리바로 매출 추이",
    ]
