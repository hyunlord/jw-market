from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    MarketMetricFact,
    V3EvidenceBundle,
)
from jw_chat_agent_poc.tool_use.v3_fusion_evidence import (
    canonical_numeric_literal,
    display_numeric_literals,
    fact_numeric_literals,
)
from jw_chat_agent_poc.tool_use.v3_selection import MultiToolChoice


_SUPPORTED_CHART_TYPES = frozenset({"line", "bar", "doughnut"})
_VIEW_TOOLS = (
    "market.get_brand_metric",
    "market.get_hhi",
    "market.get_growth_contribution",
    "market.get_channel_breakdown",
)
_VIEW_LIMITATION_TOOLS = (
    ("시장 규모 및 성장률 추이", "market.get_brand_metric"),
    ("HHI 추이", "market.get_hhi"),
    ("브랜드 순위", "market.get_brand_metric"),
    ("대상 브랜드 매출·점유율·순위", "market.get_brand_metric"),
    ("시장 성장 기여도", "market.get_growth_contribution"),
    ("채널별 구성", "market.get_channel_breakdown"),
)


@dataclass(frozen=True, slots=True)
class ScopeViewSet:
    attached: bool
    markdown: str = ""
    charts: tuple[Mapping[str, object], ...] = ()
    limitations: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    view_names: tuple[str, ...] = ()


def scope_view_choices(
    selected: Sequence[MultiToolChoice],
    *,
    scope_confirmed: bool,
) -> tuple[MultiToolChoice, ...]:
    """Derive view calls only after the existing scope resolver confirms an anchor."""

    if not scope_confirmed:
        return ()
    anchor = _scope_anchor(selected)
    if anchor is None:
        return ()
    brand, common = anchor
    choices: list[MultiToolChoice] = []
    for index, tool_name in enumerate(_VIEW_TOOLS):
        arguments = {"brand": brand, **common}
        if tool_name == "market.get_brand_metric":
            arguments.update(metric="sales", period="latest", history_points=60)
        elif tool_name == "market.get_hhi":
            arguments.update(period="latest", history_points=60)
        elif tool_name == "market.get_growth_contribution":
            arguments.update(period="latest", history_points=60)
        else:
            arguments.update(period="latest", limit=10, metric="sales")
        choices.append(
            MultiToolChoice(
                name=tool_name,
                arguments=arguments,
                call_id=f"scope-view-{index + 1}",
            )
        )
    return tuple(choices)


def merge_evidence_bundles(
    primary: V3EvidenceBundle,
    supplemental: V3EvidenceBundle,
) -> V3EvidenceBundle:
    facts = (*primary.facts, *supplemental.facts)
    failures = (*primary.failures, *supplemental.failures)
    deferred = (*primary.deferred, *supplemental.deferred)
    status = (
        "partial"
        if facts and (failures or deferred)
        else "complete"
        if facts
        else "failed"
        if failures
        else "no_selection"
    )
    return V3EvidenceBundle(
        status=status,
        facts=facts,
        failures=failures,
        deferred=deferred,
        executions=(*primary.executions, *supplemental.executions),
        original_call_count=primary.original_call_count + supplemental.original_call_count,
        executed_call_count=primary.executed_call_count + supplemental.executed_call_count,
        deduplicated_call_count=(
            primary.deduplicated_call_count + supplemental.deduplicated_call_count
        ),
    )


def build_scope_view_set(
    bundle: V3EvidenceBundle,
    *,
    scope_confirmed: bool,
    chart_numeric_override: float | None = None,
) -> ScopeViewSet:
    if not scope_confirmed:
        return ScopeViewSet(attached=False)
    facts = tuple(fact for fact in bundle.facts if isinstance(fact, MarketMetricFact))
    if not facts:
        return ScopeViewSet(
            attached=False,
            limitations=_view_limitations(bundle, (), ()),
        )

    sections: list[tuple[str, str, str]] = []
    charts: list[Mapping[str, object]] = []
    used_ids: list[str] = []

    series_fact = _first_fact_with(facts, "market_size_series") or _fact_with_table(
        facts, "시장 규모 및 성장 추이"
    )
    if series_fact is not None:
        raw = _render_data(series_fact)
        size_rows = _mapping_rows(raw.get("market_size_series"))
        if not size_rows:
            size_rows = _market_size_rows_from_table(raw)
        growth = {
            str(row.get("period")): row.get("yoy_growth_pct", row.get("growth_pct"))
            for row in _mapping_rows(
                raw.get("market_growth_series", raw.get("market_yoy_series"))
            )
            if row.get("period") is not None
        }
        if not growth:
            growth = {
                str(row.get("period")): row.get("yoy_growth_pct")
                for row in size_rows
                if row.get("period") is not None
                and row.get("yoy_growth_pct") is not None
            }
        has_growth = bool(growth)
        rows = [
            tuple(
                item
                for item in (
                    row.get("period"),
                    _display_or_none(series_fact, row.get("value"), "market_size_series"),
                    (
                        _display_or_none(
                            series_fact,
                            growth.get(str(row.get("period"))),
                            "growth",
                        )
                        if has_growth and growth.get(str(row.get("period"))) is not None
                        else None
                    ),
                )
                if item is not None
            )
            for row in size_rows
            if row.get("period") is not None and row.get("value") is not None
        ]
        expected_width = 4 if size_rows and size_rows[0].get("unit") else 3 if has_growth else 2
        rows = [
            (*row, source.get("unit")) if source.get("unit") else row
            for row, source in zip(rows, size_rows, strict=True)
        ]
        rows = [
            row
            for row in rows
            if len(row) == expected_width and all(item is not None for item in row)
        ]
        if rows:
            period = str(rows[-1][0])
            sections.append(
                (
                    "시장 규모 및 성장률 추이",
                    period,
                    _table(
                        ("기간", "시장 규모", "성장률(%)", "단위")
                        if expected_width == 4
                        else ("기간", "시장 규모", "성장률(%)")
                        if expected_width == 3
                        else ("기간", "시장 규모"),
                        rows,
                    ),
                )
            )
            chart_values = [row.get("value") for row in size_rows]
            if chart_numeric_override is not None:
                chart_values[-1] = chart_numeric_override
            charts.append(
                _chart(
                    "line",
                    "시장 규모 추이",
                    [row.get("period") for row in size_rows],
                    "시장 규모",
                    chart_values,
                    series_fact,
                )
            )
            used_ids.append(series_fact.evidence_id)

    hhi_fact = _first_fact_with(facts, "hhi_series_5y")
    if hhi_fact is not None:
        hhi_rows = _mapping_rows(_render_data(hhi_fact).get("hhi_series_5y"))
        rows = [
            (
                row.get("period"),
                _display_or_none(hhi_fact, row.get("hhi", row.get("value")), "hhi"),
            )
            for row in hhi_rows
            if row.get("period") is not None
            and row.get("hhi", row.get("value")) is not None
        ]
        if rows:
            sections.append(("HHI 추이", str(rows[-1][0]), _table(("기간", "HHI"), rows)))
            chart_values = [row.get("hhi", row.get("value")) for row in hhi_rows]
            if chart_numeric_override is not None:
                chart_values[-1] = chart_numeric_override
            charts.append(
                _chart(
                    "line",
                    "HHI 추이",
                    [row.get("period") for row in hhi_rows],
                    "HHI",
                    chart_values,
                    hhi_fact,
                )
            )
            used_ids.append(hhi_fact.evidence_id)

    ranking_fact = _first_fact_with(facts, "brand_ranking_stacked")
    if ranking_fact is not None:
        ranking_rows = _ranking_rows(
            _render_data(ranking_fact).get("brand_ranking_stacked")
        )
        if ranking_rows:
            latest = str(ranking_rows[-1].get("period"))
            latest_rows = [row for row in ranking_rows if str(row.get("period")) == latest]
            rows = [
                (
                    row.get("rank"),
                    row.get("brand", row.get("brand_name")),
                    _display_or_none(
                        ranking_fact,
                        row.get("ms", row.get("share_pct")),
                        "share",
                    ),
                )
                for row in sorted(latest_rows, key=lambda row: int(row.get("rank") or 999))
                if row.get("brand", row.get("brand_name")) and row.get("rank") is not None
            ]
            if rows:
                sections.append(
                    (
                        "브랜드 순위",
                        latest,
                        _table(("순위", "브랜드", "점유율(%)"), rows),
                    )
                )
                charts.append(
                    _chart(
                        "bar",
                        "상위 브랜드 점유율",
                        [row.get("brand", row.get("brand_name")) for row in latest_rows],
                        "점유율",
                        [row.get("ms", row.get("share_pct")) for row in latest_rows],
                        ranking_fact,
                    )
                )
                used_ids.append(ranking_fact.evidence_id)

    brand_fact = _first_fact_with(facts, "brand")
    if brand_fact is not None:
        raw = _render_data(brand_fact)
        brand = _text(raw.get("brand"))
        sales = _first(raw, "brand_sales_krw", "target_brand_sales", "value")
        share = _first(raw, "target_share_pct", "market_share", "ms_recent_pct")
        rank = _first(raw, "target_rank", "rank")
        if brand and sales is not None and share is not None and rank is not None:
            displayed_sales = _display_or_none(brand_fact, sales, "sales")
            displayed_share = _display_or_none(
                brand_fact, share, "target_share_pct"
            )
            displayed_rank = _display_or_none(brand_fact, rank, "rank")
            if None in (displayed_sales, displayed_share, displayed_rank):
                displayed_sales = displayed_share = displayed_rank = None
            period = _text(raw.get("period")) or _text(brand_fact.period)
            if displayed_sales is not None:
                sections.append(
                    (
                        f"{brand} 매출·점유율·순위",
                        period,
                        _table(
                            ("브랜드", "매출", "점유율(%)", "순위"),
                            ((brand, displayed_sales, displayed_share, displayed_rank),),
                        ),
                    )
                )
                used_ids.append(brand_fact.evidence_id)

    growth_fact = _growth_fact(facts)
    if growth_fact is not None:
        raw = _render_data(growth_fact)
        value = raw.get("value")
        growth = value if isinstance(value, Mapping) else raw.get("growth_contribution")
        if isinstance(growth, Mapping):
            rows = [
                (label, rendered)
                for key, label in (
                    ("market_growth_pct", "시장 성장률(%)"),
                    ("growth_contribution_pct", "시장 성장 기여도(%p)"),
                    ("contribution_pct", "시장 성장 기여도(%)"),
                )
                if growth.get(key) is not None
                for rendered in (_display_or_none(growth_fact, growth.get(key), key),)
                if rendered is not None
            ]
            if rows:
                period = _text(raw.get("period")) or _text(growth_fact.period)
                sections.append(
                    ("시장 성장 기여도", period, _table(("지표", "값"), rows))
                )
                used_ids.append(growth_fact.evidence_id)

    channel_fact = _first_fact_with(facts, "target_customer_competition_by_channel")
    if channel_fact is not None:
        channel_rows = _channel_rows(
            _render_data(channel_fact).get("target_customer_competition_by_channel")
        )
        if channel_rows:
            latest = str(channel_rows[-1][1])
            latest_rows = [row for row in channel_rows if str(row[1]) == latest]
            rows = [
                (
                    row[0],
                    row[2],
                    _display_or_none(channel_fact, row[3], "value_series"),
                    _display_or_none(channel_fact, row[4], "ms_series"),
                    _display_or_none(channel_fact, row[5], "rank_series"),
                )
                for row in latest_rows
                if all(item is not None for item in (row[0], row[2], row[3], row[4], row[5]))
            ]
            if rows:
                sections.append(
                    (
                        "채널별 구성",
                        latest,
                        _table(("채널", "브랜드", "값", "점유율(%)", "순위"), rows),
                    )
                )
                charts.append(
                    _chart(
                        "bar",
                        "채널별 구성",
                        [str(row[0]) for row in latest_rows],
                        "값",
                        [row[3] for row in latest_rows],
                        channel_fact,
                    )
                )
                used_ids.append(channel_fact.evidence_id)

    if not sections:
        unrendered = tuple(
            name
            for name, fact in (
                ("시장 규모 및 성장률 추이", series_fact),
                ("HHI 추이", hhi_fact),
                ("브랜드 순위", ranking_fact),
                ("대상 브랜드 매출·점유율·순위", brand_fact),
                ("시장 성장 기여도", growth_fact),
                ("채널별 구성", channel_fact),
            )
            if fact is not None
        )
        return ScopeViewSet(
            attached=False,
            limitations=_view_limitations(bundle, (), unrendered),
        )
    candidate_chart_count = len(charts)
    charts = [chart for chart in charts if chart["type"] in _SUPPORTED_CHART_TYPES]
    charts = list(_grounded_charts(charts, facts))

    view_names = tuple(section[0] for section in sections)
    unrendered_names = {
        name
        for name, fact in (
            ("시장 규모 및 성장률 추이", series_fact),
            ("HHI 추이", hhi_fact),
            ("브랜드 순위", ranking_fact),
            ("시장 성장 기여도", growth_fact),
            ("채널별 구성", channel_fact),
        )
        if fact is not None and name not in view_names
    }
    limitations = _view_limitations(bundle, view_names, unrendered_names)
    if len(charts) != candidate_chart_count:
        limitations = (*limitations, "근거와 결속되지 않은 차트는 제외했습니다.")
    markdown = "---\n\n## 시장 기본 뷰\n\n" + "\n\n".join(
        f"### {name}\n\n기준 기간: {period}\n\n{table}"
        for name, period, table in sections
    )
    return ScopeViewSet(
        attached=True,
        markdown=markdown,
        charts=tuple(charts),
        limitations=limitations,
        evidence_ids=tuple(dict.fromkeys(used_ids)),
        view_names=view_names,
    )


def _view_limitations(
    bundle: V3EvidenceBundle,
    rendered_names: Collection[str],
    unrendered_names: Collection[str],
) -> tuple[str, ...]:
    return tuple(
        f"{name} 데이터는 확인하지 못했습니다."
        for name, tool in _VIEW_LIMITATION_TOOLS
        if name not in rendered_names
        and (
            any(failure.tool_name == tool for failure in bundle.failures)
            or name in unrendered_names
        )
    )
def _scope_anchor(
    selected: Sequence[MultiToolChoice],
) -> tuple[str, dict[str, object]] | None:
    for choice in selected:
        if not choice.name.startswith("market."):
            continue
        brand = _text(choice.arguments.get("brand"))
        if not brand:
            continue
        common: dict[str, object] = {}
        market = _text(choice.arguments.get("market")) or _text(
            choice.arguments.get("market_id")
        )
        if market:
            common["market"] = market
        source = _text(choice.arguments.get("source"))
        if source:
            common["source"] = source
        scope = choice.arguments.get("scope")
        if isinstance(scope, Mapping):
            common["scope"] = dict(scope)
        view = _text(choice.arguments.get("view"))
        if view in {"general", "strategic"}:
            common["view"] = view
        elif view in {"market_landscape", "competitive_dynamics"}:
            common["view"] = "strategic"
        return brand, common
    return None


def _render_data(fact: MarketMetricFact) -> Mapping[str, object]:
    if not isinstance(fact.raw_result, Mapping):
        return {}
    value = fact.raw_result.get("render_data")
    return value if isinstance(value, Mapping) else fact.raw_result


def _first_fact_with(
    facts: Sequence[MarketMetricFact], key: str
) -> MarketMetricFact | None:
    return next((fact for fact in facts if _render_data(fact).get(key) is not None), None)


def _fact_with_table(
    facts: Sequence[MarketMetricFact], name: str
) -> MarketMetricFact | None:
    return next(
        (
            fact
            for fact in facts
            if any(table.get("name") == name for table in _dashboard_tables(_render_data(fact)))
        ),
        None,
    )


def _dashboard_tables(raw: Mapping[str, object]) -> list[Mapping[str, object]]:
    return _mapping_rows(raw.get("dashboard_tables"))


def _market_size_rows_from_table(
    raw: Mapping[str, object],
) -> list[Mapping[str, object]]:
    table = next(
        (
            item
            for item in _dashboard_tables(raw)
            if item.get("name") == "시장 규모 및 성장 추이"
        ),
        None,
    )
    if table is None:
        return []
    rows = table.get("rows")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return []
    projected: list[Mapping[str, object]] = []
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes, bytearray)):
            continue
        values = list(row)
        if len(values) < 2:
            continue
        projected.append(
            {
                "period": values[0],
                "value": values[1],
                "yoy_growth_pct": values[2] if len(values) > 2 else None,
                "unit": values[3] if len(values) > 3 else None,
            }
        )
    return projected


def _growth_fact(facts: Sequence[MarketMetricFact]) -> MarketMetricFact | None:
    return next(
        (
            fact
            for fact in facts
            if fact.tool_name == "market.get_growth_contribution"
            or _render_data(fact).get("growth_contribution") is not None
        ),
        None,
    )


def _mapping_rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _ranking_rows(value: object) -> list[Mapping[str, object]]:
    direct = _mapping_rows(value)
    if direct:
        return direct
    if not isinstance(value, Mapping):
        return []
    rows: list[Mapping[str, object]] = []
    for yearly in _mapping_rows(value.get("yearly")):
        period = yearly.get("period", yearly.get("year"))
        for ranking in _mapping_rows(yearly.get("rankings")):
            rows.append({"period": period, **ranking})
    return rows


def _channel_rows(value: object) -> list[tuple[object, ...]]:
    if not isinstance(value, Mapping):
        return []
    rows: list[tuple[object, ...]] = []
    for view in _mapping_rows(value.get("views")):
        periods = list(view.get("periods") or ())
        for brand in _mapping_rows(view.get("trend_brands")):
            values = list(brand.get("value_series") or ())
            shares = list(brand.get("ms_series") or ())
            ranks = list(brand.get("rank_series") or ())
            for index, period in enumerate(periods):
                rows.append(
                    (
                        view.get("target_name"),
                        period,
                        brand.get("brand"),
                        values[index] if index < len(values) else None,
                        shares[index] if index < len(shares) else None,
                        ranks[index] if index < len(ranks) else None,
                    )
                )
    return rows


def _display(fact: MarketMetricFact, value: object, semantic: str) -> str:
    if value is None:
        raise LookupError(f"missing display value for {semantic}")
    canonical = _canonical_number(value)
    matching = [
        item
        for item in display_numeric_literals(fact)
        if canonical_numeric_literal(str(item["raw_value"])) == canonical
        and semantic.casefold() in str(item["field_path"]).casefold()
    ]
    if matching:
        rendered = str(matching[0]["display_value"])
        if not _numeric_literal_is_grounded(fact, rendered):
            raise LookupError(f"ungrounded display value for {semantic}")
        return rendered
    if not _numeric_literal_is_grounded(fact, canonical):
        raise LookupError(f"ungrounded display value for {semantic}")
    digits = _semantic_digits(semantic)
    if digits is None:
        return str(value)
    quantizer = Decimal(1).scaleb(-digits)
    return format(
        Decimal(str(value)).quantize(quantizer, rounding=ROUND_HALF_UP),
        f".{digits}f",
    )


def _semantic_digits(semantic: str) -> int | None:
    normalized = semantic.casefold()
    if "hhi" in normalized:
        return 4
    if normalized == "target_share_pct" or "cagr" in normalized:
        return 2
    if any(
        token in normalized
        for token in ("growth", "contribution", "share", "yoy", "mom", "qoq", "ms_")
    ):
        return 1
    return None


def _display_or_none(
    fact: MarketMetricFact, value: object, semantic: str
) -> str | None:
    try:
        return _display(fact, value, semantic)
    except LookupError:
        return None


def _numeric_literal_is_grounded(fact: MarketMetricFact, value: object) -> bool:
    canonical = _canonical_number(value)
    return any(_canonical_number(item) == canonical for item in fact_numeric_literals(fact))


def _canonical_number(value: object) -> str:
    try:
        normalized = format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)
    return "0" if normalized in {"-0", ""} else normalized


def _table(columns: Sequence[object], rows: Sequence[Sequence[object]]) -> str:
    safe_rows = [row for row in rows if all(item is not None and str(item) != "" for item in row)]
    if not safe_rows:
        raise LookupError("table has no complete rows")
    header = "| " + " | ".join(_escape_cell(item) for item in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_escape_cell(item) for item in row) + " |" for row in safe_rows]
    return "\n".join((header, divider, *body))


def _escape_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _chart(
    chart_type: str,
    title: str,
    labels: Sequence[object],
    dataset_label: str,
    values: Sequence[object],
    fact: MarketMetricFact,
) -> Mapping[str, object]:
    return {
        "type": chart_type,
        "title": title,
        "labels": list(labels),
        "datasets": [{"label": dataset_label, "data": list(values)}],
        "evidence_refs": [fact.evidence_id],
    }


def _grounded_charts(
    charts: Sequence[Mapping[str, object]], facts: Sequence[MarketMetricFact]
) -> tuple[Mapping[str, object], ...]:
    from jw_chat_agent_poc.tool_use.v3_cutover import grounded_chart_specs

    raw = {fact.evidence_id: fact.raw_result for fact in facts}
    return grounded_chart_specs(charts, raw)


def _first(value: Mapping[str, object], *keys: str) -> object | None:
    return next((value[key] for key in keys if value.get(key) is not None), None)


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


__all__ = [
    "ScopeViewSet",
    "build_scope_view_set",
    "merge_evidence_bundles",
    "scope_view_choices",
]
