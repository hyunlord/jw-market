from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from jw_chat_agent_poc.contracts import RenderAuthorization
from jw_chat_agent_poc.contracts.shadow import evidence_bundle_from_legacy_facts
from jw_chat_agent_poc.service.bq_charts import build_bq_chart_specs
from jw_chat_agent_poc.service.evidence_binding import evidence_facts_from_result
from jw_chat_agent_poc.service.chart_utils import (
    BLUE,
    TEAL,
    as_mapping,
    bar_chart,
    dedupe_charts,
    doughnut_chart,
    hhi_series,
    line_chart,
    number,
    text,
)
from jw_chat_agent_poc.service.trend_charts import market_size_series, top_brand_share_series
from jw_chat_agent_poc.tools.metrics.cache_live import CausePayloadReader


def build_charts(
    result: Mapping[str, Any],
    *,
    authorization: RenderAuthorization,
    question: str = "",
    answer: str = "",
    cause_reader: CausePayloadReader | None = None,
) -> list[dict[str, Any]]:
    """Materialize only chart specs authorized for the current evidence bundle."""

    if not authorization.passed:
        return []
    if authorization.evidence_bundle_hash != _evidence_bundle_hash(result):
        return []

    authorized_ids = set(authorization.authorized_chart_ids)
    return [
        chart
        for chart in _compile_charts(
            result,
            question=question,
            answer=answer,
            cause_reader=cause_reader,
        )
        if _chart_id(chart) in authorized_ids
    ]


def issue_render_authorization(
    result: Mapping[str, Any],
    *,
    question: str,
    answer: str,
    enforce_binding: bool,
) -> RenderAuthorization:
    """Authorize exact legacy chart specs without exposing them to the response.

    The builders still emit dictionaries directly. This private preflight derives
    stable IDs until the separately scoped ChartIntent migration replaces it.
    """

    charts = _compile_charts(result, question=question, answer=answer)
    passed = not enforce_binding or _binding_allows_render(result)
    return RenderAuthorization(
        passed=passed,
        authorized_chart_ids=tuple(_chart_id(chart) for chart in charts) if passed else (),
        evidence_bundle_hash=_evidence_bundle_hash(result),
    )


def _compile_charts(
    result: Mapping[str, Any],
    *,
    question: str = "",
    answer: str = "",
    cause_reader: CausePayloadReader | None = None,
) -> list[dict[str, Any]]:
    del cause_reader
    charts: list[dict[str, Any]] = []
    calls = [call for call in result.get("tool_calls", []) if isinstance(call, Mapping)]
    bq_payloads = [
        payload
        for call in calls
        for payload in (as_mapping(call.get("render_data")) or {}).get("chart_payloads", [])
        if isinstance(payload, Mapping)
    ]
    charts.extend(build_bq_chart_specs(bq_payloads))
    target_brand = _target_brand(result, calls)
    intent = _chart_intent(question, answer)
    if not intent:
        return _valid_charts(dedupe_charts(charts))

    charts.extend(_metric_call_charts(calls, target_brand, intent))
    charts.extend(_hira_charts(calls, intent))
    return _valid_charts(dedupe_charts(charts))


def _chart_id(chart: Mapping[str, Any]) -> str:
    payload = json.dumps(
        chart,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _evidence_bundle_hash(result: Mapping[str, Any]) -> str:
    facts = evidence_facts_from_result(result)
    return evidence_bundle_from_legacy_facts(facts).bundle_hash


def _binding_allows_render(result: Mapping[str, Any]) -> bool:
    gate = result.get("_qa_claim_gate")
    if not isinstance(gate, Mapping):
        return True
    try:
        blocked_claim_count = int(gate.get("blocked_claim_count") or 0)
    except (TypeError, ValueError):
        return False
    return blocked_claim_count == 0 and gate.get("disposition") != "unavailable"


def filter_charts_for_binding(
    charts: Sequence[Mapping[str, Any]],
    *,
    result: Mapping[str, Any],
    question: str,
) -> list[dict[str, Any]]:
    """Drop an obvious market-only sales chart from a brand-only request."""

    gate = result.get("_qa_claim_gate")
    if isinstance(gate, Mapping) and (
        int(gate.get("blocked_claim_count") or 0) > 0
        or gate.get("disposition") == "unavailable"
    ):
        return []

    calls = [call for call in result.get("tool_calls", []) if isinstance(call, Mapping)]
    target_brand = _target_brand(result, calls)
    if not target_brand or "시장" in question or "market_vs_brand" in _chart_intent(question, ""):
        return [dict(chart) for chart in charts]

    facts = evidence_facts_from_result(result)
    has_target_sales_evidence = any(
        fact.entity.strip().casefold() == target_brand.strip().casefold()
        and fact.metric in {"매출", "sales"}
        for fact in facts
    )
    if not has_target_sales_evidence:
        return [dict(chart) for chart in charts]

    return [
        dict(chart)
        for chart in charts
        if not _is_market_only_sales_chart(chart)
    ]


def _is_market_only_sales_chart(chart: Mapping[str, Any]) -> bool:
    datasets = chart.get("datasets")
    if not isinstance(datasets, Sequence) or isinstance(datasets, (str, bytes)):
        return False
    labels = {
        text(dataset.get("label"))
        for dataset in datasets
        if isinstance(dataset, Mapping)
    }
    labels.discard(None)
    return bool(labels) and labels == {"시장 매출"}


def _metric_call_charts(
    calls: Sequence[Mapping[str, Any]],
    target_brand: str | None,
    intent: set[str],
) -> list[dict[str, Any]]:
    charts: list[dict[str, Any]] = []
    share_rows: list[dict[str, Any]] = []
    for call in calls:
        render_data = as_mapping(call.get("render_data")) or {}
        if _is_single_brand_focus(render_data):
            continue
        brand = text(render_data.get("brand")) or target_brand
        brand_series = _brand_sales_series(render_data)
        market_series = market_size_series(render_data)
        top_brand_share = top_brand_share_series(render_data)
        if "series" in intent and "comparison" in intent and not _is_single_brand_scope(render_data) and top_brand_share:
            labels, datasets = top_brand_share
            charts.append(
                line_chart(
                    "상위 브랜드 점유율 추이",
                    labels,
                    datasets,
                    f"{call.get('tool', 'cache')}.render_data.level_top5_trend_series",
                    unit="%",
                )
            )
        if "market_vs_brand" in intent and brand_series and market_series and brand:
            labels, brand_values = brand_series
            market_labels, market_values = market_series
            aligned = _aligned_series(labels, brand_values, market_labels, market_values)
            if aligned:
                aligned_labels, aligned_brand_values, aligned_market_values = aligned
                charts.append(
                    line_chart(
                        f"{brand}{_and_particle(brand)} 시장 매출 추이",
                        aligned_labels,
                        [
                            {"label": f"{brand} 매출", "data": aligned_brand_values, "borderColor": TEAL, "unit": "KRW"},
                            {"label": "시장 매출", "data": aligned_market_values, "borderColor": BLUE, "unit": "KRW"},
                        ],
                        f"{call.get('tool', 'cache')}.render_data.brand_value_series_10pt+market_size_series",
                        unit="KRW",
                    )
                )
        if "series" in intent and brand_series and brand:
            labels, values = brand_series
            charts.append(
                line_chart(
                    f"{brand} 매출 추이",
                    labels,
                    [{"label": f"{brand} 매출", "data": values, "borderColor": TEAL, "unit": "KRW"}],
                    f"{call.get('tool', 'cache')}.render_data.brand_value_series_10pt",
                    unit="KRW",
                )
            )

        if "series" in intent and market_series:
            labels, values = market_series
            charts.append(
                line_chart(
                    "시장 매출 추이",
                    labels,
                    [{"label": "시장 매출", "data": values, "borderColor": TEAL, "unit": "KRW"}],
                    f"{call.get('tool', 'cache')}.render_data",
                    unit="KRW",
                )
            )

        hhi = hhi_series(render_data.get("hhi_series_5y"))
        if "hhi" in intent and hhi:
            labels, values = hhi
            charts.append(
                line_chart(
                    "HHI 추이",
                    labels,
                    [{"label": "HHI", "data": values, "borderColor": BLUE, "unit": "HHI"}],
                    f"{call.get('tool', 'cache')}.render_data",
                    unit="HHI",
                )
            )

        share = number(render_data.get("ms_recent_pct")) or number(render_data.get("market_share"))
        if brand and share is not None:
            share_rows.append({"brand": brand, "ms_recent_pct": share})

        level_segments = render_data.get("level_segments")
        if "comparison" in intent and not _is_single_brand_scope(render_data) and isinstance(level_segments, list):
            rows = {
                text(row.get("name")) or "미분류": _first_number(row.get("ms_recent_pct"), row.get("value"))
                for row in level_segments
                if isinstance(row, Mapping)
            }
            if len(rows) >= 2:
                charts.append(bar_chart(f"{text(render_data.get('level')) or '분석 Level'}별 점유율", rows, f"{call.get('tool', 'cache')}.render_data.level_segments", unit="%"))

    if "comparison" in intent and len(share_rows) >= 2:
        rows = {row["brand"]: row["ms_recent_pct"] for row in share_rows if row.get("brand")}
        if len(rows) >= 2:
            charts.append(bar_chart("브랜드별 점유율", rows, "tool_calls.render_data.ms_recent_pct", unit="%"))
    return charts


def _is_single_brand_scope(render_data: Mapping[str, Any]) -> bool:
    """Return whether chart output must stay centered on the requested brand."""

    return render_data.get("answer_scope") in {"single_brand_trend", "single_brand_focus"}


def _is_single_brand_focus(render_data: Mapping[str, Any]) -> bool:
    """Return whether metric charts would distract from a focused brand answer."""

    return render_data.get("answer_scope") == "single_brand_focus"


def _hira_charts(calls: Sequence[Mapping[str, Any]], intent: set[str]) -> list[dict[str, Any]]:
    if "distribution" not in intent:
        return []
    gender_age: dict[str, float] = {}
    in_out: dict[str, float] = {}
    area: dict[str, float] = {}

    for call in calls:
        tool = str(call.get("tool", ""))
        render_data = as_mapping(call.get("render_data")) or {}
        rows = [as_mapping(item) or {} for item in render_data.get("items", []) if isinstance(item, Mapping)]
        if tool == "hira_disease_gender_age_stats":
            _add_gender_age_rows(rows, gender_age)
        elif tool == "hira_disease_hospitalization_outpatient_stats":
            _add_named_rows(rows, in_out, "inpatOpat")
        elif tool == "hira_disease_area_stats":
            _add_named_rows(rows, area, "lcName")

    charts: list[dict[str, Any]] = []
    if len(gender_age) >= 2:
        charts.append(bar_chart("성별·연령 환자 분포", gender_age, "HIRA gender/age stats", unit="명"))
    if len(in_out) >= 2:
        charts.append(doughnut_chart("입원/외래 환자 분포", in_out, "HIRA hospitalization/outpatient stats", unit="명"))
    if len(area) >= 2:
        charts.append(bar_chart("지역별 환자 분포", area, "HIRA area stats", unit="명"))
    return charts


def _chart_intent(question: str, answer: str) -> set[str]:
    text_value = f"{question} {answer}".lower()
    intents: set[str] = set()
    if any(token in text_value for token in ("추이", "시계열", "월별", "기간별", "트렌드", "trend", "변화", "증감", "대비")):
        intents.add("series")
    if "hhi" in text_value:
        intents.add("hhi")
    if any(token in text_value for token in ("상위", "랭킹", "순위", "비교", "브랜드별", "채널별")):
        intents.add("comparison")
    if any(token in text_value for token in ("경쟁", "위협", "오르는", "동안", "교차")):
        intents.add("comparison")
    if "시장" in text_value and any(token in text_value for token in ("영향", "고유", "하락", "동행", "같은 방향")):
        intents.add("market_vs_brand")
    if any(token in text_value for token in ("분포", "통계", "환자수", "지역별", "성별", "연령")):
        intents.add("distribution")
    return intents


def _brand_sales_series(render_data: Mapping[str, Any]) -> tuple[list[str], list[float | None]] | None:
    raw_series = render_data.get("brand_value_series_10pt")
    if not isinstance(raw_series, Sequence) or isinstance(raw_series, (str, bytes)):
        return None
    labels: list[str] = []
    values: list[float | None] = []
    for item in raw_series:
        row = as_mapping(item)
        if not row:
            return None
        label = text(row.get("period")) or text(row.get("year"))
        value = _first_number(row.get("value_krw"), row.get("value"))
        eok_value = number(row.get("value_억원"))
        if value is None and eok_value is not None:
            value = eok_value * 100_000_000
        if label is None:
            return None
        labels.append(label)
        values.append(value)
    if len(labels) < 2:
        return None
    return labels, values


def _and_particle(value: str) -> str:
    if not value:
        return "와"
    code = ord(value[-1])
    if 0xAC00 <= code <= 0xD7A3 and (code - 0xAC00) % 28:
        return "과"
    return "와"


def _aligned_series(
    left_labels: Sequence[str],
    left_values: Sequence[float | None],
    right_labels: Sequence[str],
    right_values: Sequence[float | None],
) -> tuple[list[str], list[float | None], list[float | None]] | None:
    right_by_label = {label: value for label, value in zip(right_labels, right_values, strict=False)}
    labels: list[str] = []
    left: list[float | None] = []
    right: list[float | None] = []
    for label, value in zip(left_labels, left_values, strict=False):
        if label not in right_by_label:
            continue
        labels.append(label)
        left.append(value)
        right.append(right_by_label[label])
    if len(labels) < 2:
        return None
    return labels, left, right


def _first_number(*values: Any) -> float | None:
    for value in values:
        parsed = number(value)
        if parsed is not None:
            return parsed
    return None


def _valid_charts(charts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(chart) for chart in charts if _chart_point_count(chart) >= 2]


def _chart_point_count(chart: Mapping[str, Any]) -> int:
    labels = chart.get("labels")
    if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes)):
        return 0
    label_count = len(labels)
    datasets = chart.get("datasets")
    if not isinstance(datasets, Sequence) or isinstance(datasets, (str, bytes)):
        return label_count
    data_counts: list[int] = []
    for dataset in datasets:
        dataset_map = as_mapping(dataset)
        data = dataset_map.get("data") if dataset_map else None
        if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
            data_counts.append(sum(number(value) is not None for value in data))
    if not data_counts:
        return label_count
    return min(label_count, max(data_counts))


def _add_gender_age_rows(rows: Sequence[Mapping[str, Any]], target: dict[str, float]) -> None:
    for row in rows:
        label = " ".join(filter(None, [text(row.get("sex")), text(row.get("age"))])) or "미분류"
        target[label] = target.get(label, 0.0) + (number(row.get("ptntCnt")) or 0.0)


def _add_named_rows(rows: Sequence[Mapping[str, Any]], target: dict[str, float], key: str) -> None:
    for row in rows:
        label = text(row.get(key)) or "미분류"
        target[label] = target.get(label, 0.0) + (number(row.get("ptntCnt")) or 0.0)


def _target_brand(result: Mapping[str, Any], calls: Sequence[Mapping[str, Any]]) -> str | None:
    resolution = result.get("resolution")
    if isinstance(resolution, Mapping):
        brand = text(resolution.get("canonical_brand"))
        if brand:
            return brand
    for call in calls:
        brand = text((as_mapping(call.get("render_data")) or {}).get("brand"))
        if brand:
            return brand
    return None
