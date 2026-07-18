from __future__ import annotations

import json

import pytest

from jw_chat_agent_poc.agentic import sales_filter_aliases
from jw_chat_agent_poc.agent_loop.population_specs import strict_query_plan
from jw_chat_agent_poc.agentic.sales_filter_extraction import extract_metric_filter_entries
from jw_chat_agent_poc.tools.query_layer.store import MartRecord


@pytest.mark.parametrize(
    ("question", "expected"),
    (
        ("종합병원 채널에서 리바로 매출은?", "종병"),
        ("상급종합병원 채널에서 리바로 매출은?", "상급종병"),
        ("상급병원 채널에서 리바로 매출은?", "상급종병"),
        ("병원 채널에서 리바로 매출은?", "병원"),
        ("의원 채널에서 리바로 매출은?", "의원"),
        ("보건소 채널에서 리바로 매출은?", "보건소"),
        ("치과의원 채널에서 리바로 매출은?", "기타"),
    ),
)
def test_channel_matching_uses_specific_aliases(question: str, expected: str) -> None:
    match_channel_in_text = getattr(sales_filter_aliases, "match_channel_in_text", None)
    assert callable(match_channel_in_text)

    assert match_channel_in_text(question) == expected
    assert ("channel", expected) in extract_metric_filter_entries(question)


def test_channel_matching_is_longest_first_and_deterministic() -> None:
    question = "상급종합병원과 병원을 비교해줘"
    match_channel_in_text = getattr(sales_filter_aliases, "match_channel_in_text", None)
    assert callable(match_channel_in_text)

    assert match_channel_in_text(question) == "상급종병"
    assert match_channel_in_text(question) == "상급종병"


def test_unknown_channel_keeps_existing_no_match_behavior() -> None:
    question = "온라인몰 채널에서 리바로 매출은?"
    match_channel_in_text = getattr(sales_filter_aliases, "match_channel_in_text", None)
    assert callable(match_channel_in_text)

    assert match_channel_in_text(question) is None
    assert all(field != "channel" for field, _value in extract_metric_filter_entries(question))


def test_alias_conflict_is_rejected() -> None:
    build_alias_map = getattr(sales_filter_aliases, "_build_alias_map", None)
    assert callable(build_alias_map)

    with pytest.raises(ValueError, match="conflicting channel alias"):
        build_alias_map((("같은별칭", "병원"), ("같은별칭", "의원")))


def test_population_plan_uses_shared_channel_matcher() -> None:
    plan = strict_query_plan("상급종합병원에서 리바로 성분 점유율은?", "리바로")

    assert plan is not None
    assert plan.specs[0]["filters"] == {"channel": "상급종병"}


@pytest.mark.parametrize(
    ("question", "expected_channel"),
    (
        ("종합병원 채널에서 리바로 매출은?", "종병"),
        ("상급종합병원 채널에서 리바로 매출은?", "상급종병"),
        ("병원 채널에서 리바로 매출은?", "병원"),
    ),
)
def test_specific_channel_sales_plan_filters_instead_of_listing_all_channels(
    question: str,
    expected_channel: str,
) -> None:
    plan = strict_query_plan(question, "리바로")

    assert plan is not None
    assert plan.unsupported_message == ""
    assert plan.specs == (
        {
            "source": "ubist",
            "view": "market_landscape",
            "dimensions": ["product"],
            "group_by": ["product"],
            "metrics": ["sales"],
            "derive": [],
            "filters": {"brand": "리바로", "channel": expected_channel},
            "limit": 1,
        },
    )


def test_channel_top_brand_plan_ranks_products_inside_requested_channel() -> None:
    plan = strict_query_plan("리바로 시장에서 상급종합병원 채널 내 상위 브랜드를 알려줘", "리바로")

    assert plan is not None
    assert plan.specs[0]["group_by"] == ["product"]
    assert plan.specs[0]["metrics"] == ["share"]
    assert plan.specs[0]["filters"] == {"channel": "상급종병"}
    assert plan.specs[0]["limit"] == 5


def test_unknown_specific_channel_is_rejected() -> None:
    plan = strict_query_plan("온라인몰 채널에서 리바로 매출은?", "리바로")

    assert plan is not None
    assert plan.specs == ()
    assert "온라인몰" in plan.unsupported_message
    assert "지원" in plan.unsupported_message


def test_generic_channel_distribution_remains_unchanged() -> None:
    plan = strict_query_plan("리바로 채널별 매출을 보여줘", "리바로")

    assert plan is not None
    assert plan.specs[0]["dimensions"] == ["channel"]
    assert plan.specs[0]["filters"] == {"brand": "리바로"}


def test_mart_record_normalises_storage_channel_keys() -> None:
    history = {"2026-05": {"raw_value": 100.0}}
    row = {
        "ml_id": "ml_test",
        "brand_name": "리바로",
        "source": "ubist",
        "measure": "sales",
        "metric_history": json.dumps(history, ensure_ascii=False),
        "channel_data": json.dumps(
            {
                "상급종합병원": history,
                "종합병원": history,
                "병원": history,
                "의원": history,
                "보건소": history,
                "기타(치과의원, 치과병원 등)": history,
            },
            ensure_ascii=False,
        ),
        "specialty_data": "{}",
        "dimension_data": "{}",
        "by_dimension": "{}",
    }

    record = MartRecord.from_row(row)

    assert tuple(record.channel_data) == (
        "상급종병",
        "종병",
        "병원",
        "의원",
        "보건소",
        "기타",
    )
