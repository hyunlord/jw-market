from __future__ import annotations

from jw_chat_agent_poc.service.charts import build_charts
from jw_chat_agent_poc.service.bq_charts import build_bq_chart_specs


def test_line_chart_preserves_null_and_zero_with_evidence_refs() -> None:
    # Given: a BQ line payload with a missing point and a real zero.
    payload = {
        "chart_type": "line",
        "title": "리바로 매출 추이",
        "source": "BQ Q1",
        "scope": "MARKET",
        "evidence_refs": ["bq:q1:row-1"],
        "labels": ["2026-01", "2026-02", "2026-03"],
        "datasets": [{"label": "매출", "unit": "KRW", "data": [12.0, None, 0.0]}],
    }

    # When: the chart spec is built.
    charts = build_bq_chart_specs([payload])

    # Then: the existing line shape is retained and null is not coerced to zero.
    assert charts[0]["type"] == "line"
    assert charts[0]["datasets"][0]["data"] == [12.0, None, 0.0]
    assert charts[0]["evidence_refs"] == ["bq:q1:row-1"]


def test_bar_chart_reuses_existing_payload_shape_when_evidence_exists() -> None:
    # Given: a BQ bar payload using labels and one dataset.
    payload = {
        "chart_type": "bar",
        "title": "브랜드별 점유율",
        "source": "BQ Q2",
        "scope": "MARKET",
        "evidence_refs": ["bq:q2:rows-1-3"],
        "labels": ["A", "B"],
        "datasets": [{"label": "M/S %", "unit": "%", "data": [0.0, None]}],
    }

    # When: the chart spec is built.
    charts = build_bq_chart_specs([payload])

    # Then: zero and null keep their distinct meanings.
    assert charts[0]["type"] == "bar"
    assert charts[0]["datasets"][0]["data"] == [0.0, None]


def test_doughnut_chart_reuses_existing_payload_shape_when_evidence_exists() -> None:
    # Given: a BQ donut request using the renderer's existing doughnut type.
    payload = {
        "chart_type": "doughnut",
        "title": "채널 구성",
        "source": "BQ Q3",
        "scope": "MARKET",
        "evidence_refs": ["bq:q3:rows-1-2"],
        "labels": ["종병", "의원"],
        "datasets": [{"label": "매출", "unit": "KRW", "data": [100.0, 0.0]}],
    }

    # When: the chart spec is built.
    charts = build_bq_chart_specs([payload])

    # Then: the output uses Chart.js-compatible doughnut, not a new renderer type.
    assert charts[0]["type"] == "doughnut"
    assert charts[0]["datasets"][0]["data"] == [100.0, 0.0]


def test_scatter_chart_keeps_point_null_and_zero_values() -> None:
    # Given: scatter points from BQ evidence.
    payload = {
        "chart_type": "scatter",
        "title": "성장률 대비 점유율",
        "source": "BQ Q4",
        "scope": "MARKET",
        "evidence_refs": ["bq:q4:rows-1-2"],
        "datasets": [
            {
                "label": "브랜드",
                "unit": "%",
                "data": [{"x": 12.5, "y": None}, {"x": 0.0, "y": 0.0}],
            }
        ],
    }

    # When: the chart spec is built.
    charts = build_bq_chart_specs([payload])

    # Then: scatter point values are not coerced or dropped.
    assert charts[0]["type"] == "scatter"
    assert charts[0]["datasets"][0]["data"] == [{"x": 12.5, "y": None}, {"x": 0.0, "y": 0.0}]


def test_waterfall_request_is_rendered_as_bar_with_chart_kind() -> None:
    # Given: a waterfall request expressed with Chart.js floating bar data.
    payload = {
        "chart_type": "waterfall",
        "title": "매출 증감 분해",
        "source": "BQ Q4",
        "scope": "MARKET",
        "evidence_refs": ["bq:q4:waterfall"],
        "labels": ["기초", "감소", "기말"],
        "datasets": [{"label": "증감", "unit": "KRW", "data": [[0.0, 100.0], [100.0, 80.0], [80.0, 80.0]]}],
    }

    # When: the chart spec is built.
    charts = build_bq_chart_specs([payload])

    # Then: the renderer receives a bar chart plus additive waterfall metadata.
    assert charts[0]["type"] == "bar"
    assert charts[0]["chart_kind"] == "waterfall"
    assert charts[0]["datasets"][0]["data"][1] == [100.0, 80.0]


def test_dual_axis_line_assigns_dataset_axes_without_summing_units() -> None:
    # Given: a dual-axis request with KRW and percent series.
    payload = {
        "chart_type": "dual_axis_line",
        "title": "매출과 성장률",
        "source": "BQ Q1",
        "scope": "MARKET",
        "evidence_refs": ["bq:q1:dual-axis"],
        "labels": ["2026-01", "2026-02"],
        "axes": {"y": {"unit": "KRW"}, "y1": {"unit": "%", "position": "right"}},
        "datasets": [
            {"label": "매출", "unit": "KRW", "yAxisID": "y", "data": [100.0, 0.0]},
            {"label": "성장률", "unit": "%", "yAxisID": "y1", "data": [None, 3.2]},
        ],
    }

    # When: the chart spec is built.
    charts = build_bq_chart_specs([payload])

    # Then: the line chart keeps separate axes and raw series values.
    assert charts[0]["type"] == "line"
    assert [dataset["yAxisID"] for dataset in charts[0]["datasets"]] == ["y", "y1"]
    assert charts[0]["datasets"][0]["data"] == [100.0, 0.0]
    assert charts[0]["datasets"][1]["data"] == [None, 3.2]


def test_mixed_request_keeps_market_and_file_specs_separate_without_total() -> None:
    # Given: a MIXED request with one market and one file chart candidate.
    payload = {
        "scope": "MIXED",
        "market": [
            {
                "chart_type": "bar",
                "title": "시장 매출",
                "source": "BQ Q1",
                "evidence_refs": ["bq:q1:market"],
                "labels": ["2026"],
                "datasets": [{"label": "시장", "unit": "KRW", "data": [100.0]}],
            }
        ],
        "file": [
            {
                "chart_type": "bar",
                "title": "첨부 전망",
                "source": "report.xlsx",
                "evidence_refs": ["file:sheet1:row2"],
                "labels": ["2026"],
                "datasets": [{"label": "파일", "unit": "KRW", "data": [30.0]}],
            }
        ],
    }

    # When: the chart specs are built.
    charts = build_bq_chart_specs([payload])

    # Then: market and file evidence remain separate and no summed MIXED chart is produced.
    assert [chart["scope"] for chart in charts] == ["MARKET", "FILE"]
    assert [chart["datasets"][0]["data"] for chart in charts] == [[100.0], [30.0]]
    assert all(chart["scope"] != "MIXED" for chart in charts)


def test_payload_without_evidence_refs_is_suppressed() -> None:
    # Given: a plausible chart payload without evidence references.
    payload = {
        "chart_type": "line",
        "title": "근거 없는 추이",
        "source": "BQ Q1",
        "scope": "MARKET",
        "labels": ["2026-01", "2026-02"],
        "datasets": [{"label": "매출", "unit": "KRW", "data": [1.0, 2.0]}],
    }

    # When: the chart spec is built.
    charts = build_bq_chart_specs([payload])

    # Then: no chart is emitted without evidence refs.
    assert charts == []


def test_payload_without_explicit_scope_is_suppressed() -> None:
    payload = {
        "chart_type": "line",
        "title": "스코프 없는 추이",
        "source": "BQ Q1",
        "evidence_refs": ["bq:q1:row-1"],
        "labels": ["2026-01"],
        "datasets": [{"label": "매출(KRW)", "unit": "KRW", "data": [1.0]}],
    }

    assert build_bq_chart_specs([payload]) == []


def test_service_chart_builder_includes_grounded_bq_payloads() -> None:
    payload = {
        "chart_type": "dual_axis_line",
        "title": "CSD 활동과 UBIST 매출 시점 대조",
        "source": "CSD+UBIST side-by-side",
        "scope": "MARKET",
        "evidence_refs": ["CSD.series", "UBIST.series"],
        "labels": ["2026-01", "2026-02"],
        "axes": {"y": {"unit": "건"}, "y1": {"unit": "KRW"}},
        "datasets": [
            {"label": "CSD 활동(건)", "unit": "건", "yAxisID": "y", "data": [12.0, 37.0]},
            {"label": "UBIST 매출(KRW)", "unit": "KRW", "yAxisID": "y1", "data": [80.0, 84.0]},
        ],
    }
    result = {
        "tool_calls": [
            {
                "tool": "bq_analysis",
                "render_data": {"contract_id": "D2", "chart_payloads": [payload]},
            }
        ]
    }

    charts = build_charts(result, question="리바로 영업활동이 매출에 영향 줬어?")

    assert charts == build_bq_chart_specs([payload])
