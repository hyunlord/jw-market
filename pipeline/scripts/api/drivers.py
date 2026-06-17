from __future__ import annotations

from typing import Any


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_drivers(metric_row: dict[str, Any], view: str = "market_landscape") -> list[dict[str, Any]]:
    """Map Layer 3 metrics into deterministic cause drivers."""
    drivers: list[dict[str, Any]] = []

    ei = _as_float(metric_row.get("ei_5y"))
    if ei is not None:
        if ei > 120:
            drivers.append(
                {
                    "type": "evolution_outperform",
                    "metric": "ei_5y",
                    "value": ei,
                    "severity": "info",
                    "explanation": "5Y CAGR이 시장 CAGR 대비 우위입니다.",
                }
            )
        elif ei < 80:
            drivers.append(
                {
                    "type": "evolution_underperform",
                    "metric": "ei_5y",
                    "value": ei,
                    "severity": "warning",
                    "explanation": "5Y CAGR이 시장 CAGR 대비 낮습니다.",
                }
            )

    momentum = _as_float(metric_row.get("momentum_score"))
    if momentum is not None:
        drivers.append(
            {
                "type": "momentum_up" if momentum > 0 else "momentum_down",
                "metric": "momentum_score",
                "value": momentum,
                "severity": "info",
                "explanation": "최근 4분기 시장점유율 slope입니다.",
            }
        )

    contribution = _as_float(metric_row.get("growth_contribution"))
    if contribution is not None:
        drivers.append(
            {
                "type": "growth_contributor" if contribution > 0 else "growth_detractor",
                "metric": "growth_contribution",
                "value": contribution,
                "severity": "info" if contribution >= 0 else "warning",
                "explanation": "시장 성장 변화에 대한 브랜드 기여도입니다.",
            }
        )

    hhi = _as_float(metric_row.get("hhi"))
    if hhi is not None and view == "market_landscape":
        drivers.append(
            {
                "type": "concentration_high" if hhi >= 1500 else "concentration_low",
                "metric": "hhi",
                "value": hhi,
                "severity": "warning" if hhi >= 1500 else "info",
                "explanation": "시장 집중도(HHI) 기준 경쟁 구도입니다.",
            }
        )

    market_cagr = _as_float(metric_row.get("market_cagr_5y"))
    if market_cagr is not None:
        drivers.append(
            {
                "type": "market_growth_tailwind" if market_cagr > 0 else "market_growth_headwind",
                "metric": "market_cagr_5y",
                "value": market_cagr,
                "severity": "info" if market_cagr >= 0 else "warning",
                "explanation": "시장 전체 5Y CAGR 방향성입니다.",
            }
        )

    return drivers
