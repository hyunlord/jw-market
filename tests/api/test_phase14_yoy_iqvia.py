import json
import urllib.parse
import urllib.request


def get_api(path):
    with urllib.request.urlopen(f"http://127.0.0.1:8013{path}") as r:
        return json.load(r)


def test_market_size_series_entries_include_yoy_growth_pct():
    """A.1 frontend reads YoY from each market_size_series point."""
    brand = urllib.parse.quote("뉴트로진")
    api = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")
    series = api["data"]["sources_data"]["market_size_series"]

    assert series
    assert all("yoy_growth_pct" in point for point in series)
    non_null = [point["yoy_growth_pct"] for point in series if point["yoy_growth_pct"] is not None]
    assert len(non_null) >= len(series) - 5


def test_iqvia_recent_quarters_are_not_artificially_low_from_duplicate_history():
    """Recent IQVIA quarters should not look like a collapse caused by duplicate older files."""
    brand = urllib.parse.quote("뉴트로진")
    api = get_api(f"/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales")
    series = api["data"]["sources_data"]["market_size_series"]
    values = [point["value"] for point in series if point.get("value")]

    assert len(values) >= 8
    baseline = values[-8:-2]
    recent = values[-2:]
    baseline_avg = sum(baseline) / len(baseline)
    for value in recent:
        assert value >= baseline_avg * 0.5
