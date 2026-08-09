from __future__ import annotations

import ast
import importlib
import inspect

import pytest

from pipeline.scripts.api.brand_activity_brand_resolver import resolve_brand_set
from pipeline.scripts.api.brand_activity_csd_activity_contract import CsdActivitySeriesRequest
from pipeline.scripts.api.brand_activity_topic_matrix import topic_data_status
from pipeline.scripts.api.models.brand_activity import BrandActivityTopicsRequest


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    (
        ("pipeline.scripts.api.brand_activity_topic_matrix", "get_topic_brand_payload"),
        ("pipeline.scripts.api.brand_activity_csd_activity_series", "get_csd_activity_series"),
        ("pipeline.scripts.api.brand_activity_csd_timeseries", "get_csd_timeseries"),
        ("pipeline.scripts.api.brand_activity_interest_rx_matrix", "get_interest_rx_matrix"),
        ("pipeline.scripts.api.brand_activity_interest_timeseries", "get_interest_timeseries"),
    ),
)
def test_brand_activity_uses_one_cause_cohort_contract(
    module_name: str,
    function_name: str,
) -> None:
    function = getattr(importlib.import_module(module_name), function_name)
    tree = ast.parse(inspect.getsource(function))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_brand_set"
    ]

    assert len(calls) == 1
    keywords = {item.arg for item in calls[0].keywords}
    assert "source" in keywords
    assert "ranking_quarters" not in keywords
    assert "prefilter_strategic_choices" not in keywords


def test_cause_cohort_resolver_has_no_quarter_ranking_escape_hatch() -> None:
    parameters = inspect.signature(resolve_brand_set).parameters

    assert "ranking_quarters" not in parameters
    assert "prefilter_strategic_choices" not in parameters


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("IQVIA", "iqvia_nsa"),
        ("iqvia_nsa", "iqvia_nsa"),
        ("UBIST", "ubist"),
    ),
)
def test_brand_activity_requests_preserve_cause_source(raw: str, expected: str) -> None:
    common = {
        "view": "general",
        "selected_brand": "리바로",
        "filters": {"atc4": ["C10A1"]},
        "source": raw,
    }

    topic = BrandActivityTopicsRequest.model_validate(common)
    activity = CsdActivitySeriesRequest.model_validate(common)

    assert topic.source == expected
    assert activity.source == expected


@pytest.mark.parametrize(
    (
        "event_count",
        "has_mapping",
        "source_row_count",
        "classified_row_count",
        "guard_valid_row_count",
        "query_failed",
        "code",
        "label",
    ),
    (
        (3, True, 3, 3, 3, False, "available", None),
        (0, True, 686, 686, 0, False, "identity_mismatch", "재분류 필요"),
        (0, True, 3, 0, 0, False, "zero", "0"),
        (0, True, 0, 0, 0, False, "source_absent", "데이터 없음"),
        (0, False, 0, 0, 0, False, "mapping_failure", "매핑 실패"),
        (0, True, 3, 3, 0, True, "unknown", "모름"),
    ),
)
def test_topic_data_statuses_are_distinct(
    event_count: int,
    has_mapping: bool,
    source_row_count: int,
    classified_row_count: int,
    guard_valid_row_count: int,
    query_failed: bool,
    code: str,
    label: str | None,
) -> None:
    status = topic_data_status(
        event_count=event_count,
        has_mapping=has_mapping,
        source_row_count=source_row_count,
        classified_row_count=classified_row_count,
        guard_valid_row_count=guard_valid_row_count,
        query_failed=query_failed,
    )

    assert status["code"] == code
    assert status["label"] == label
    if code == "identity_mismatch":
        assert status == {
            "code": "identity_mismatch",
            "label": "재분류 필요",
            "source_row_count": 686,
            "classified_row_count": 686,
            "guard_valid_row_count": 0,
        }
