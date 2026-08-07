from __future__ import annotations

import json
from pathlib import Path

from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.agent_loop.requested_source import extract_requested_sources
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.query_layer import (
    MartRecord,
    StaticStrategicMartReader,
    StrategicQueryLayer,
)


def test_structured_route_uses_explicit_source_when_market_has_it() -> None:
    layer = _layer(
        _record("리바로", "iqvia_nsa", "2026-Q1", 218.7),
        _record("리바로", "ubist", "2026-03", 200.0),
    )

    result = _agent(layer).answer("리바로 IQVIA 매출 알려줘")

    metric_calls = _metric_calls(result)
    assert metric_calls
    assert {
        call["render_data"]["query_spec"]["source"]
        for call in metric_calls
    } == {"iqvia_nsa"}
    assert result["agent_loop_metrics"]["requested_source"] == "iqvia_nsa"
    assert result["agent_loop_metrics"]["served_source"] == "iqvia_nsa"


def test_structured_route_preserves_unavailable_request_and_explains_fallback() -> None:
    layer = _layer(_record("리바로", "ubist", "2026-05", 80.39))

    result = _agent(layer).answer("리바로 IQVIA 매출 알려줘")

    failed_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "query_failed"
    ]
    assert failed_calls
    assert {
        call["render_data"]["arguments"]["source"]
        for call in failed_calls
    } == {"iqvia_nsa"}
    assert result["agent_loop_metrics"]["requested_source"] == "iqvia_nsa"
    assert result["agent_loop_metrics"]["served_source"] is None
    assert "80.39" not in result["answer"]
    assert "다른 소스 값으로 대체하지 않습니다" in result["answer"]
    assert result["router_diagnostics"]["gate_reason"] == "requested_source_unavailable"


def test_explicit_ubist_uses_ubist_without_mismatch_notice() -> None:
    layer = _layer(
        _record("리바로", "ubist", "2026-05", 80.39),
        _record("리바로", "iqvia_nsa", "2026-Q1", 90.86),
    )

    result = _agent(layer).answer("리바로 UBIST 매출 알려줘")

    assert {
        call["render_data"]["query_spec"]["source"]
        for call in _metric_calls(result)
    } == {"ubist"}
    assert result["agent_loop_metrics"]["requested_source"] == "ubist"
    assert result["agent_loop_metrics"]["served_source"] == "ubist"
    assert "측정 대상이 다른" not in result["markdown_response"]["notice_md"]


def test_iqvia_only_market_serves_iqvia_request(
    tmp_path: Path,
) -> None:
    resolver = _fixture_resolver(tmp_path, "아일리아", "S01P0")
    layer = _layer(
        _record(
            "아일리아",
            "iqvia_nsa",
            "2026-Q1",
            218.7,
            market_id="S01P0",
        )
    )

    result = _agent(layer, resolver=resolver).answer("아일리아 IQVIA 매출 알려줘")

    assert {
        call["render_data"]["query_spec"]["source"]
        for call in _metric_calls(result)
    } == {"iqvia_nsa"}
    assert result["agent_loop_metrics"]["requested_source"] == "iqvia_nsa"
    assert result["agent_loop_metrics"]["served_source"] == "iqvia_nsa"


def test_source_unspecified_keeps_existing_ubist_default() -> None:
    layer = _layer(
        _record("리바로", "ubist", "2026-05", 80.39),
        _record("리바로", "iqvia_nsa", "2026-Q1", 90.86),
    )

    result = _agent(layer).answer("리바로 매출 알려줘")

    assert {
        call["render_data"]["query_spec"]["source"]
        for call in _metric_calls(result)
    } == {"ubist"}
    assert result["agent_loop_metrics"]["requested_source"] is None
    assert result["agent_loop_metrics"]["served_source"] == "ubist"
    assert "측정 대상이 다른" not in result["markdown_response"]["notice_md"]
    source_rows = [
        line
        for line in result["markdown_response"]["sources_md"].splitlines()
        if line.startswith("|")
    ]
    assert source_rows
    # 8 columns + the leading and trailing empty split segments
    assert all(len(row.split("|")) == 10 for row in source_rows)


def test_notice_markdown_reaches_final_genos_prompt_without_digits() -> None:
    notice = (
        "이 시장은 원외 처방(UBIST) 기준으로 정의돼 있습니다. "
        "제조사 출하(IQVIA NSA) 기준과는 측정 대상이 다른 지표입니다."
    )
    markdown_response = {
        "fact_md": "- 리바로 매출",
        "notice_md": f"## 안내\n\n- {notice}\n- 일반 처리 안내",
    }

    prompt = GenosClient._markdown_messages(
        "리바로 IQVIA랑 UBIST 수치가 다른데 왜?",
        markdown_response,
    )[1]["content"]

    assert notice in prompt
    assert "일반 처리 안내" not in prompt
    assert not any(character.isdigit() for character in notice)


def test_requested_source_extraction_is_bounded_to_allow_list() -> None:
    assert extract_requested_sources("리바로 IQVIA 매출") == ("iqvia_nsa",)
    assert extract_requested_sources("리바로 유비스트 매출") == ("ubist",)
    assert extract_requested_sources("IQVIA랑 UBIST 수치가 다른데 왜?") == (
        "iqvia_nsa",
        "ubist",
    )
    assert extract_requested_sources("리바로 다른 출처 매출") == ()


def test_iqvia_month_request_does_not_silently_switch_to_ubist(
    tmp_path: Path,
) -> None:
    resolver = _fixture_resolver(tmp_path, "가드렛", "ml_003")
    layer = _layer(
        _record(
            "가드렛",
            "ubist",
            "2026-05",
            40.0,
            market_id="ml_003",
        ),
        _record(
            "가드렛",
            "iqvia_nsa",
            "2026-Q1",
            120.0,
            market_id="ml_003",
        ),
    )

    result = _agent(layer, resolver=resolver).answer(
        "가드렛 2026-05 IQVIA 매출 알려줘"
    )

    assert result["agent_loop_metrics"]["status"] == "no_data"
    assert result["agent_loop_metrics"]["requested_source"] == "iqvia_nsa"
    assert {call["source"] for call in _metric_calls(result)} == {"IQVIA"}


def _agent(
    layer: StrategicQueryLayer,
    *,
    resolver: BrandResolver | None = None,
) -> ToolUseAgent:
    return ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=resolver or BrandResolver(mode="fixture"),
        query_layer=layer,
    )


def _metric_calls(result: dict[str, object]) -> list[dict[str, object]]:
    calls = result["tool_calls"]
    assert isinstance(calls, list)
    return [
        call
        for call in calls
        if isinstance(call, dict)
        and call.get("tool") == "get_brand_metric"
        and isinstance(call.get("render_data"), dict)
    ]


def _layer(*records: MartRecord) -> StrategicQueryLayer:
    return StrategicQueryLayer(reader=StaticStrategicMartReader(tuple(records)))


def _record(
    brand: str,
    source: str,
    period: str,
    value_eok: float,
    *,
    market_id: str = "ml_006",
) -> MartRecord:
    return MartRecord(
        ml_id=market_id,
        brand_name=brand,
        source=source,
        measure="sales",
        metric_history={
            period: {
                "raw_value": value_eok * 100_000_000,
                "ms": 10.0,
                "source_status": "OK",
            }
        },
        channel_data={},
        specialty_data={},
        dimension_data={},
        by_dimension={"company": "테스트제약", "molecule": f"{brand}성분"},
    )


def _fixture_resolver(
    tmp_path: Path,
    brand: str,
    market_id: str,
) -> BrandResolver:
    fixture_path = tmp_path / "brand_catalog.json"
    fixture_path.write_text(
        json.dumps(
            [
                {
                    "canonical_brand": brand,
                    "aliases": [],
                    "audit_code": f"fixture_{brand}",
                    "molecule_en": [],
                    "atc": [],
                    "edi_code": None,
                    "item_seq": None,
                    "market_id": market_id,
                    "market_name": market_id,
                    "evidence_source": "test fixture",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return BrandResolver(fixture_path=fixture_path, mode="fixture")
