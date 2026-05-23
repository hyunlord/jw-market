import json
import math
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

import pymysql


ROOT = Path(__file__).resolve().parents[2]
FRONTEND_HTML = ROOT / "docs/reference/jw_market_hardcoded_mockup_v3_4.html"


def get_api(path):
    with urllib.request.urlopen(f"http://127.0.0.1:8013{path}") as r:
        return json.load(r)


def _latest_metric_history_value(metric_history):
    if not metric_history:
        return None
    if isinstance(metric_history, str):
        metric_history = json.loads(metric_history)
    if isinstance(metric_history, dict):
        items = sorted(metric_history.items())
        if not items:
            return None
        latest = items[-1][1]
        if isinstance(latest, dict):
            for key in ("raw_value", "value", "sales", "sales_krw", "metric_value"):
                if latest.get(key) is not None:
                    return float(latest[key])
            return None
        return float(latest)
    if isinstance(metric_history, list):
        latest = metric_history[-1]
        if isinstance(latest, dict):
            for key in ("raw_value", "value", "sales", "sales_krw", "metric_value"):
                if latest.get(key) is not None:
                    return float(latest[key])
        return float(latest)
    return None


def test_hhi_matches_all_brand_manual_calculation():
    """HHI must be calculated from every brand in the market, not displayed top 6."""
    brand = urllib.parse.quote("가드메트")
    api = get_api(f"/api/cause/{brand}?view=market_landscape&source=UBIST&measure=sales")
    api_hhi = (
        api["data"]["kpi"].get("hhi")
        or api["data"]["kpi"].get("hhi_current")
        or api["data"]["kpi"].get("hhi_recent")
        or api["data"]["sources_data"].get("hhi_recent")
    )
    assert api_hhi is not None

    conn = pymysql.connect(
        host="localhost",
        port=3308,
        user="root",
        password=os.getenv("JW_MART_DB_PASSWORD", "root"),
        database="jw_mart",
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT metric_history
                FROM mart_strategic_ml_brand_metric
                WHERE ml_id='ml_003' AND source='UBIST' AND measure='sales'
                """
            )
            values = [
                v
                for row in cur.fetchall()
                if (v := _latest_metric_history_value(row["metric_history"])) is not None
            ]
    finally:
        conn.close()

    total = sum(values)
    manual_all_hhi = sum((v / total * 100) ** 2 for v in values)
    manual_top6_hhi = sum((v / total * 100) ** 2 for v in sorted(values, reverse=True)[:6])

    assert len(values) >= 100
    assert math.isclose(api_hhi, manual_all_hhi, abs_tol=0.1)
    assert not math.isclose(api_hhi, manual_top6_hhi, abs_tol=5)


def test_company_ranking_has_raw_value_field():
    """A.4 company ranking tooltip must have a raw numeric value source."""
    brand = urllib.parse.quote("악템라")
    api = get_api(f"/api/cause/{brand}?view=market_landscape&source=IQVIA&measure=sales")
    yearly = api["data"]["company_ranking_stacked"].get("yearly", [])
    assert len(yearly) >= 3

    for year in yearly:
        for row in year.get("rankings", []):
            assert "value" in row, f"{row.get('company')}: value field missing"
            assert isinstance(row["value"], (int, float)), f"{row.get('company')}: value is not numeric"


def test_frontend_has_no_residual_unit_conversions():
    """v3.4 must render raw numbers: no 억/천/만/조 display conversion or 1e8/1e4 scaling."""
    html = FRONTEND_HTML.read_text()

    forbidden_patterns = {
        "1e8 scaling": r"/\s*1e8",
        "1e4 scaling": r"/\s*1e4",
        "won symbol": r"₩",
        "billion suffix literal": r"['\"`]\s*억\s*['\"`]| 억",
        "thousand suffix literal": r"['\"`]\s*천\s*['\"`]| 천",
        "trillion suffix": r"조원|['\"`]조['\"`]",
        "million won suffix": r"백만원",
        "KRW label": r"KRW",
    }
    offenders = {
        label: re.findall(pattern, html)
        for label, pattern in forbidden_patterns.items()
        if re.findall(pattern, html)
    }
    assert offenders == {}


def test_frontend_company_tooltip_uses_value_not_sales_krw_scaling():
    html = FRONTEND_HTML.read_text()
    company_start = html.index("function renderCompanyRanking")
    company_end = html.index("function renderGrowthMsMatrix")
    company_block = html[company_start:company_end]

    assert "r.value" in company_block
    assert "sales_krw / 1e8" not in company_block
    assert " 억" not in company_block
