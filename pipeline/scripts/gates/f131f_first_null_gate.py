"""F-131f gate: every returned market-size series starts with a null growth point.

Exercises all four return boundaries end-to-end with the exact functions the
API endpoints call, including the range-less legacy path the screen actually
requests. Exits non-zero on any violation so it can gate a deploy.

    python3 -m pipeline.scripts.gates.f131f_first_null_gate
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any

# build_cache_cause imports its sibling ``cache_build_common`` by bare name, the
# way the ETL runtime invokes it (etl dir on sys.path). Mirror that here.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ETL_DIR = _REPO_ROOT / "pipeline" / "scripts" / "etl"
for _path in (str(_REPO_ROOT), str(_ETL_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.dynamic_market.cause_time import market_size_series
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics
from pipeline.scripts.api.market_scope.legacy_shape import market_size_series_payload
from pipeline.scripts.etl.build_cache_cause import market_size_series_with_yoy

GROWTH_KEYS = ("mom_growth_pct", "cmgr", "cqgr")

# UBIST monthly, >5 years so the earliest period is a genuine boundary point.
# Second point (2026-02) carries the reference -2.01% used by the deploy checks.
_RIVARO_LIKE = {
    "2021-06": 90_209_049_371.0,
    "2026-01": 84_000_000_000.0,
    "2026-02": 82_054_035_370.0,
    "2026-05": 87_019_172_843.0,
}


def _first_of_list(series: list[dict[str, Any]]) -> dict[str, Any]:
    return min(series, key=lambda point: str(point.get("period")))


def _first_of_mapping(series: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return series[min(series, key=str)]


def _check(name: str, point: dict[str, Any], failures: list[str]) -> None:
    for key in GROWTH_KEYS:
        if key in point and point[key] is not None:
            failures.append(f"{name}: first-point {key}={point[key]!r} (expected null)")
    if "mom_growth_pct" not in point:
        failures.append(f"{name}: first-point mom_growth_pct field missing")


def _metrics() -> AggregatedMetrics:
    return AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=sum(_RIVARO_LIKE.values()),
        hhi=None,
        cagr=None,
        monthly_series=tuple(
            {"period": period, "market_size": value}
            for period, value in _RIVARO_LIKE.items()
        ),
        brands=(),
    )


def main() -> int:
    failures: list[str] = []

    # Path ① dynamic cause_time
    _check("① cause_time", _first_of_list(market_size_series(_metrics())), failures)

    # Path ② legacy scoped recompute (range-less screen shape)
    _check("② legacy_shape", _first_of_mapping(market_size_series_payload(_RIVARO_LIKE, source="UBIST")), failures)

    # Path ③ cache emission dict
    _check("③ build_cache", _first_of_mapping(market_size_series_with_yoy(_RIVARO_LIKE)), failures)

    # Path ④ composer — dict-shaped legacy cache (screen request, no range)
    dict_cache = {"data": {"market_size_series": {p: {"value": v} for p, v in _RIVARO_LIKE.items()}}}
    dict_payload = compose_cached_json(dict_cache, measure="sales", source="UBIST")
    _check("④ composer(dict)", _first_of_list(dict_payload["data"]["market_size_series"]), failures)

    # Path ④ composer — precomputed point-list cache (bypass path F-131d closed)
    list_cache = {
        "data": {
            "market_size_series": [
                {"period": "2021-06", "value": 10.0, "mom_growth_pct": 46.4, "cqgr": 46.4},
                {"period": "2026-02", "value": 11.0, "mom_growth_pct": -2.01, "cqgr": -2.01},
            ]
        }
    }
    list_payload = compose_cached_json(list_cache, measure="sales", source="UBIST")
    list_points = list_payload["data"]["market_size_series"]
    _check("④ composer(list)", _first_of_list(list_points), failures)

    # G-4: second point preserved unchanged (value + growth), no regression.
    second = next(point for point in list_points if point.get("period") == "2026-02")
    if second.get("mom_growth_pct") != -2.01:
        failures.append(f"G-4: second-point mom_growth_pct={second.get('mom_growth_pct')!r} (expected -2.01)")

    if failures:
        print("F-131f FIRST-NULL GATE: FAIL")
        for line in failures:
            print(f"  ✗ {line}")
        return 1

    print("F-131f FIRST-NULL GATE: PASS (paths ①②③④, dict+list, second-point preserved)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
