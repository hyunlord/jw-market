from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HTML_PATH = ROOT / "docs/reference/jw_market_hardcoded_mockup_v3_4.html"


def html() -> str:
    return HTML_PATH.read_text()


def test_v3_4_no_static_forecast_or_simulation_numbers() -> None:
    """Issue 13: simulation/forecast cannot show the old mock numbers without backend data."""
    source = html()

    banned_literals = [
        "482억",
        "372억",
        "298억",
        "48백만원",
        "신뢰도 78",
        "960<span",
        "341<span",
        "233<span",
        "+15.3% YoY",
        "-13.1%p",
        "+12.1%",
    ]
    for literal in banned_literals:
        assert literal not in source


def test_v3_4_forecast_chart_requires_backend_forecast_values() -> None:
    source = html()

    assert "hasForecastValues" in source
    assert "예측 데이터 없음" in source
    assert "예측 데이터가 아직 구현되지 않았습니다" in source
    assert "if (!hasForecastValues)" in source


def test_v3_4_simulation_ci_has_no_numeric_default() -> None:
    source = html()

    assert "|| 0.95" not in source
    assert "const ciLevel = simBrand.horizon_ci_levels?.[horizonKey];" in source


def test_v3_4_c1_timeseries_uses_raw_value_series() -> None:
    source = html()
    match = re.search(
        r"function renderAnalysisLevels\(.*?function renderCompanyRanking",
        source,
        flags=re.DOTALL,
    )
    assert match, "renderAnalysisLevels block not found"
    block = match.group(0)

    assert "const seriesKey = 'value_series';" in block
    assert "series_pct" not in block
    assert ".toLocaleString('ko-KR')" in block
