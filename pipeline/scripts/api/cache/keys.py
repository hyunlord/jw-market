from __future__ import annotations


def cache_key_brands() -> str:
    return "brands_list"


def cache_key_market_status(period: str, top_n: int | None = None) -> str:
    if top_n is None or top_n == 20:
        return f"market_status:{period}"
    return f"market_status:{period}:top{top_n}"


def cache_key_cause(brand_name: str, view: str, source: str, measure: str, period: str) -> str:
    return f"cause:{brand_name}:{view}:{source}:{measure}:{period}"


def cache_key_deep_analysis(brand_name: str, period: str) -> str:
    return f"deep_analysis:{brand_name}:{period}"
