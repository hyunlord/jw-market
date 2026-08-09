from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from jw_chat_agent_poc.orchestrator.markdown_formatting import (
    allowed_numbers,
    cell,
    eok_value,
    items,
    latest_series_eok,
    number_value,
    pct_value,
    rank_value,
    source_description,
    source_label,
    table,
)
from jw_chat_agent_poc.orchestrator.source_grading import grade_evidence_source
from jw_chat_agent_poc.orchestrator.surface_policy import can_surface_derived_value, cagr_operands_from_data, surface_year

LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    fact_id: str
    label: str
    value: str
    source: str
    tool: str
    path: str
    period: str
    allowed_numbers: tuple[str, ...]
    visible: bool = True
    entity: str = ""
    metric: str = ""
    unit: str = ""
    source_grade: str = ""
    view: str = ""
    market_id: str = ""
    operand_fact_ids: tuple[str, ...] = ()
    # Whether this fact came from a builder that can express market identity.
    #
    # An empty market_id means two different things. A market builder that
    # could have supplied one and did not has a real gap, and binding must go
    # on rejecting it. A fact projected from a tool envelope never had the
    # field at all -- tool_use.contracts.EvidenceFact forbids extras and
    # declares no market_id -- so rejecting it demands something its source
    # cannot ever provide.
    #
    # Default False, meaning "assumed unable to answer the market axis". Only
    # the market builders flip it, at the same call sites that pass market_id.
    market_scope_capable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NumberVerification:
    status: str
    unexpected_numbers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evidence_from_calls(calls: list[dict[str, Any]], data_md: str) -> tuple[EvidenceFact, ...]:
    facts: list[EvidenceFact] = []
    for call in calls:
        facts.extend(_structured_facts(call, len(facts)))
    for call in calls:
        facts.extend(_summary_text_facts(call, len(facts)))
    facts.extend(_table_token_facts(data_md, facts, len(facts)))
    return tuple(facts)


def project_hira_nedrug_binding_evidence(
    tool_calls: Sequence[Mapping[str, Any]],
    fact_md: str,
    *,
    canonical_hira_code: str | None = None,
) -> list[dict[str, Any]]:
    """Project exact HIRA counts and the MFDS result count for claim binding."""

    rendered_lines = frozenset(fact_md.splitlines())
    projected: list[dict[str, Any]] = []
    for call in tool_calls:
        if str(call.get("status") or "") != "ok":
            continue
        tool = str(call.get("tool") or "")
        facts = _tool_envelope_facts(call)
        if tool.startswith("hira_disease_") and tool.endswith("_stats"):
            for fact in facts:
                if (
                    fact.get("value") is None
                    or _render_serialized_evidence_claim(fact) not in rendered_lines
                ):
                    continue
                projected_fact = _hira_patient_binding_fact(
                    tool,
                    fact,
                    canonical_hira_code=canonical_hira_code,
                )
                if projected_fact is not None:
                    projected.append(projected_fact)
        elif tool == "mfds_permission_search":
            aggregate = _mfds_permission_count_binding_fact(tool, facts, rendered_lines)
            if aggregate is not None:
                projected.append(aggregate)
    return projected


def _tool_envelope_facts(call: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping) or render_data.get("ok") is False:
        return ()
    serialized = render_data.get("evidence")
    if not isinstance(serialized, Sequence) or isinstance(serialized, (str, bytes)):
        return ()
    facts: list[Mapping[str, Any]] = []
    for raw_fact in serialized:
        if not isinstance(raw_fact, Mapping):
            continue
        required = ("fact_id", "subject", "metric", "source_name")
        if any(not str(raw_fact.get(key) or "").strip() for key in required):
            continue
        facts.append(raw_fact)
    return tuple(facts)


def _render_serialized_evidence_claim(fact: Mapping[str, Any]) -> str:
    raw_value = fact.get("value")
    is_numeric = raw_value is not None
    value = str(raw_value) if is_numeric else str(fact.get("source_locator") or "확인됨")
    unit = str(fact.get("unit") or "") if is_numeric else ""
    period_value = str(fact.get("period") or "")
    period = f" ({period_value})" if period_value else ""
    locator_value = str(fact.get("source_locator") or "")
    locator = f" · {locator_value}" if is_numeric and locator_value else ""
    return (
        f"- {fact['subject']}{period}: {fact['metric']} = {value}{unit} "
        f"[{fact['source_name']}{locator}]"
    )


def _hira_patient_binding_fact(
    tool: str,
    fact: Mapping[str, Any],
    *,
    canonical_hira_code: str | None,
) -> dict[str, Any] | None:
    try:
        value = format(Decimal(str(fact["value"])), "f")
    except (InvalidOperation, ValueError):
        return None
    period = str(fact.get("period") or "")
    number_source = " ".join(part for part in (f"{value}명", period) if part)
    return {
        "fact_id": str(fact["fact_id"]),
        "label": "환자수",
        "value": f"{value}명",
        "source": str(fact["source_name"]),
        "tool": tool,
        "path": str(fact.get("raw_ref") or f"render_data.evidence.{fact['fact_id']}"),
        "period": period,
        "allowed_numbers": list(allowed_numbers(number_source)),
        "visible": True,
        "entity": canonical_hira_code or str(fact["subject"]),
        "metric": "환자수",
        "unit": "명",
        "source_grade": "AUTHORITATIVE",
        "view": "",
        "market_id": "",
        "operand_fact_ids": [],
    }


def _mfds_permission_count_binding_fact(
    tool: str,
    facts: tuple[Mapping[str, Any], ...],
    rendered_lines: frozenset[str],
) -> dict[str, Any] | None:
    item_facts = tuple(
        fact
        for fact in facts
        if fact.get("metric") == "허가 품목"
        and _render_serialized_evidence_claim(fact) in rendered_lines
    )
    if not item_facts:
        return None
    subjects = {str(fact["subject"]) for fact in item_facts}
    if len(subjects) != 1:
        return None
    count = len(item_facts)
    first = item_facts[0]
    return {
        "fact_id": f"{tool}:aggregate:count",
        "label": "허가 품목 수",
        "value": f"{count}건",
        "source": str(first["source_name"]),
        "tool": tool,
        "path": "render_data.evidence[metric=허가 품목]",
        "period": "현재 조회",
        "allowed_numbers": [f"{count}건"],
        "visible": True,
        "entity": str(first["subject"]),
        "metric": "허가 품목 수",
        "unit": "건",
        "source_grade": "AUTHORITATIVE",
        "view": "",
        "market_id": "",
        "operand_fact_ids": [],
    }


def evidence_markdown(facts: tuple[EvidenceFact, ...]) -> str:
    grouped: dict[str, set[str]] = {}
    for fact in facts:
        if not fact.visible:
            continue
        grouped.setdefault(fact.source, set()).add(fact.label)
    rows = tuple(
        (source, _source_provides(source, labels), ", ".join(sorted(labels)))
        for source, labels in sorted(grouped.items())
    )
    if not rows:
        return ""
    return table("## 근거", ("출처", "제공 내용", "주요 항목"), rows)


def verify_markdown_numbers(markdown: str, facts: tuple[EvidenceFact, ...]) -> NumberVerification:
    allowed = {token for fact in facts for token in fact.allowed_numbers}
    unexpected = tuple(sorted(token for token in number_tokens(markdown) if token not in allowed))
    return NumberVerification(status="pass" if not unexpected else "fail", unexpected_numbers=unexpected)


def number_tokens(markdown: str) -> tuple[str, ...]:
    return allowed_numbers(LINK_TARGET_RE.sub("]", markdown))


def interpretation_has_unverified_numbers(markdown: str, allowed: tuple[str, ...]) -> bool:
    allowed_set = set(allowed)
    return any(token not in allowed_set for token in number_tokens(markdown))


def verification_notice() -> str:
    return "숫자 검증: 근거 표에 없는 숫자 표현을 감지해 해석을 확정 데이터 기준으로 제한했습니다."


def _structured_facts(call: dict[str, Any], offset: int) -> list[EvidenceFact]:
    data = call.get("render_data")
    if not isinstance(data, dict):
        return []
    tool = str(call.get("tool") or "-")
    source = _fact_source(call, data)
    period = str(data.get("period") or "")
    values = _metric_values(data)
    facts: list[EvidenceFact] = []
    for label, value, path in values:
        if value:
            entity, metric, fact_period, unit, view, market_id = _metric_binding(
                data,
                label,
                period,
                path,
            )
            facts.append(
                _fact(
                    offset + len(facts),
                    label=label,
                    value=value,
                    source=source,
                    tool=tool,
                    path=path,
                    period=fact_period,
                    visible=True,
                    entity=entity,
                    metric=metric,
                    unit=unit,
                    source_grade=grade_evidence_source(tool=tool, source=source).value,
                    view=view,
                    market_id=market_id,
                    market_scope_capable=True,
                )
            )
    facts.extend(
        _brand_sales_series_facts(
            data,
            offset=offset + len(facts),
            source=source,
            tool=tool,
        )
    )
    facts.extend(
        _level_segment_rank_facts(
            data,
            offset=offset + len(facts),
            source=source,
            tool=tool,
        )
    )
    facts = _bind_derived_operands(facts)
    facts.extend(
        _hira_facts(
            data,
            offset=offset + len(facts),
            source=source,
            tool=tool,
        )
    )
    return facts


def _brand_sales_series_facts(
    data: dict[str, Any],
    *,
    offset: int,
    source: str,
    tool: str,
) -> list[EvidenceFact]:
    if data.get("measure") == "volume":
        return []
    entity = _brand_entity(data)
    if not entity:
        return []

    raw_series = data.get("brand_value_series_10pt")
    path_root = "render_data.brand_value_series_10pt"
    if not isinstance(raw_series, list) or not raw_series:
        raw_series = data.get("series")
        path_root = "render_data.series"

    rows: list[tuple[str, str, str]] = []
    if isinstance(raw_series, dict):
        for period, raw_value in raw_series.items():
            value = eok_value(None, raw_value)
            if value:
                rows.append((str(period), value, f"{path_root}[{period}]"))
    elif isinstance(raw_series, list):
        for index, row in enumerate(raw_series):
            if not isinstance(row, dict):
                continue
            period = str(row.get("period") or "")
            value = eok_value(row.get("value_억원"), row.get("value_krw"))
            if period and value:
                rows.append((period, value, f"{path_root}[{index}].value_krw"))

    view = str(data.get("view_type") or data.get("view") or "")
    market_id = str(data.get("market_id") or data.get("ml_id") or data.get("cd_id") or "")
    source_grade = grade_evidence_source(tool=tool, source=source).value
    return [
        _fact(
            offset + index,
            label="매출",
            value=value,
            source=source,
            tool=tool,
            path=path,
            period=period,
            visible=True,
            entity=entity,
            metric="매출",
            unit="억원",
            source_grade=source_grade,
            view=view,
            market_id=market_id,
            market_scope_capable=True,
        )
        for index, (period, value, path) in enumerate(rows)
    ]


def _level_segment_rank_facts(
    data: dict[str, Any],
    *,
    offset: int,
    source: str,
    tool: str,
) -> list[EvidenceFact]:
    segments = data.get("level_segments")
    if not isinstance(segments, list):
        return []

    entity = _metric_entity(data, "render_data.level_segments")
    period = str(data.get("period") or "")
    view = str(data.get("view_type") or data.get("view") or "")
    market_id = str(data.get("market_id") or data.get("ml_id") or data.get("cd_id") or "")
    source_grade = grade_evidence_source(tool=tool, source=source).value
    facts: list[EvidenceFact] = []
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        value = rank_value(segment.get("rank"), None)
        if not value:
            continue
        facts.append(
            _fact(
                offset + len(facts),
                label="순위",
                value=value,
                source=source,
                tool=tool,
                path=f"render_data.level_segments[{index}].rank",
                period=period,
                visible=True,
                entity=entity,
                metric="순위",
                unit="위",
                source_grade=source_grade,
                view=view,
                market_id=market_id,
                market_scope_capable=True,
            )
        )
    return facts


def _metric_values(data: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    is_volume = data.get("measure") == "volume"
    period = data.get("period")
    if isinstance(period, str) and period:
        rows.append(("기간", period, "render_data.period"))
    rows.extend(
        (
            ("매출", eok_value(data.get("sales_억원"), data.get("sales_krw")), "render_data.sales_krw"),
            (
                "처방량",
                _prescription_volume_value(data.get("prescription_volume")),
                "render_data.prescription_volume",
            ),
            (
                "시장규모",
                _market_size_value(data) or _latest_market_size(data),
                "render_data.market_size_recent_krw",
            ),
            (
                "필터 시장규모",
                eok_value(data.get("market_size_억원"), data.get("market_size_filtered_krw")),
                "render_data.market_size_filtered_krw",
            ),
            (
                "처방량 점유율" if is_volume else "시장점유율",
                pct_value(data.get("ms_recent_pct", data.get("market_share"))),
                "render_data.ms_recent_pct",
            ),
            ("순위", rank_value(data.get("rank"), data.get("total_brands_in_market")), "render_data.rank"),
            ("시장 구성 브랜드 수", number_value(data.get("total_brands_in_market")), "render_data.total_brands_in_market"),
            ("표시 브랜드 수", number_value(data.get("displayed_brand_count")), "render_data.displayed_brand_count"),
            ("브랜드 CAGR", _surfaceable_cagr_value(data, "brand_cagr_5y_pct"), "render_data.brand_cagr_5y_pct"),
            ("시장 CAGR", _surfaceable_cagr_value(data, "market_cagr_5y_pct"), "render_data.market_cagr_5y_pct"),
            ("Excess growth", _surfaceable_cagr_value(data, "excess_growth_pct"), "render_data.excess_growth_pct"),
            ("HHI", number_value(data.get("hhi_recent", data.get("hhi"))), "render_data.hhi_recent"),
            ("Momentum", number_value(data.get("momentum_score")), "render_data.momentum_score"),
            ("EI", number_value(data.get("ei")), "render_data.ei"),
            ("기준 점유율", pct_value(data.get("from_ms_pct")), "render_data.from_ms_pct"),
            ("비교 점유율", pct_value(data.get("to_ms_pct")), "render_data.to_ms_pct"),
            ("점유율 변화", pct_value(data.get("ms_delta_pct")), "render_data.ms_delta_pct"),
            ("기준 매출", eok_value(data.get("from_sales_억원"), data.get("from_sales_krw")), "render_data.from_sales_krw"),
            ("비교 매출", eok_value(data.get("to_sales_억원"), data.get("to_sales_krw")), "render_data.to_sales_krw"),
            ("매출 변화", eok_value(data.get("sales_delta_억원"), data.get("sales_delta_krw")), "render_data.sales_delta_krw"),
            ("매출 변화율", pct_value(data.get("sales_delta_pct")), "render_data.sales_delta_pct"),
        )
    )
    rows.extend(_series_insight_values(data.get("series_insight")))
    return rows


def _prescription_volume_value(value: Any) -> str:
    formatted = number_value(value)
    return f"{formatted} Rx" if formatted else ""


def _market_size_value(data: dict[str, Any]) -> str:
    value = eok_value(data.get("market_size_억원"), data.get("market_size_recent_krw"))
    market_id = str(data.get("market_id") or data.get("market") or "").lower()
    view = str(data.get("view_type") or data.get("view") or "").lower()
    if not (market_id.startswith("ml_") or "market_landscape" in view):
        return value

    raw = data.get("market_size_억원")
    divisor = Decimal("1")
    if raw is None:
        raw = data.get("market_size_recent_krw")
        divisor = Decimal("100000000")
    try:
        amount = Decimal(str(raw)) / divisor
    except (InvalidOperation, TypeError, ValueError):
        return value
    if not amount.is_finite():
        return value
    return f"{amount:,.6f}억원"


def _series_insight_values(raw: Any) -> list[tuple[str, str, str]]:
    if not isinstance(raw, dict):
        return []
    rows: list[tuple[str, str, str]] = []
    specs = (
        ("점유율 시작", "share_start_pct", "pct"),
        ("점유율 종료", "share_end_pct", "pct"),
        ("점유율 변화", "share_delta_pctp", "pctp"),
        ("매출 시작", "sales_start_krw", "eok"),
        ("매출 종료", "sales_end_krw", "eok"),
        ("매출 변화", "sales_delta_krw", "eok"),
        ("브랜드 성장률", "brand_growth_pct", "pct"),
        ("시장 성장률", "market_growth_pct", "pct"),
        ("초과성장", "excess_growth_pctp", "pctp"),
        ("브랜드 MoM", "brand_mom_pct", "pct"),
        ("시장 MoM", "market_mom_pct", "pct"),
        ("브랜드 YoY", "brand_yoy_pct", "pct"),
        ("시장 YoY", "market_yoy_pct", "pct"),
        ("브랜드 CMGR", "brand_cmgr_pct", "pct"),
        ("시장 CMGR", "market_cmgr_pct", "pct"),
        ("브랜드 CQGR", "brand_cqgr_pct", "pct"),
        ("시장 CQGR", "market_cqgr_pct", "pct"),
        ("최고 점유율", "share_max_pct", "pct"),
        ("최저 점유율", "share_min_pct", "pct"),
        ("HHI", "hhi_end", "num"),
        ("CR5", "cr5_end_pct", "pct"),
        ("분모", "denominator_end", "count"),
        ("추세 기간", "trend_months", "months"),
        ("시작 순위", "rank_start", "rank"),
        ("종료 순위", "rank_end", "rank"),
    )
    for label, key, kind in specs:
        value = raw.get(key)
        rendered = _insight_value(value, kind)
        if rendered:
            rows.append((label, rendered, f"render_data.series_insight.{key}"))
    for key in ("share_max_period", "share_min_period", "turning_point"):
        period = raw.get(key)
        if isinstance(period, str) and period:
            rows.append(("파생 기간", period, f"render_data.series_insight.{key}"))
    competitors = raw.get("competitors")
    if isinstance(competitors, list | tuple):
        for index, competitor in enumerate(competitors):
            if not isinstance(competitor, dict):
                continue
            for label, key, kind in (
                ("경쟁 브랜드 시작 점유율", "share_start_pct", "pct"),
                ("경쟁 브랜드 종료 점유율", "share_end_pct", "pct"),
                ("경쟁 브랜드 매출", "sales_end_krw", "eok"),
                ("경쟁 브랜드 순위", "rank_end", "rank"),
            ):
                rendered = _insight_value(competitor.get(key), kind)
                if rendered:
                    rows.append((label, rendered, f"render_data.series_insight.competitors[{index}].{key}"))
            for label, start_key, end_key, kind in (
                ("경쟁 브랜드 점유율 변화", "share_start_pct", "share_end_pct", "pctp"),
                ("경쟁 브랜드 매출 변화", "sales_start_krw", "sales_end_krw", "eok"),
            ):
                rendered = _derived_insight_delta(competitor.get(start_key), competitor.get(end_key), kind)
                if rendered:
                    rows.append(
                        (
                            label,
                            rendered,
                            f"render_data.series_insight.competitors[{index}].{end_key}-{start_key}",
                        )
                    )
    return rows


def _derived_insight_delta(start: Any, end: Any, kind: str) -> str:
    if not isinstance(start, int | float) or not isinstance(end, int | float):
        return ""
    start_value = float(start)
    end_value = float(end)
    if not isfinite(start_value) or not isfinite(end_value):
        return ""
    return _insight_value(end_value - start_value, kind)


def _insight_value(value: Any, kind: str) -> str:
    if not isinstance(value, int | float):
        return ""
    if kind == "pct":
        return pct_value(value)
    if kind == "pctp":
        return f"{float(value):.2f}%p"
    if kind == "eok":
        return eok_value(None, abs(float(value)))
    if kind == "count":
        return f"{int(value)}개"
    if kind == "months":
        return f"{int(value)}개월"
    if kind == "rank":
        return f"{int(value)}위"
    return f"{float(value):.2f}"


def _surfaceable_cagr_value(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if can_surface_derived_value(value, cagr_operands=cagr_operands_from_data(data, key)):
        return pct_value(value)
    return ""


def _hira_facts(
    data: dict[str, Any],
    *,
    offset: int,
    source: str,
    tool: str,
) -> list[EvidenceFact]:
    source_grade = grade_evidence_source(tool=tool, source=source).value
    calls = data.get("calls")
    if not isinstance(calls, list):
        calls = [{"render_data": data}] if items(data) else []
    facts: list[EvidenceFact] = []
    for call_index, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        for item_index, item in enumerate(items(render_data)):
            count = item.get("ptntCnt")
            year = surface_year(render_data, item)
            code = str(item.get("sickCd") or render_data.get("sickCd") or "").strip().upper()
            name = str(item.get("sickNm") or render_data.get("sickNm") or "").strip()
            entity = code or name
            if can_surface_derived_value(count, required_period=year):
                facts.append(
                    _fact(
                        offset + len(facts),
                        label="환자수",
                        value=f"{year}년 {count}명",
                        source=source,
                        tool=tool,
                        path=f"render_data.calls[{call_index}].render_data.items[{item_index}].ptntCnt",
                        period=str(year),
                        visible=True,
                        entity=entity,
                        metric="환자수",
                        unit="명",
                        source_grade=source_grade,
                    )
                )
            if code:
                facts.append(
                    _fact(
                        offset + len(facts),
                        label="질병코드",
                        value=code,
                        source=source,
                        tool=tool,
                        path=f"render_data.calls[{call_index}].render_data.items[{item_index}].sickCd",
                        period=str(year or ""),
                        visible=True,
                        entity=entity,
                        metric="질병코드",
                        unit="code",
                        source_grade=source_grade,
                    )
                )
    return facts


def _metric_binding(
    data: dict[str, Any],
    label: str,
    default_period: str,
    path: str,
) -> tuple[str, str, str, str, str, str]:
    entity = _metric_entity(data, path)
    metric = {
        "기간": "기간",
        "매출": "매출",
        "시장규모": "시장규모",
        "필터 시장규모": "시장규모",
        "시장점유율": "시장점유율",
        "순위": "순위",
        "브랜드 CAGR": "CAGR",
        "시장 CAGR": "CAGR",
        "Excess growth": "초과성장",
        "HHI": "HHI",
        "CR5": "CR5",
        "Momentum": "Momentum",
        "EI": "EI",
        "기준 점유율": "시장점유율",
        "비교 점유율": "시장점유율",
        "점유율 변화": "점유율 변화",
        "기준 매출": "매출",
        "비교 매출": "매출",
        "매출 변화": "매출 변화",
        "매출 변화율": "매출 변화율",
        "점유율 시작": "시장점유율",
        "점유율 종료": "시장점유율",
        "최고 점유율": "시장점유율",
        "최저 점유율": "시장점유율",
        "경쟁 브랜드 시작 점유율": "시장점유율",
        "경쟁 브랜드 종료 점유율": "시장점유율",
        "경쟁 브랜드 점유율 변화": "점유율 변화",
        "매출 시작": "매출",
        "매출 종료": "매출",
        "경쟁 브랜드 매출": "매출",
        "경쟁 브랜드 매출 변화": "매출 변화",
        "브랜드 성장률": "매출 변화율",
        "시장 성장률": "매출 변화율",
        "브랜드 MoM": "매출 변화율",
        "시장 MoM": "매출 변화율",
        "브랜드 YoY": "매출 변화율",
        "시장 YoY": "매출 변화율",
        "브랜드 CMGR": "매출 변화율",
        "시장 CMGR": "매출 변화율",
        "브랜드 CQGR": "매출 변화율",
        "시장 CQGR": "매출 변화율",
        "시작 순위": "순위",
        "종료 순위": "순위",
        "경쟁 브랜드 순위": "순위",
    }.get(label, label)
    unit = _metric_unit(metric)
    view = str(data.get("view_type") or data.get("view") or "")
    market_id = str(data.get("market_id") or data.get("ml_id") or data.get("cd_id") or "")
    return entity, metric, _metric_period(default_period, path), unit, view, market_id


def _metric_period(default_period: str, path: str) -> str:
    periods = re.findall(r"20\d{2}-(?:0[1-9]|1[0-2]|Q[1-4])", default_period, re.IGNORECASE)
    if len(periods) < 2:
        return default_period
    if any(token in path for token in ("from_", "_start", "share_start", "sales_start", "rank_start")):
        return periods[0].upper()
    if any(token in path for token in ("to_", "_end", "share_end", "sales_end", "rank_end")):
        return periods[-1].upper()
    return default_period


def _metric_entity(data: dict[str, Any], path: str) -> str:
    competitor_match = re.search(r"series_insight\.competitors\[(\d+)\]", path)
    if competitor_match:
        series_insight = data.get("series_insight")
        competitors = series_insight.get("competitors") if isinstance(series_insight, dict) else None
        index = int(competitor_match.group(1))
        if isinstance(competitors, list | tuple) and index < len(competitors):
            competitor = competitors[index]
            if isinstance(competitor, dict):
                for key in ("brand_key", "brand_name", "brand"):
                    value = competitor.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
    for key in (
        "brand_key",
        "brand_name",
        "brand",
        "market_id",
        "market",
        "atc4_code",
        "ml_id",
        "cd_id",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _brand_entity(data: dict[str, Any]) -> str:
    for key in ("brand_key", "brand_name", "brand", "anchor_brand"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _metric_unit(metric: str) -> str:
    if metric in {"시장 구성 브랜드 수", "표시 브랜드 수", "분모"}:
        return "개"
    if metric in {"점유율 변화", "초과성장"}:
        return "%p"
    if metric == "CR5":
        return "%"
    if "점유율" in metric or "CAGR" in metric or "growth" in metric.lower() or "변화율" in metric:
        return "%"
    if "매출" in metric or "시장규모" in metric:
        return "억원"
    if "순위" in metric:
        return "위"
    if metric == "HHI":
        return "index"
    if metric in {"Momentum", "EI"}:
        return "score"
    if metric == "기간":
        return "period"
    return ""


def _bind_derived_operands(facts: list[EvidenceFact]) -> list[EvidenceFact]:
    by_path = {fact.path: fact for fact in facts}
    bound: list[EvidenceFact] = []
    for fact in facts:
        operand_paths = _derived_operand_paths(fact.path)
        if not operand_paths:
            bound.append(fact)
            continue
        operand_ids = tuple(
            by_path[path].fact_id if path in by_path else f"missing:{path}"
            for path in operand_paths
        )
        bound.append(replace(fact, operand_fact_ids=operand_ids))
    return bound


def _derived_operand_paths(path: str) -> tuple[str, ...]:
    exact = {
        "render_data.ms_delta_pct": (
            "render_data.from_ms_pct",
            "render_data.to_ms_pct",
        ),
        "render_data.sales_delta_krw": (
            "render_data.from_sales_krw",
            "render_data.to_sales_krw",
        ),
        "render_data.sales_delta_pct": (
            "render_data.from_sales_krw",
            "render_data.to_sales_krw",
        ),
        "render_data.excess_growth_pct": (
            "render_data.brand_cagr_5y_pct",
            "render_data.market_cagr_5y_pct",
        ),
        "render_data.series_insight.share_delta_pctp": (
            "render_data.series_insight.share_start_pct",
            "render_data.series_insight.share_end_pct",
        ),
        "render_data.series_insight.sales_delta_krw": (
            "render_data.series_insight.sales_start_krw",
            "render_data.series_insight.sales_end_krw",
        ),
        "render_data.series_insight.excess_growth_pctp": (
            "render_data.series_insight.brand_growth_pct",
            "render_data.series_insight.market_growth_pct",
        ),
    }
    if path in exact:
        return exact[path]
    match = re.fullmatch(
        r"(render_data\.series_insight\.competitors\[\d+\])\.(share|sales)_end_(pct|krw)-(?:share|sales)_start_(?:pct|krw)",
        path,
    )
    if not match:
        return ()
    prefix, metric, suffix = match.groups()
    return (
        f"{prefix}.{metric}_start_{suffix}",
        f"{prefix}.{metric}_end_{suffix}",
    )


def _latest_market_size(data: dict[str, Any]) -> str:
    market_series = data.get("market_size_series")
    if isinstance(market_series, list) and market_series:
        latest = market_series[-1]
        if isinstance(latest, dict):
            return eok_value(latest.get("value_억원"), latest.get("value_krw"))
    if _brand_entity(data):
        return ""
    return latest_series_eok(data.get("series"))


def _summary_text_facts(call: dict[str, Any], offset: int) -> list[EvidenceFact]:
    summary = call.get("summary_text")
    if not isinstance(summary, str):
        return []
    tokens = number_tokens(summary)
    if not tokens:
        return []
    return [
        _fact(
            offset + index,
            label="도구 요약 숫자",
            value=token,
            source=_fact_source(call, {}),
            tool=str(call.get("tool") or "-"),
            path="summary_text",
            period="",
            visible=False,
        )
        for index, token in enumerate(tokens)
    ]


def _table_token_facts(data_md: str, facts: list[EvidenceFact], offset: int) -> list[EvidenceFact]:
    known = {token for fact in facts for token in fact.allowed_numbers}
    missing = [token for token in number_tokens(data_md) if token not in known]
    return [
        _fact(
            offset + index,
            label="표 숫자",
            value=token,
            source="렌더링된 도구 표",
            tool="markdown_table",
            path="data_md",
            period="",
            visible=False,
        )
        for index, token in enumerate(missing)
    ]


def _fact(
    index: int,
    *,
    label: str,
    value: str,
    source: str,
    tool: str,
    path: str,
    period: str,
    visible: bool,
    entity: str = "",
    metric: str = "",
    unit: str = "",
    source_grade: str = "",
    view: str = "",
    market_id: str = "",
    operand_fact_ids: tuple[str, ...] = (),
    market_scope_capable: bool = False,
) -> EvidenceFact:
    allowed = set(number_tokens(value))
    allowed.update(number_tokens(label))
    allowed.update(_period_display_tokens(value))
    if label == "환자수":
        for count in re.findall(r"(?<!\d)(\d[\d,]*)(?=명|\s|$)", value):
            allowed.update(number_tokens(count))
            allowed.update(number_tokens(f"{count}명"))
    if label in {"매출 변화", "매출 변화율", "점유율 변화"} and value and not value.startswith(("+", "-")):
        allowed.update(number_tokens(f"+{value}"))
    if label in {"매출 변화", "매출 변화율", "점유율 변화"} and value.startswith("-"):
        allowed.update(number_tokens(value[1:]))
    if label == "점유율 변화" and value.endswith("%"):
        allowed.update(number_tokens(f"{value}p"))
        if value.startswith("-"):
            allowed.update(number_tokens(f"{value[1:]}p"))
        elif not value.startswith("+"):
            allowed.update(number_tokens(f"+{value}p"))
    if label == "순위":
        allowed.update(_rank_display_tokens(value))
    return EvidenceFact(
        fact_id=f"fact_{_letters(index)}",
        label=cell(label),
        value=cell(value),
        source=cell(source),
        tool=cell(tool),
        path=cell(path),
        period=cell(period),
        allowed_numbers=tuple(sorted(allowed)),
        visible=visible,
        entity=str(entity).strip(),
        metric=str(metric).strip(),
        unit=str(unit).strip(),
        source_grade=str(source_grade).strip(),
        view=str(view).strip(),
        market_id=str(market_id).strip(),
        operand_fact_ids=operand_fact_ids,
        market_scope_capable=market_scope_capable,
    )


def _period_display_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for year, month in re.findall(r"(20\d{2})-(\d{2})", value):
        tokens.update(number_tokens(f"{year}년"))
        tokens.update(number_tokens(f"{int(month)}월"))
        tokens.update(number_tokens(f"{month}월"))
    return tokens


def _rank_display_tokens(value: str) -> set[str]:
    match = re.fullmatch(r"(\d+)/(\d+)", value)
    if not match:
        return set()
    rank, total = match.groups()
    tokens = set(number_tokens(f"{rank}위"))
    tokens.update(number_tokens(f"{total}개"))
    return tokens


def _letters(index: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    current = index
    chars: list[str] = []
    while True:
        current, remainder = divmod(current, len(alphabet))
        chars.append(alphabet[remainder])
        if current == 0:
            break
        current -= 1
    return "".join(reversed(chars))


def _fact_source(call: dict[str, Any], data: dict[str, Any]) -> str:
    data_source_label = data.get("source_label")
    if isinstance(data_source_label, str) and data_source_label:
        return source_label(data_source_label)
    source = call.get("source")
    return source_label(str(source or "tool_result"))


def _source_provides(source: str, labels: set[str]) -> str:
    if source == "UBIST" and any("처방량" in label for label in labels):
        return "처방량·처방량 점유율·순위 등 UBIST 운영 지표"
    return source_description(source)
