"""Phase 5 cache_market_status KPI invariant and basis tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pymysql

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ETL_DIR = PROJECT_ROOT / "pipeline" / "scripts" / "etl"
if str(ETL_DIR) not in sys.path:
    sys.path.insert(0, str(ETL_DIR))

from build_cache_market_status import build_kpi, movement_pct_from_history  # noqa: E402


def _db() -> pymysql.connections.Connection:
    return pymysql.connect(
        host="127.0.0.1",
        port=3308,
        user="root",
        password="",
        database="jw_mart",
        charset="utf8mb4",
    )


def _current_payload() -> dict:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT response_json FROM cache_market_status WHERE query_key='default'")
            row = cur.fetchone()
    assert row is not None
    return json.loads(row[0])


def test_current_cache_market_status_kpi_invariant() -> None:
    """Each source reports only classified brands, so count equals rising plus declining."""
    payload = _current_payload()
    for source in ("ubist", "iqvia"):
        kpi = payload["kpi"][source]
        assert kpi["brand_count"] == kpi["rising_brand_count"] + kpi["declining_brand_count"]


def test_build_kpi_uses_ubist_recent_month_movement_and_counts_zero_as_rising() -> None:
    """UBIST uses the latest monthly movement, not YoY, and zero movement is rising."""
    rows = [
        {
            "source": "ubist",
            "measure": "sales",
            "metric_history": json.dumps(
                {
                    "2025-10": {"raw_value": 100, "ms": 10, "yoy": -99},
                    "2025-11": {"raw_value": 120, "ms": 12, "yoy": -99},
                }
            ),
            "extended_metric_history": json.dumps({"2025-11": {"cagr_5y": 1.5}}),
        },
        {
            "source": "ubist",
            "measure": "sales",
            "metric_history": json.dumps(
                {
                    "2025-10": {"raw_value": 200, "ms": 20, "yoy": 99},
                    "2025-11": {"raw_value": 180, "ms": 18, "yoy": 99},
                }
            ),
            "extended_metric_history": json.dumps({"2025-11": {"cagr_5y": -1.0}}),
        },
        {
            "source": "ubist",
            "measure": "sales",
            "metric_history": json.dumps(
                {
                    "2025-10": {"raw_value": 300, "ms": 30, "yoy": -99},
                    "2025-11": {"raw_value": 300, "ms": 30, "yoy": -99},
                }
            ),
            "extended_metric_history": json.dumps({"2025-11": {"cagr_5y": 0.0}}),
        },
    ]

    kpi = build_kpi("UBIST", rows)

    assert kpi["brand_count"] == 3
    assert kpi["rising_brand_count"] == 2
    assert kpi["declining_brand_count"] == 1


def test_build_kpi_uses_iqvia_recent_quarter_movement_and_excludes_null_basis() -> None:
    """IQVIA uses the latest quarterly movement and excludes rows with no comparable prior period."""
    rows = [
        {
            "source": "iqvia_nsa",
            "measure": "sales",
            "metric_history": json.dumps(
                {
                    "2025-Q3": {"raw_value": 100, "ms": 10, "yoy": -99},
                    "2025-Q4": {"raw_value": 110, "ms": 11, "yoy": -99},
                }
            ),
            "extended_metric_history": json.dumps({"2025-Q4": {"cagr_5y": 2.0}}),
        },
        {
            "source": "iqvia_nsa",
            "measure": "sales",
            "metric_history": json.dumps(
                {
                    "2025-Q3": {"raw_value": 200, "ms": 20, "yoy": 99},
                    "2025-Q4": {"raw_value": 100, "ms": 10, "yoy": 99},
                }
            ),
            "extended_metric_history": json.dumps({"2025-Q4": {"cagr_5y": -2.0}}),
        },
        {
            "source": "iqvia_nsa",
            "measure": "sales",
            "metric_history": json.dumps({"2025-Q4": {"raw_value": 100, "ms": 10, "yoy": 99}}),
            "extended_metric_history": json.dumps({"2025-Q4": {"cagr_5y": 0.0}}),
        },
    ]

    kpi = build_kpi("IQVIA", rows)

    assert kpi["brand_count"] == 2
    assert kpi["rising_brand_count"] == 1
    assert kpi["declining_brand_count"] == 1


def test_movement_pct_from_history_returns_none_when_prior_value_is_missing_or_zero() -> None:
    assert movement_pct_from_history({"2025-Q4": {"raw_value": 10}}) is None
    assert movement_pct_from_history(
        {"2025-Q3": {"raw_value": 0}, "2025-Q4": {"raw_value": 10}}
    ) is None
