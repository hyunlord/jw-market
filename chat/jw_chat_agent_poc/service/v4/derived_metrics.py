from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict

MetricValue = int | float | str

_TWO_PLACES = Decimal("0.01")
_V1_METRICS_BY_SOURCE: dict[str, tuple[str, ...]] = {
    "mart": (
        "brand_growth_rate",
        "market_growth_rate",
        "growth_spread_vs_market",
        "absolute_gap",
        "gap_change",
        "share_delta",
        "rank_delta",
    ),
    "nedrug": (
        "approval_age",
        "time_since_last_change",
        "reexam_remaining",
        "approvals_by_strength",
    ),
    "patent": (
        "earliest_active_expiry",
        "latest_active_expiry",
        "expiry_remaining",
        "active_patent_count",
    ),
    "hira": (
        "patient_yoy_growth",
        "gender_ratio",
        "age_top_segment_share",
    ),
    "clinicaltrials": (
        "count_by_status",
        "months_since_latest_registration",
    ),
}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DerivedMetricCard(_FrozenModel):
    id: str
    type: str
    entity: str
    value: MetricValue
    unit: str
    period: str | None = None
    inputs: tuple[str, ...]
    formula: str
    population: str | None = None


class DerivedMetricSkip(_FrozenModel):
    type: str
    source: str
    reason: str
    missing_inputs: tuple[str, ...]


class DerivedMetricManifest(_FrozenModel):
    generated: tuple[str, ...] = ()
    skipped: tuple[DerivedMetricSkip, ...] = ()


class DerivedMetricBuild(_FrozenModel):
    metrics: tuple[DerivedMetricCard, ...]
    manifest: DerivedMetricManifest


def build_derived_metrics(
    cards: Sequence[Mapping[str, Any]],
    *,
    observed_on: date,
) -> DerivedMetricBuild:
    """Build auditable relation cards from normalized FactDigest cards."""

    metrics: list[DerivedMetricCard] = []
    sources_present: set[str] = set()
    for card in cards:
        source = str(card.get("source") or "")
        if source not in _V1_METRICS_BY_SOURCE:
            continue
        sources_present.add(source)
        if source == "mart":
            metrics.extend(_market_metrics(card))
        elif source == "nedrug":
            metrics.extend(_nedrug_metrics(card, observed_on))
        elif source == "patent":
            metrics.extend(_patent_metrics(card))
        elif source == "hira":
            metrics.extend(_hira_metrics(card))
        elif source == "clinicaltrials":
            metrics.extend(_clinical_metrics(card, observed_on))

    generated_types = {metric.type for metric in metrics}
    skipped = tuple(
        DerivedMetricSkip(
            type=metric_type,
            source=source,
            reason=(
                "source_card_missing"
                if source not in sources_present
                else "required_normalized_inputs_missing"
            ),
            missing_inputs=_missing_inputs(metric_type),
        )
        for source, metric_types in _V1_METRICS_BY_SOURCE.items()
        for metric_type in metric_types
        if metric_type not in generated_types
    )
    ordered = tuple(metrics)
    return DerivedMetricBuild(
        metrics=ordered,
        manifest=DerivedMetricManifest(
            generated=tuple(metric.id for metric in ordered),
            skipped=skipped,
        ),
    )


def _market_metrics(card: Mapping[str, Any]) -> list[DerivedMetricCard]:
    entity = str(card.get("entity") or "브랜드")
    ids = _evidence_ids(card)
    row = _market_relation_row(card, entity)
    brand_series = _money_series(
        row.get("brand_value_series_10pt") if row else None,
    )
    if len(brand_series) < 2:
        brand_series = _money_series(_nested(card, "full_stats", "series"))
    market_series = _money_series(row.get("market_size_series") if row else None)
    metrics: list[DerivedMetricCard] = []

    brand_growth = _growth_metric(
        "brand_growth_rate",
        entity,
        brand_series,
        _series_endpoint_inputs(brand_series, ids),
    )
    if brand_growth is not None:
        metrics.append(brand_growth)
    market_growth = _growth_metric(
        "market_growth_rate",
        str(row.get("market_name") or "시장 전체") if row else "시장 전체",
        market_series,
        _series_endpoint_inputs(market_series, ids),
    )
    if market_growth is not None:
        metrics.append(market_growth)
    if brand_growth is not None and market_growth is not None:
        metrics.append(
            _metric(
                "growth_spread_vs_market",
                entity,
                _round_two(_decimal(brand_growth.value) - _decimal(market_growth.value)),
                "%p",
                brand_growth.period,
                tuple(dict.fromkeys((*brand_growth.inputs, *market_growth.inputs))),
                f"{brand_growth.value}-{market_growth.value}",
            )
        )

    if len(brand_series) >= 2:
        start, end = brand_series[0], brand_series[-1]
        period = f"{start['period']}~{end['period']}"
        endpoint_inputs = _series_endpoint_inputs((start, end), ids)
        start_share = _decimal_or_none(start.get("share"))
        end_share = _decimal_or_none(end.get("share"))
        if start_share is not None and end_share is not None:
            metrics.append(
                _metric(
                    "share_delta",
                    entity,
                    _round_two(end_share - start_share),
                    "%p",
                    period,
                    endpoint_inputs,
                    f"{_display(end_share)}-{_display(start_share)}",
                )
            )
        start_rank = _int_or_none(start.get("rank"))
        end_rank = _int_or_none(end.get("rank"))
        if start_rank is not None and end_rank is not None:
            metrics.append(
                _metric(
                    "rank_delta",
                    entity,
                    end_rank - start_rank,
                    "rank",
                    period,
                    endpoint_inputs,
                    f"{end_rank}-{start_rank}",
                )
            )
        competitors = _competitor_series(row, entity) if row else ()
        for competitor_name, competitor_series in competitors[:2]:
            comparable = _aligned_endpoints(brand_series, competitor_series)
            if comparable is None:
                continue
            brand_start, brand_end, competitor_start, competitor_end = comparable
            gap_inputs = _point_inputs(
                (brand_start, brand_end, competitor_start, competitor_end),
                ids,
            )
            competitor_period = (
                f"{brand_start['period']}~{brand_end['period']}"
            )
            gap_start = abs(
                _decimal(brand_start["value"]) - _decimal(competitor_start["value"])
            )
            gap_end = abs(
                _decimal(brand_end["value"]) - _decimal(competitor_end["value"])
            )
            relation_entity = f"{entity} vs {competitor_name}"
            for point, gap, target, competitor in (
                (brand_start, gap_start, brand_start, competitor_start),
                (brand_end, gap_end, brand_end, competitor_end),
            ):
                metrics.append(
                    _metric(
                        "absolute_gap",
                        relation_entity,
                        _round_two(gap),
                        "억원",
                        str(point["period"]),
                        _series_endpoint_inputs((target, competitor), gap_inputs),
                        f"abs({_display(target['value'])}-{_display(competitor['value'])})",
                    )
                )
            metrics.append(
                _metric(
                    "gap_change",
                    relation_entity,
                    _round_two(gap_end - gap_start),
                    "억원",
                    competitor_period,
                    gap_inputs,
                    f"{_display(gap_end)}-{_display(gap_start)}",
                )
            )
    return metrics


def _series_endpoint_inputs(
    series: Sequence[Mapping[str, Any]],
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    if not series:
        return fallback
    return _point_inputs((series[0], series[-1]), fallback)


def _point_inputs(
    points: Sequence[Mapping[str, Any]],
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    inputs = tuple(
        dict.fromkeys(
            str(point.get("evidence_id") or "")
            for point in points
            if str(point.get("evidence_id") or "")
        )
    )
    return inputs or fallback


def _nedrug_metrics(
    card: Mapping[str, Any],
    observed_on: date,
) -> list[DerivedMetricCard]:
    entity = str(card.get("entity") or "허가 품목")
    ids = _evidence_ids(card)
    temporal = _mapping(card.get("temporal_stats"))
    metrics: list[DerivedMetricCard] = []
    for approval in _mappings(temporal.get("approvals")):
        evidence_ids = _item_inputs(approval, ids)
        elapsed = _int_or_none(approval.get("elapsed_years"))
        if elapsed is None:
            continue
        item = str(approval.get("item_name") or entity)
        approval_date = str(approval.get("approval_date") or "")
        metrics.append(
            _metric(
                "approval_age",
                item,
                elapsed,
                "years",
                approval_date or None,
                evidence_ids,
                f"elapsed_years({approval_date},{observed_on.isoformat()})",
            )
        )
    for change in _mappings(temporal.get("latest_changes")):
        changed_on = _date_or_none(change.get("change_date"))
        if changed_on is None:
            continue
        item = str(change.get("item_name") or entity)
        metrics.append(
            _metric(
                "time_since_last_change",
                item,
                max(_whole_months(changed_on, observed_on), 0),
                "months",
                f"{changed_on.isoformat()}~{observed_on.isoformat()}",
                _item_inputs(change, ids),
                f"whole_months({changed_on.isoformat()},{observed_on.isoformat()})",
            )
        )
    for reexam in _mappings(temporal.get("reexaminations")):
        remaining = _int_or_none(reexam.get("remaining_months"))
        if remaining is None or reexam.get("is_expired") is True:
            continue
        item = str(reexam.get("item_name") or entity)
        end_date = str(reexam.get("reexam_end_date") or "")
        metrics.append(
            _metric(
                "reexam_remaining",
                item,
                remaining,
                "months",
                end_date or None,
                _item_inputs(reexam, ids),
                f"whole_months({observed_on.isoformat()},{end_date})",
            )
        )

    strengths: Counter[str] = Counter()
    for row in _mappings(card.get("visible_rows")):
        item_name = str(row.get("item_name") or row.get("ITEM_NAME") or "")
        strength = _strength(item_name)
        if strength:
            strengths[strength] += 1
    for strength, count in sorted(strengths.items()):
        metrics.append(
            _metric(
                "approvals_by_strength",
                f"{entity} {strength}",
                count,
                "count",
                None,
                ids,
                f"count(strength={strength})",
            )
        )
    return metrics


def _patent_metrics(card: Mapping[str, Any]) -> list[DerivedMetricCard]:
    entity = str(card.get("entity") or "특허")
    ids = _evidence_ids(card)
    temporal = _mapping(card.get("temporal_stats"))
    active = tuple(
        item
        for item in _mappings(temporal.get("expirations"))
        if item.get("is_expired") is False
        and str(item.get("status") or "").casefold() in {"등록", "active", "registered"}
        and _date_or_none(item.get("expiration_date")) is not None
    )
    if not active:
        return []
    ordered = tuple(sorted(active, key=lambda item: str(item["expiration_date"])))
    earliest, latest = ordered[0], ordered[-1]
    metrics = [
        _metric(
            "earliest_active_expiry",
            entity,
            str(earliest["expiration_date"]),
            "date",
            str(earliest["expiration_date"]),
            _item_inputs(earliest, ids),
            "min(active.expiration_date)",
        ),
        _metric(
            "latest_active_expiry",
            entity,
            str(latest["expiration_date"]),
            "date",
            str(latest["expiration_date"]),
            _item_inputs(latest, ids),
            "max(active.expiration_date)",
        ),
    ]
    representative_expiry = str(
        _nested(card, "representative", "expiration_date") or ""
    )
    representative = next(
        (
            item
            for item in ordered
            if str(item.get("expiration_date")) == representative_expiry
        ),
        latest,
    )
    remaining = _int_or_none(representative.get("remaining_months"))
    if remaining is not None:
        metrics.append(
            _metric(
                "expiry_remaining",
                entity,
                remaining,
                "months",
                str(representative.get("expiration_date") or "") or None,
                _item_inputs(representative, ids),
                "representative.remaining_months",
            )
        )
    population_count = _int_or_none(
        _nested(card, "full_stats", "product_combination_count")
    )
    if population_count is not None:
        metrics.append(
            _metric(
                "active_patent_count",
                entity,
                len(active),
                "count",
                str(temporal.get("reference_date") or "") or None,
                tuple(
                    dict.fromkeys(
                        evidence_id
                        for item in active
                        for evidence_id in _item_inputs(item, ids)
                    )
                ),
                "count(active where status=registered)",
                population=f"direct_related_{population_count}",
            )
        )
    return metrics


def _hira_metrics(card: Mapping[str, Any]) -> list[DerivedMetricCard]:
    entity = str(card.get("entity") or "질환")
    ids = _evidence_ids(card)
    rows = _mappings(card.get("visible_rows"))
    metrics: list[DerivedMetricCard] = []
    series_by_scope: dict[tuple[str, str], list[tuple[str, Decimal]]] = defaultdict(list)
    for row in rows:
        period = _row_period(row)
        value = _row_value(row)
        if not period or value is None:
            continue
        code = str(
            row.get("sickCd")
            or row.get("sick_cd")
            or row.get("disease_code")
            or row.get("code")
            or ""
        )
        dimension = "|".join(
            str(row.get(key) or "")
            for key in ("sex", "gender", "sexCd", "age", "age_group", "ageCd")
        )
        series_by_scope[(code, dimension)].append((period, value))
    growth_candidate = next(
        (
            sorted(points)
            for points in series_by_scope.values()
            if len({period for period, _value in points}) >= 2
        ),
        None,
    )
    if growth_candidate:
        start, end = growth_candidate[0], growth_candidate[-1]
        if start[1] != 0:
            metrics.append(
                _metric(
                    "patient_yoy_growth",
                    entity,
                    _round_two((end[1] - start[1]) / start[1] * Decimal(100)),
                    "%",
                    f"{start[0]}~{end[0]}",
                    ids,
                    f"({_display(end[1])}-{_display(start[1])})/{_display(start[1])}*100",
                )
            )

    representative = _mapping(card.get("representative"))
    representative_code = str(
        representative.get("sickCd")
        or representative.get("sick_cd")
        or representative.get("disease_code")
        or representative.get("code")
        or ""
    )
    gender_values: Counter[str] = Counter()
    age_values: Counter[str] = Counter()
    metric_inputs = ids
    selected_scope = _select_hira_metric_scope(
        _mappings(_nested(card, "full_stats", "metric_scopes")),
        representative_code,
    )
    if selected_scope:
        latest_period = str(selected_scope.get("period") or "")
        selected_scope_key = (
            latest_period,
            str(selected_scope.get("code") or ""),
            str(selected_scope.get("source_tool") or ""),
        )
        gender_values.update(
            {
                str(key): float(value)
                for key, value in _mapping(selected_scope.get("gender_totals")).items()
            }
        )
        age_values.update(
            {
                str(key): float(value)
                for key, value in _mapping(selected_scope.get("age_totals")).items()
            }
        )
        scoped_ids = selected_scope.get("evidence_ids")
        if isinstance(scoped_ids, Sequence) and not isinstance(scoped_ids, (str, bytes)):
            metric_inputs = tuple(dict.fromkeys(str(value) for value in scoped_ids)) or ids
    else:
        analysis_groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            period = _row_period(row)
            code = str(
                row.get("sickCd")
                or row.get("sick_cd")
                or row.get("disease_code")
                or row.get("code")
                or ""
            )
            gender = str(row.get("sex") or row.get("gender") or row.get("sexCd") or "")
            age = str(row.get("age") or row.get("age_group") or row.get("ageCd") or "")
            if not period or not gender or not age:
                continue
            if representative_code and code != representative_code:
                continue
            source_tool = str(row.get("_source_tool") or row.get("source_tool") or "")
            analysis_groups[(period, code, source_tool)].append(row)
        selected_scope_key = max(
            analysis_groups,
            key=lambda key: (key[0], len(analysis_groups[key]), key[1], key[2]),
            default=None,
        )
        latest_rows = tuple(analysis_groups.get(selected_scope_key, ()))
        latest_period = selected_scope_key[0] if selected_scope_key is not None else ""
        for row in latest_rows:
            value = _row_value(row)
            if value is None:
                continue
            gender = str(row.get("sex") or row.get("gender") or row.get("sexCd") or "")
            age = str(row.get("age") or row.get("age_group") or row.get("ageCd") or "")
            if gender:
                gender_values[gender] += float(value)
            if age:
                age_values[age] += float(value)
    if len(gender_values) >= 2:
        ordered_gender = gender_values.most_common(2)
        denominator = Decimal(str(ordered_gender[1][1]))
        if denominator != 0:
            metrics.append(
                _metric(
                    "gender_ratio",
                    f"{entity} {ordered_gender[0][0]}/{ordered_gender[1][0]}",
                    _round_two(Decimal(str(ordered_gender[0][1])) / denominator),
                    "ratio",
                    latest_period or None,
                    metric_inputs,
                    f"{ordered_gender[0][1]}/{ordered_gender[1][1]}",
                    population=("|".join(selected_scope_key) if selected_scope_key else None),
                )
            )
    age_total = sum(Decimal(str(value)) for value in age_values.values())
    if age_values and age_total > 0:
        age, value = age_values.most_common(1)[0]
        metrics.append(
            _metric(
                "age_top_segment_share",
                f"{entity} {age}",
                _round_two(Decimal(str(value)) / age_total * Decimal(100)),
                "%",
                latest_period or None,
                metric_inputs,
                f"{value}/{_display(age_total)}*100",
                population=("|".join(selected_scope_key) if selected_scope_key else None),
            )
        )
    return metrics


def _select_hira_metric_scope(
    scopes: Sequence[Mapping[str, Any]],
    representative_code: str,
) -> Mapping[str, Any]:
    eligible = tuple(
        scope
        for scope in scopes
        if not representative_code or str(scope.get("code") or "") == representative_code
    )
    return max(
        eligible,
        key=lambda scope: (
            str(scope.get("period") or ""),
            len(_mapping(scope.get("age_totals"))),
            str(scope.get("code") or ""),
            str(scope.get("source_tool") or ""),
        ),
        default={},
    )


def _clinical_metrics(
    card: Mapping[str, Any],
    observed_on: date,
) -> list[DerivedMetricCard]:
    entity = str(card.get("entity") or "임상시험")
    ids = _evidence_ids(card)
    metrics: list[DerivedMetricCard] = []
    statuses = _mapping(_nested(card, "distributions", "status"))
    for status, raw_count in sorted(statuses.items()):
        count = _int_or_none(raw_count)
        if count is None:
            continue
        metrics.append(
            _metric(
                "count_by_status",
                f"{entity} {status}",
                count,
                "count",
                str(_nested(card, "temporal_stats", "reference_date") or "") or None,
                ids,
                f"count(status={status})",
            )
        )
    latest_update = _mapping(_nested(card, "temporal_stats", "latest_update"))
    registered_on = _date_or_none(latest_update.get("last_update_date"))
    if registered_on is None:
        registered_on = _date_or_none(
            _nested(card, "representative", "last_update_date")
        )
    if registered_on is not None:
        metrics.append(
            _metric(
                "months_since_latest_registration",
                entity,
                max(_whole_months(registered_on, observed_on), 0),
                "months",
                f"{registered_on.isoformat()}~{observed_on.isoformat()}",
                _item_inputs(latest_update, ids),
                f"whole_months({registered_on.isoformat()},{observed_on.isoformat()})",
            )
        )
    return metrics


def _growth_metric(
    metric_type: str,
    entity: str,
    series: Sequence[Mapping[str, Any]],
    inputs: tuple[str, ...],
) -> DerivedMetricCard | None:
    if len(series) < 2:
        return None
    start, end = series[0], series[-1]
    start_value = _decimal(start["value"])
    end_value = _decimal(end["value"])
    if start_value == 0:
        return None
    period = f"{start['period']}~{end['period']}"
    return _metric(
        metric_type,
        entity,
        _round_two((end_value - start_value) / start_value * Decimal(100)),
        "%",
        period,
        inputs,
        f"({_display(end_value)}-{_display(start_value)})/{_display(start_value)}*100",
    )


def _metric(
    metric_type: str,
    entity: str,
    value: MetricValue,
    unit: str,
    period: str | None,
    inputs: tuple[str, ...],
    formula: str,
    *,
    population: str | None = None,
) -> DerivedMetricCard:
    identity = "|".join((metric_type, entity, str(period or ""), formula))
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return DerivedMetricCard(
        id=f"dm_{metric_type}_{suffix}",
        type=metric_type,
        entity=entity,
        value=value,
        unit=unit,
        period=period,
        inputs=inputs,
        formula=formula,
        population=population,
    )


def _market_relation_row(
    card: Mapping[str, Any],
    entity: str,
) -> Mapping[str, Any]:
    normalized = _nested(card, "full_stats", "relation_inputs")
    if isinstance(normalized, Mapping) and normalized.get(
        "brand_value_series_10pt"
    ):
        return normalized
    rows = _mappings(card.get("visible_rows"))
    exact = tuple(
        row
        for row in rows
        if str(row.get("brand") or row.get("entity") or "") == entity
        and str(row.get("metric") or "").casefold() == "sales"
    )
    if exact:
        return exact[0]
    rich = tuple(
        row
        for row in rows
        if row.get("brand_value_series_10pt") and row.get("market_size_series")
    )
    return rich[0] if rich else {}


def _money_series(value: Any) -> tuple[dict[str, Any], ...]:
    points: list[dict[str, Any]] = []
    for row in _mappings(value):
        period = str(row.get("period") or "")
        amount = _money_eok(row)
        if not period or amount is None:
            continue
        points.append(
            {
                "period": period,
                "value": amount,
                "share": row.get("ms_pct") or row.get("market_share_pct"),
                "rank": row.get("rank"),
                "evidence_id": row.get("evidence_id"),
            }
        )
    points.sort(key=lambda item: item["period"])
    return tuple(points)


def _money_eok(row: Mapping[str, Any]) -> Decimal | None:
    for key in ("value_억원", "sales_억원", "market_size_억원"):
        value = _decimal_or_none(row.get(key))
        if value is not None:
            return value
    for key in ("value_krw", "sales_krw", "market_size_recent_krw"):
        value = _decimal_or_none(row.get(key))
        if value is not None:
            return value / Decimal(100_000_000)
    value = _decimal_or_none(row.get("value"))
    if value is None:
        return None
    if str(row.get("unit_label") or "").upper() == "KRW":
        return value / Decimal(100_000_000)
    return value


def _competitor_series(
    row: Mapping[str, Any],
    entity: str,
) -> tuple[tuple[str, tuple[dict[str, Any], ...]], ...]:
    competitors: list[tuple[int, str, tuple[dict[str, Any], ...]]] = []
    for competitor in _mappings(row.get("level_top5_trend_series")):
        name = str(competitor.get("brand") or "")
        rank = _int_or_none(competitor.get("rank"))
        series = _money_series(competitor.get("series"))
        if not name or name == entity or rank not in {1, 2} or len(series) < 2:
            continue
        competitors.append((rank, name, series))
    competitors.sort(key=lambda item: (item[0], item[1]))
    return tuple((name, series) for _rank, name, series in competitors)


def _aligned_endpoints(
    target: Sequence[Mapping[str, Any]],
    competitor: Sequence[Mapping[str, Any]],
) -> tuple[
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
] | None:
    competitor_by_period = {str(point["period"]): point for point in competitor}
    shared = [point for point in target if str(point["period"]) in competitor_by_period]
    if len(shared) < 2:
        return None
    start, end = shared[0], shared[-1]
    return (
        start,
        end,
        competitor_by_period[str(start["period"])],
        competitor_by_period[str(end["period"])],
    )


def _evidence_ids(card: Mapping[str, Any]) -> tuple[str, ...]:
    raw = card.get("evidence_ids")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(dict.fromkeys(str(value) for value in raw if str(value)))


def _item_inputs(
    item: Mapping[str, Any],
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    evidence_id = str(item.get("evidence_id") or "")
    return (evidence_id,) if evidence_id else fallback


def _mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nested(card: Mapping[str, Any], *path: str) -> Any:
    current: Any = card
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _decimal(value: Any) -> Decimal:
    parsed = _decimal_or_none(value)
    if parsed is None:
        raise ValueError(f"not a finite decimal: {value!r}")
    return parsed


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _int_or_none(value: Any) -> int | None:
    parsed = _decimal_or_none(value)
    if parsed is None or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _round_two(value: Decimal) -> float:
    return float(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def _display(value: Any) -> str:
    parsed = _decimal_or_none(value)
    if parsed is None:
        return str(value)
    return format(parsed.normalize(), "f")


def _date_or_none(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _whole_months(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    return months - int(end.day < start.day)


def _strength(item_name: str) -> str:
    match = re.search(
        r"(?<!\d)(\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?)\s*(밀리그램|mg|그램|g)",
        item_name,
        re.IGNORECASE,
    )
    return re.sub(r"\s+", "", match.group(0)) if match else ""


def _row_period(row: Mapping[str, Any]) -> str:
    return str(
        row.get("period")
        or row.get("year")
        or row.get("yyyy")
        or row.get("base_year")
        or ""
    )


def _row_value(row: Mapping[str, Any]) -> Decimal | None:
    for key in (
        "value",
        "patient_count",
        "count",
        "ptntCnt",
        "rnum",
        "total",
    ):
        value = _decimal_or_none(row.get(key))
        if value is not None:
            return value
    return None


def _missing_inputs(metric_type: str) -> tuple[str, ...]:
    requirements = {
        "brand_growth_rate": ("brand_series_t0", "brand_series_t1"),
        "market_growth_rate": ("market_series_t0", "market_series_t1"),
        "growth_spread_vs_market": ("brand_growth_rate", "market_growth_rate"),
        "absolute_gap": ("brand_series", "rank_1_or_2_series"),
        "gap_change": ("absolute_gap_t0", "absolute_gap_t1"),
        "share_delta": ("share_t0", "share_t1"),
        "rank_delta": ("rank_t0", "rank_t1"),
        "approval_age": ("approval_date",),
        "time_since_last_change": ("change_date",),
        "reexam_remaining": ("reexam_end_date",),
        "approvals_by_strength": ("item_name_strength",),
        "earliest_active_expiry": ("active_expiration_date",),
        "latest_active_expiry": ("active_expiration_date",),
        "expiry_remaining": ("representative_expiration_date",),
        "active_patent_count": ("active_patents", "population"),
        "patient_yoy_growth": ("patient_t0", "patient_t1"),
        "gender_ratio": ("gender_value_a", "gender_value_b"),
        "age_top_segment_share": ("age_segment_values",),
        "count_by_status": ("clinical_statuses",),
        "months_since_latest_registration": ("last_update_date",),
    }
    return requirements[metric_type]


__all__ = [
    "DerivedMetricBuild",
    "DerivedMetricCard",
    "DerivedMetricManifest",
    "DerivedMetricSkip",
    "build_derived_metrics",
]
