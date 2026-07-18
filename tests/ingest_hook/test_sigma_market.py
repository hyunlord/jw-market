"""Market Σ pin: Σ brand raw_value == market_size_series per atc4×period."""
from __future__ import annotations

import json
import sqlite3

import pytest

from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.sigma_market import MarketSigmaError, check_market_sigma


@pytest.fixture
def mart(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "mart.db"))
    conn.execute("CREATE TABLE mart_general_market_metric (atc4_code TEXT, source TEXT, measure TEXT, market_size_series TEXT)")
    conn.execute("CREATE TABLE mart_general_brand_metric (atc4_code TEXT, source TEXT, measure TEXT, metric_history TEXT)")

    def add_market(atc4, series):
        conn.execute(
            "INSERT INTO mart_general_market_metric VALUES (?, 'ubist', 'sales', ?)",
            (atc4, json.dumps(series)),
        )

    def add_brand(atc4, history):
        conn.execute(
            "INSERT INTO mart_general_brand_metric VALUES (?, 'ubist', 'sales', ?)",
            (atc4, json.dumps(history)),
        )

    add_market("C10C0", {"2026-06": 30.0, "2026-07": 100.0})
    add_brand("C10C0", {"2026-06": {"raw_value": 10.0}, "2026-07": {"raw_value": 60.0}})
    add_brand("C10C0", {"2026-06": {"raw_value": 20.0}, "2026-07": {"raw_value": 40.0}})
    add_market("A10H5", {"2026-07": 5.0})
    add_brand("A10H5", {"2026-07": {"raw_value": 5.0}})
    conn.commit()
    return conn


def _check(conn, periods, **kwargs):
    return check_market_sigma(conn, source="ubist", periods=periods, mark="?", **kwargs)


def test_reconciled_load_passes(mart):
    report = _check(mart, ("2026-06", "2026-07"))
    assert report.markets_checked == 2
    assert report.cells_checked == 3
    assert report.worst_rel == 0.0


def test_broken_total_fails(mart):
    mart.execute(
        "UPDATE mart_general_market_metric SET market_size_series=? WHERE atc4_code='C10C0'",
        (json.dumps({"2026-07": 999.0}),),
    )
    with pytest.raises(MarketSigmaError, match="C10C0 2026-07"):
        _check(mart, ("2026-07",))


def test_period_never_loaded_fails_closed(mart):
    with pytest.raises(MarketSigmaError, match="never received"):
        _check(mart, ("2030-01",))


def test_market_whole_missing_for_loaded_period_fails(mart):
    mart.execute(
        "UPDATE mart_general_market_metric SET market_size_series=? WHERE atc4_code='A10H5'",
        (json.dumps({"2026-06": 1.0}),),
    )
    with pytest.raises(MarketSigmaError, match="market whole missing"):
        _check(mart, ("2026-07",))


def test_category_map_pins_sigma_sources():
    assert resolve_category("ubist").sigma_source == "ubist"
    assert resolve_category("iqvia").sigma_source == "iqvia_nsa"
    assert resolve_category("mimaster").sigma_source is None
    assert resolve_category("skeleton").sigma_source is None
