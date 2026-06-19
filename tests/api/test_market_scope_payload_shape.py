from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.market_scope.fact_collector import StrategyFact
from pipeline.scripts.api.market_scope.recompute import recompute_strategy_payload


@dataclass(frozen=True, slots=True)
class ShapeSignature:
    """Container kind and first list-item keys for FE-facing parity checks."""

    kind: str
    item_keys: tuple[str, ...] = ()


def test_recompute_payload_matches_cache_frontend_shape_for_ubist() -> None:
    """Lock the scoped UBIST contract that FE charts consume with ``.map``."""

    _assert_frontend_shape_parity(source="UBIST", periods=tuple(f"2025-{month:02d}" for month in range(1, 13)))


def test_recompute_payload_matches_cache_frontend_shape_for_iqvia() -> None:
    """Lock the scoped IQVIA contract that FE charts consume with ``.map``."""

    _assert_frontend_shape_parity(source="IQVIA", periods=("2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"))


def _assert_frontend_shape_parity(*, source: str, periods: tuple[str, ...]) -> None:
    """Compare cache composer output and scoped recompute output at FE paths."""

    recompute = recompute_strategy_payload(
        _facts(source=source, periods=periods),
        focus_brand_key="Focus",
        source=source,
        measure="sales",
    )
    cache_payload = compose_cached_json(_cache_like_payload(recompute), measure="sales")
    for path in _required_paths():
        assert _shape(cache_payload, path).kind == _shape(recompute, path).kind

    _assert_point_series(recompute, "$.data.market_size_series")
    _assert_point_series(recompute, "$.data.sources_data.market_size_series")
    assert _shape(recompute, "$.data.kpi").kind == "dict"
    assert _shape(recompute, "$.data.brand_ranking_stacked.yearly[]").kind == "dict"
    assert _shape(recompute, "$.data.company_ranking_stacked.yearly[]").kind == "dict"
    assert _shape(recompute, "$.data.ei_ms_matrix.data[]").kind == "dict"
    assert _shape(recompute, "$.data.growth_contribution.by_brand.top_contributors[]").kind == "dict"


def _required_paths() -> tuple[str, ...]:
    """Return the FE paths whose container kind must match cache fast-path."""

    return (
        "$.data.market_size_series",
        "$.data.sources_data.market_size_series",
        "$.data.sources_data.hhi_series_5y",
        "$.data.kpi",
        "$.data.brand_ranking_stacked.yearly[]",
        "$.data.company_ranking_stacked.yearly[]",
        "$.data.ei_ms_matrix.data[]",
        "$.data.growth_contribution.by_brand.top_contributors[]",
    )


def _assert_point_series(payload: dict[str, Any], path: str) -> None:
    """Assert a market-size series is a FE-safe point array, not a dict."""

    value = _value_at(payload, path)
    assert isinstance(value, list)
    assert value
    first = value[0]
    assert isinstance(first, dict)
    assert {"period", "value", "sales_krw"}.issubset(first)


def _shape(payload: Any, path: str) -> ShapeSignature:
    """Return a small structural signature for a JSON path."""

    value = _value_at(payload, path)
    if isinstance(value, list):
        first_dict = next((item for item in value if isinstance(item, dict)), None)
        return ShapeSignature("list", tuple(sorted(first_dict.keys())) if first_dict else ())
    if isinstance(value, dict):
        return ShapeSignature("dict", tuple(sorted(value.keys())))
    return ShapeSignature(type(value).__name__)


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


def _cache_like_payload(recompute: dict[str, Any]) -> dict[str, Any]:
    """Return a cache-shaped sample that exercises composer aliases."""

    data = dict(recompute["data"])
    series_points = data["market_size_series"]
    assert isinstance(series_points, list)
    series_dict = {str(point["period"]): point["value"] for point in series_points if isinstance(point, dict)}
    sources_data = dict(data["sources_data"])
    sources_data["market_size_series"] = series_dict
    data["market_size_series"] = series_dict
    data["sources_data"] = sources_data
    payload = dict(recompute)
    payload["data"] = data
    return payload


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
