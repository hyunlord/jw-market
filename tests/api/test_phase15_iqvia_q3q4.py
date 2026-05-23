import json
import urllib.parse
import urllib.request


def get_api(path):
    with urllib.request.urlopen(f"http://127.0.0.1:8013{path}") as r:
        return json.load(r)


def _period_value(series, period):
    for point in series:
        if point.get("period") == period:
            return point.get("value")
    return None


def test_iqvia_q3_not_halved_across_pl_markets():
    """IQVIA Q3 should not look halved because prior overlapping extracts remain duplicated."""
    for brand_name in ["제이클", "뉴트로진", "가드메트", "페린젝트"]:
        brand = urllib.parse.quote(brand_name)
        api = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")
        series = api["data"]["sources_data"]["market_size_series"]
        q1 = _period_value(series, "2025-Q1")
        q3 = _period_value(series, "2025-Q3")

        if q1 is None or q3 is None:
            continue
        assert q3 / q1 >= 0.70, f"{brand_name}: Q3/Q1={q3 / q1 * 100:.1f}%"


def test_yoy_growth_pct_remains_embedded_in_market_size_series():
    """Phase 14 contract: A.1 frontend reads YoY from each market_size_series point."""
    brand = urllib.parse.quote("제이클")
    api = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")
    series = api["data"]["sources_data"]["market_size_series"]

    assert series
    assert all("yoy_growth_pct" in point for point in series)
