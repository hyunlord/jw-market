from __future__ import annotations

from jw_chat_agent_poc.orchestrator.bq_causal_analysis import build_causal_analysis_call


def test_malformed_external_numbers_are_ignored_instead_of_crashing() -> None:
    calls = [
        {
            "tool": "get_brand_metric",
            "render_data": {
                "query_spec": {"source": "ubist"},
                "brand_value_series_10pt": [
                    {"period": "2026-05", "value_krw": "not-a-number"},
                ],
            },
        },
        {
            "tool": "csd_activity_trend",
            "render_data": {
                "series": [
                    {"period": "2026-04", "product_details": "not-a-number"},
                    {"period": "2026-05", "product_details": object()},
                ],
            },
        },
    ]

    assert build_causal_analysis_call(calls) is None
