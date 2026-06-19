from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.market_scope.fact_collector import StrategyFact
from pipeline.scripts.api.market_scope.recompute import recompute_strategy_payload


SchemaSignature = dict[str, tuple[str, tuple[str, ...]]]


def test_recompute_payload_matches_cache_frontend_shape_for_ubist() -> None:
    """Lock the scoped UBIST contract that FE charts consume with ``.map``."""

    _assert_frontend_shape_parity(source="UBIST", periods=tuple(f"2025-{month:02d}" for month in range(1, 13)))


def test_recompute_payload_matches_cache_frontend_shape_for_iqvia() -> None:
    """Lock the scoped IQVIA contract that FE charts consume with ``.map``."""

    _assert_frontend_shape_parity(source="IQVIA", periods=("2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"))


def _assert_frontend_shape_parity(*, source: str, periods: tuple[str, ...]) -> None:
    """Compare full cache fast-path and scoped recompute JSON schemas."""

    recompute = recompute_strategy_payload(
        _facts(source=source, periods=periods),
        focus_brand_key="Focus",
        source=source,
        measure="sales",
    )
    cache_payload = _legacy_cache_payload_template(source=source, periods=periods, recompute=recompute)
    assert _schema_signature(recompute) == _schema_signature(cache_payload), _schema_diff(
        expected=_schema_signature(cache_payload),
        actual=_schema_signature(recompute),
    )
    _assert_point_series(recompute, "$.data.market_size_series")
    _assert_point_series(recompute, "$.data.sources_data.market_size_series")


def _assert_point_series(payload: dict[str, Any], path: str) -> None:
    """Assert a market-size series is a FE-safe point array, not a dict."""

    value = _value_at(payload, path)
    assert isinstance(value, list)
    assert value
    first = value[0]
    assert isinstance(first, dict)
    assert {"period", "value", "sales_krw"}.issubset(first)


def _schema_signature(payload: Any) -> SchemaSignature:
    """Return JSON paths with container kinds and list-item keysets."""

    signature: SchemaSignature = {}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            keys = tuple(sorted(value.keys()))
            signature[path] = ("dict", keys)
            for key in keys:
                visit(value[key], f"{path}.{key}")
            return
        if isinstance(value, list):
            first_dict = next((item for item in value if isinstance(item, dict)), None)
            item_keys = tuple(sorted(first_dict.keys())) if first_dict else ()
            signature[path] = ("list", item_keys)
            if first_dict is not None:
                visit(first_dict, f"{path}[]")
            return
        signature[path] = (type(value).__name__, ())

    visit(payload, "$")
    return signature


def _schema_diff(*, expected: SchemaSignature, actual: SchemaSignature) -> str:
    """Return a compact structural diff for assertion output."""

    lines: list[str] = []
    for path in sorted(set(expected) | set(actual)):
        if expected.get(path) != actual.get(path):
            lines.append(f"{path}: expected={expected.get(path)!r} actual={actual.get(path)!r}")
    return "\n".join(lines[:80])


def _value_at(payload: Any, path: str) -> Any:
    """Resolve the subset of JSONPath syntax used by these tests."""

    current = payload
    for token in path.removeprefix("$.").split("."):
        if token.endswith("[]"):
            key = token.removesuffix("[]")
            assert isinstance(current, dict)
            current = current[key]
            assert isinstance(current, list)
            assert current, f"{path} resolved to an empty list"
            current = current[0]
            continue
        assert isinstance(current, dict)
        current = current[token]
    return current


def _legacy_cache_payload_template(
    *,
    source: str,
    periods: tuple[str, ...],
    recompute: dict[str, Any],
) -> dict[str, Any]:
    """Return a cache fast-path schema template with legacy empty sections."""

    data = recompute["data"]
    assert isinstance(data, dict)
    series_points = data["market_size_series"]
    assert isinstance(series_points, list)
    series_dict = {
        str(point["period"]): {
            "value": point["value"],
            "yoy_growth_pct": point.get("yoy_growth_pct"),
        }
        for point in series_points
        if isinstance(point, dict)
    }
    yoy_series = {period: None for period in periods}
    payload = {
        "brand": recompute["brand"],
        "brand_key": recompute["brand_key"],
        "brand_name": recompute["brand"],
        "data": {
            "analysis_level_market_status": _empty_market_status_template(),
            "analysis_levels": _empty_analysis_levels_template(periods=periods, source=source),
            "brand_ranking": data["brand_ranking"],
            "brand_ranking_stacked": data["brand_ranking_stacked"],
            "company_concentration_trend": {"periods": [], "hhi_values": []},
            "company_ranking": data["company_ranking"],
            "company_ranking_stacked": data["company_ranking_stacked"],
            "data_period_coverage": _coverage_template(periods=periods, source=source),
            "ei_ms_matrix": data["ei_ms_matrix"],
            "growth_contribution": data["growth_contribution"],
            "growth_contribution_ms_matrix": {"data": [], "ms_avg_pct": 0.0, "share_avg_pct": 0.0},
            "hhi_recent": data["hhi_recent"],
            "hhi_series_5y": data["hhi_series_5y"],
            "kpi": data["kpi"],
            "level_top5_trend": _empty_level_top5_template(),
            "market_size_series": series_dict,
            "market_yoy_recent_pct": None,
            "market_yoy_series": yoy_series,
            "sources_data": {
                "periods_unit": _period_unit(source),
                "periods_count": len(periods),
                "market_size_series": series_dict,
                "market_yoy_series": yoy_series,
                "market_yoy_recent_pct": None,
                "hhi_series_5y": data["hhi_series_5y"],
                "hhi_recent": data["hhi_recent"],
                "cagr_5y_pct": data["kpi"]["market_cagr_5y_pct"],
            },
            "target_customer_competition": _empty_target_template(),
            "target_customer_competition_by_channel": {},
            "ubist_specialty_channels": [],
            "ubist_specialty_target_channels": [],
        },
        "market_id": "scope:unresolved",
        "market_meta": _market_meta_template(source=source, recompute=recompute),
        "measure": recompute["measure"],
        "source": recompute["source"],
        "unit_label": recompute["unit_label"],
        "view": recompute["view"],
    }
    return compose_cached_json(payload, measure="sales")


def _empty_analysis_levels_template(*, periods: tuple[str, ...], source: str) -> dict[str, Any]:
    """Return legacy AnalysisLevels shape without scoped-only note keys."""

    return {
        "period_unit": _period_unit(source),
        "channels": [],
        "levels": [],
        "periods_monthly": list(periods) if source == "UBIST" else [],
        "periods_quarterly": list(periods) if source != "UBIST" else [],
        "data": {},
    }


def _empty_market_status_template() -> dict[str, Any]:
    """Return legacy ALMS shape for a scope with no level overlay."""

    return {
        "available_levels": [],
        "default_level": None,
        "by_level": {},
        "channels": [],
        "by_channel": {},
        "ms_by_channel": {},
        "targets": [],
        "note": "",
    }


def _empty_level_top5_template() -> dict[str, Any]:
    """Return legacy level trend shape for a scope with no level overlay."""

    return {"available_levels": [], "default_level": None, "by_level": {}, "note": ""}


def _empty_target_template() -> dict[str, Any]:
    """Return legacy target-competition shape for a scope with no targets."""

    return {"available_in_view": [], "target_type": "strategy_union", "targets": [], "views": [], "note": ""}


def _coverage_template(*, periods: tuple[str, ...], source: str) -> dict[str, Any]:
    """Return the legacy period coverage container."""

    years = {period[:4] for period in periods}
    latest = periods[-1] if periods else None
    latest_year = int(latest[:4]) if latest else None
    counts = {year: sum(1 for period in periods if period.startswith(year)) for year in sorted(years)}
    expected = 12 if source == "UBIST" else 4
    latest_count = counts.get(str(latest_year), 0) if latest_year is not None else 0
    return {
        "latest_period": latest,
        "latest_year": latest_year,
        "latest_year_period_count": latest_count,
        "latest_year_is_partial": latest_count < expected,
        "period_count_by_year": counts,
        "expected_periods_per_year": expected,
    }


def _market_meta_template(*, source: str, recompute: dict[str, Any]) -> dict[str, Any]:
    """Return the legacy market_meta keyset for scoped recompute payloads."""

    return {
        "strategic_market_id": recompute["market_id"],
        "market_name": "Scoped strategy union",
        "market_name_short": "Scoped strategy union",
        "market_label_kor": "Scoped strategy union",
        "market_definition_label": "Scoped strategy union",
        "market_definition_full": "Scoped strategy union",
        "mkt_team": "Runtime",
        "brand_list": [],
        "atc_codes": [],
        "atc_desc": "",
        "view_source_id": "market_scope_union",
        "atc_count": None,
        "nhi_type": None,
        "sources": [recompute["source"]],
        "source_label": recompute["source"],
        "is_dual_source": False,
        "measures": ["sales", "volume"] if source == "UBIST" else ["counting_unit", "dosage_unit", "sales", "unit"],
        "measures_label": {"primary": "sales", "secondary": None},
        "available_levels": [],
        "direct_competition_count": recompute["data"]["kpi"]["direct_competition_count"],
        "market_size_recent": recompute["data"]["kpi"]["market_size_recent"],
        "market_cagr_5y_pct": recompute["data"]["kpi"]["market_cagr_5y_pct"],
        "is_jw": False,
        "is_target": False,
    }


def _period_unit(source: str) -> str:
    """Return the legacy period unit label for source-specific series."""

    return "월간" if source == "UBIST" else "분기"


def _facts(*, source: str, periods: tuple[str, ...]) -> tuple[StrategyFact, ...]:
    """Build enough facts to produce all required FE structures."""

    source_key = "ubist" if source == "UBIST" else "iqvia_nsa"
    focus_history = {period: float(index + 10) for index, period in enumerate(periods)}
    other_history = {period: float(index + 20) for index, period in enumerate(periods)}
    return (
        _fact("Focus", "Focus", "JW", source_key, focus_history),
        _fact("Other", "Other", "Other Co", source_key, other_history),
    )


def _fact(
    brand_key: str,
    brand_name: str,
    company: str,
    source: str,
    raw_value_history: dict[str, float],
) -> StrategyFact:
    """Build one recompute fact."""

    return StrategyFact(
        market_id="strategy_001",
        raw_fact_id=f"raw:{brand_key}",
        brand_key=brand_key,
        brand_name=brand_name,
        company=company,
        source=source,
        measure="sales",
        unit_label="KRW",
        raw_value_history=raw_value_history,
    )
