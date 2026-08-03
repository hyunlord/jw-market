from __future__ import annotations

import inspect

import pytest

from jw_chat_agent_poc.contracts import RenderAuthorization
from jw_chat_agent_poc.service.charts import (
    build_charts,
    issue_render_authorization,
)


def _series_result() -> dict:
    return {
        "resolution": {"canonical_brand": "리바로"},
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "brand": "리바로",
                    "metric": "series",
                    "brand_value_series_10pt": [
                        {"period": "2026-03", "value_krw": 8_711_248_139.54},
                        {"period": "2026-04", "value_krw": 8_493_234_217.11},
                    ],
                },
            }
        ],
    }


def test_build_charts_requires_render_authorization() -> None:
    parameter = inspect.signature(build_charts).parameters["authorization"]

    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        build_charts(_series_result(), question="리바로 매출 추이")  # type: ignore[call-arg]


def test_failed_render_authorization_suppresses_charts() -> None:
    result = _series_result()
    issued = issue_render_authorization(
        result,
        question="리바로 매출 추이",
        answer="",
        enforce_binding=True,
    )
    denied = RenderAuthorization(
        passed=False,
        authorized_chart_ids=(),
        evidence_bundle_hash=issued.evidence_bundle_hash,
    )

    assert build_charts(
        result,
        authorization=denied,
        question="리바로 매출 추이",
    ) == []


def test_binding_failure_issues_denied_authorization_without_chart_ids() -> None:
    result = {
        **_series_result(),
        "_qa_claim_gate": {
            "blocked_claim_count": 1,
            "disposition": "blocked",
        },
    }

    authorization = issue_render_authorization(
        result,
        question="리바로 매출 추이",
        answer="",
        enforce_binding=True,
    )

    assert authorization.passed is False
    assert authorization.authorized_chart_ids == ()


def test_unlisted_chart_id_is_not_materialized() -> None:
    result = _series_result()
    issued = issue_render_authorization(
        result,
        question="리바로 매출 추이",
        answer="",
        enforce_binding=True,
    )
    unauthorized = RenderAuthorization(
        passed=True,
        authorized_chart_ids=(),
        evidence_bundle_hash=issued.evidence_bundle_hash,
    )

    assert build_charts(
        result,
        authorization=unauthorized,
        question="리바로 매출 추이",
    ) == []


def test_only_listed_chart_id_is_materialized() -> None:
    result = _series_result()
    result["tool_calls"][0]["render_data"]["market_size_series"] = [
        {"period": "2026-03", "value_krw": 228_838_670_570.0},
        {"period": "2026-04", "value_krw": 225_677_368_890.0},
    ]
    issued = issue_render_authorization(
        result,
        question="리바로 매출 추이",
        answer="",
        enforce_binding=True,
    )
    restricted = RenderAuthorization(
        passed=True,
        authorized_chart_ids=issued.authorized_chart_ids[:1],
        evidence_bundle_hash=issued.evidence_bundle_hash,
    )

    charts = build_charts(
        result,
        authorization=restricted,
        question="리바로 매출 추이",
    )

    assert [chart["title"] for chart in charts] == ["리바로 매출 추이"]


def test_authorization_for_another_evidence_bundle_is_rejected() -> None:
    result = _series_result()
    issued = issue_render_authorization(
        result,
        question="리바로 매출 추이",
        answer="",
        enforce_binding=True,
    )
    wrong_bundle = RenderAuthorization(
        passed=True,
        authorized_chart_ids=issued.authorized_chart_ids,
        evidence_bundle_hash="0" * 64,
    )

    assert build_charts(
        result,
        authorization=wrong_bundle,
        question="리바로 매출 추이",
    ) == []


def test_issued_authorization_preserves_chart_payload() -> None:
    result = _series_result()
    authorization = issue_render_authorization(
        result,
        question="리바로 매출 추이",
        answer="",
        enforce_binding=True,
    )

    charts = build_charts(
        result,
        authorization=authorization,
        question="리바로 매출 추이",
    )

    assert [chart["title"] for chart in charts] == ["리바로 매출 추이"]
    assert charts[0]["datasets"][0]["data"] == [8_711_248_139.54, 8_493_234_217.11]
