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
    ("event_count", "has_mapping", "source_present", "query_failed", "code", "label"),
    (
        (3, True, True, False, "available", None),
        (3, True, False, False, "available", None),
        (0, True, True, False, "zero", "0"),
        (0, True, False, False, "source_absent", "데이터 없음"),
        (0, False, False, False, "mapping_failure", "매핑 실패"),
        (0, True, True, True, "unknown", "모름"),
    ),
)
def test_topic_data_statuses_are_distinct(
    event_count: int,
    has_mapping: bool,
    source_present: bool,
    query_failed: bool,
    code: str,
    label: str | None,
) -> None:
    assert topic_data_status(
        event_count=event_count,
        has_mapping=has_mapping,
        source_present=source_present,
        query_failed=query_failed,
    ) == {"code": code, "label": label}
