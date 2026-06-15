from __future__ import annotations

from pathlib import Path
import json


class MetricsTool:
    def __init__(self, fixture_path: Path | None = None) -> None:
        path = fixture_path or Path(__file__).resolve().parents[2] / "fixtures" / "metrics_cache.json"
        self._data = json.loads(path.read_text(encoding="utf-8"))

    def get_market_landscape(self, market: str, level: str = "overall", view_type: str = "market_landscape") -> dict:
        item = self._data["markets"].get(market)
        if not item:
            raise LookupError(f"Unknown market fixture: {market}")
        return {
            "source": "cache",
            "tool": "get_market_landscape",
            "summary_text": (
                f"{item['label']} 시장은 {item['latest_period']} 기준 {item['market_size_krw']}원, "
                f"5년 CAGR {item['cagr_5y_pct']}%, HHI {item['hhi']}입니다."
            ),
            "render_data": {
                "market": market,
                "level": level,
                "view_type": view_type,
                "series": item["market_size_series"],
                "cagr_5y_pct": item["cagr_5y_pct"],
                "hhi": item["hhi"],
            },
        }

    def get_brand_metric(self, brand: str, metric: str = "market_share", period: str = "2026-04") -> dict:
        item = self._data["brands"].get(brand)
        if not item:
            raise LookupError(f"Unknown brand fixture: {brand}")
        value = item.get(metric)
        return {
            "source": "cache",
            "tool": "get_brand_metric",
            "summary_text": f"{brand}의 {period} {metric} 값은 {value}입니다.",
            "render_data": {"brand": brand, "metric": metric, "period": period, "value": value, **item},
        }

