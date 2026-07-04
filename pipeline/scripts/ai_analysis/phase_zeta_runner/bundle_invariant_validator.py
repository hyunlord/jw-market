from __future__ import annotations

from typing import Any

from .config import ValidatorConfig


def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _market_size_value(market_size_history: dict[str, Any], period: str) -> float | None:
    value = market_size_history.get(period)
    if isinstance(value, dict):
        return _numeric(value.get("raw_value", value.get("value")))
    return _numeric(value)


def _brand_rows_for_period(bundle: dict[str, Any], view: dict[str, Any], period: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    target_history = (view.get("target_brand_metric", {}) or {}).get("history", {}) or {}
    target_period = target_history.get(period, {}) or {}
    target_rank = _numeric(target_period.get("rank"))
    if "raw_value" in target_period and target_rank is not None:
        rows.append(
            {
                "brand": (bundle.get("brand_context", {}) or {}).get("name") or bundle.get("bundle_meta", {}).get("brand"),
                "raw": _numeric(target_period.get("raw_value")),
                "rank": int(target_rank),
                "ms": _numeric(target_period.get("ms_pct", target_period.get("ms"))),
                "is_target": True,
            }
        )

    for comp in view.get("competitors_top5", []) or []:
        comp_period = ((comp.get("history", {}) or {}).get(period, {}) or {})
        comp_rank = _numeric(comp_period.get("rank"))
        if "raw_value" in comp_period and comp_rank is not None:
            rows.append(
                {
                    "brand": comp.get("brand_name"),
                    "raw": _numeric(comp_period.get("raw_value")),
                    "rank": int(comp_rank),
                    "ms": _numeric(comp_period.get("ms_pct", comp_period.get("ms"))),
                    "is_target": False,
                }
            )
    return [row for row in rows if row["raw"] is not None]


def _periods(view: dict[str, Any]) -> set[str]:
    periods: set[str] = set()
    periods.update(((view.get("target_brand_metric", {}) or {}).get("history", {}) or {}).keys())
    for comp in view.get("competitors_top5", []) or []:
        periods.update((comp.get("history", {}) or {}).keys())
    return periods


def _rank_order_violations(view_id: str, period: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    sorted_rows = sorted(rows, key=lambda item: item["raw"], reverse=True)
    context = [(item["brand"], item["raw"], item["rank"]) for item in sorted_rows[:10]]

    for observed_position, item in enumerate(sorted_rows, start=1):
        order_conflict = any(
            other is not item and other["raw"] > item["raw"] and other["rank"] >= item["rank"]
            for other in sorted_rows
        )
        if order_conflict:
            violations.append(
                {
                    "view_id": view_id,
                    "period": period,
                    "type": "rank_vs_raw_order_mismatch",
                    "brand": item["brand"],
                    "expected_rank_by_raw": observed_position,
                    "actual_rank_in_bundle": item["rank"],
                    "raw_value": item["raw"],
                    "context_top_brands": context,
                }
            )
    return violations


def validate_bundle_invariants(bundle: dict[str, Any], config: ValidatorConfig) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []

    for view in bundle.get("market_views", []) or []:
        view_id = str(view.get("view_id", "unknown_view"))
        market_size_history = (view.get("market_size", {}) or {}).get("history", {}) or {}

        for period in sorted(_periods(view)):
            rows = _brand_rows_for_period(bundle, view, period)
            if len(rows) >= 2:
                violations.extend(_rank_order_violations(view_id, period, rows))

            market_size = _market_size_value(market_size_history, period)
            if market_size:
                for row in rows:
                    if row["ms"] is None:
                        continue
                    computed_ms = row["raw"] / market_size * 100
                    if abs(row["ms"] - computed_ms) > config.tolerance_percent:
                        violations.append(
                            {
                                "view_id": view_id,
                                "period": period,
                                "type": "ms_calculation_mismatch",
                                "brand": row["brand"],
                                "declared_ms": row["ms"],
                                "computed_ms": round(computed_ms, 4),
                                "raw": row["raw"],
                                "market_size": market_size,
                            }
                        )

            total_ms = sum(row["ms"] or 0.0 for row in rows)
            if total_ms > 100.5:
                violations.append(
                    {
                        "view_id": view_id,
                        "period": period,
                        "type": "ms_sum_exceeds_100",
                        "total_ms": round(total_ms, 4),
                        "brand_count": len(rows),
                    }
                )

        # The v1_1 bundles usually carry only recent periods, while CAGR is a
        # 5-year KPI. Only assert CAGR direction when the embedded history spans
        # enough periods to make the sign comparison meaningful.
        target = view.get("target_brand_metric", {}) or {}
        history = target.get("history", {}) or {}
        cagr = (target.get("kpi_extras", {}) or {}).get("brand_cagr_5y_pct")
        if cagr is not None and len(history) >= 24:
            periods_sorted = sorted(history)
            first_raw = _numeric((history.get(periods_sorted[0], {}) or {}).get("raw_value"))
            last_raw = _numeric((history.get(periods_sorted[-1], {}) or {}).get("raw_value"))
            if first_raw is not None and last_raw is not None and first_raw != last_raw:
                growth_positive = last_raw > first_raw
                cagr_positive = float(cagr) > 0
                if growth_positive != cagr_positive:
                    violations.append(
                        {
                            "view_id": view_id,
                            "type": "cagr_sign_inconsistent",
                            "cagr_5y": float(cagr),
                            "first_raw": first_raw,
                            "last_raw": last_raw,
                            "first_period": periods_sorted[0],
                            "last_period": periods_sorted[-1],
                        }
                    )

    return {
        "valid": not violations,
        "total_violations": len(violations),
        "violations": violations,
    }
