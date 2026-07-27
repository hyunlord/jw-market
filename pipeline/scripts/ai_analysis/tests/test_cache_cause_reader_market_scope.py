"""cache_cause reads must be scoped to the market the caller is asking about.

The producer writes one row per market. The reader used to match four of the
five key columns and take ``LIMIT 1``, so for a brand present in two markets it
could answer with the other market's KPIs. These tests hold the line on the two
properties that matter: the right market wins, and a miss stays a miss.
"""

from __future__ import annotations

import json

import pytest

from bundle_builder import market_view_builder
from bundle_builder.cache_cause_key import cache_market_id
from bundle_builder.ms_recomputer import (
    _cache_row,
    get_kpi_extras_from_cache_cause,
    get_ms_from_cache_cause,
)


def _payload(ms_pct, ei):
    return json.dumps({"data": {"kpi": {"ms_pct": ms_pct, "ei": ei}}}, ensure_ascii=False)


class FakeCursor:
    """Only honours market_id when the query actually constrains it."""

    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        text = " ".join(sql.split())
        self.conn.queries.append(text)
        scoped = "market_id = %s" in text
        brand, view, source, measure = params[0], params[1], params[2], params[3]
        market = params[4] if scoped else None
        matches = [
            row
            for row in self.conn.rows
            if row["brand"] == brand
            and row["view_type"] == view
            and row["source"] == source
            and row["measure"] == measure
            and (market is None or row["market_id"] == market)
        ]
        if "SELECT 1" in text:
            self.rows = [{"1": 1}] if matches else []
        else:
            self.rows = [{"response_json": row["response_json"]} for row in matches]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def cursor(self):
        return FakeCursor(self)


def _two_market_rows():
    """One competitor brand cached in both of its markets, different KPIs."""

    return [
        {
            "brand": "건카베딜",
            "view_type": "market_landscape",
            "source": "UBIST",
            "measure": "sales",
            "market_id": "strategy_005",
            "response_json": _payload(3.5, 1.10),
        },
        {
            "brand": "건카베딜",
            "view_type": "market_landscape",
            "source": "UBIST",
            "measure": "sales",
            "market_id": "strategy_008",
            "response_json": _payload(0.8, 0.42),
        },
    ]


@pytest.mark.parametrize(
    "ml_id, expected_ms, expected_ei",
    [("ml_005", 3.5, 1.10), ("ml_008", 0.8, 0.42)],
)
def test_reader_returns_the_requested_market(ml_id, expected_ms, expected_ei):
    conn = FakeConn(_two_market_rows())
    market = cache_market_id("market_landscape", ml_id)

    assert get_ms_from_cache_cause("건카베딜", "market_landscape", "UBIST", "sales", conn, market) == expected_ms
    extras = get_kpi_extras_from_cache_cause(
        "건카베딜", "market_landscape", "UBIST", "sales", conn, market
    )
    assert extras["ei"] == expected_ei


# ---------------------------------------------- fault injection (3) ----------


def test_dropping_market_id_from_the_query_returns_the_wrong_market():
    """(3) Remove the market constraint and the consistency contract fails."""

    conn = FakeConn(_two_market_rows())

    # Simulate the pre-fix query by handing the fake a market-blind statement.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT response_json FROM cache_cause WHERE brand = %s AND view_type = %s "
            "AND source = %s AND measure = %s LIMIT 1",
            ("건카베딜", "market_landscape", "UBIST", "sales"),
        )
        unscoped = json.loads(cur.fetchone()["response_json"])

    scoped = _cache_row(
        "건카베딜", "market_landscape", "UBIST", "sales", conn, cache_market_id("market_landscape", "ml_008")
    )

    # The market-blind read hands back ml_005's numbers for an ml_008 question.
    assert unscoped["data"]["kpi"]["ms_pct"] == 3.5
    assert scoped["data"]["kpi"]["ms_pct"] == 0.8


def test_reader_refuses_to_run_without_a_market():
    conn = FakeConn(_two_market_rows())

    with pytest.raises(ValueError, match="market_id is required"):
        _cache_row("건카베딜", "market_landscape", "UBIST", "sales", conn)


def test_cache_exists_refuses_to_run_without_a_market():
    conn = FakeConn(_two_market_rows())

    with pytest.raises(ValueError, match="cache_market is required"):
        market_view_builder._cache_exists("건카베딜", "market_landscape", "UBIST", "sales", conn)


# ------------------------------------------------------------ miss contract --


def test_a_miss_is_a_miss_and_never_borrows_another_market():
    """The contract that makes exact matching safe."""

    conn = FakeConn(_two_market_rows())
    absent = cache_market_id("market_landscape", "ml_012")

    assert _cache_row("건카베딜", "market_landscape", "UBIST", "sales", conn, absent) is None
    assert (
        market_view_builder._cache_exists(
            "건카베딜", "market_landscape", "UBIST", "sales", conn, absent
        )
        is False
    )


def test_miss_yields_all_none_extras_rather_than_another_market_values():
    conn = FakeConn(_two_market_rows())
    absent = cache_market_id("market_landscape", "ml_012")

    extras = get_kpi_extras_from_cache_cause(
        "건카베딜", "market_landscape", "UBIST", "sales", conn, absent
    )

    assert set(extras) == {
        "ei",
        "ei_basis",
        "ei_period_years",
        "ei_note",
        "brand_cagr_5y_pct",
        "market_cagr_5y_pct",
        "momentum_score",
        "target_rank",
        "total_brands_in_market",
        "market_avg_ms_pct",
    }
    assert all(value is None for value in extras.values())


def test_every_cache_query_the_reader_issues_is_market_scoped():
    conn = FakeConn(_two_market_rows())
    market = cache_market_id("market_landscape", "ml_005")

    market_view_builder._cache_exists("건카베딜", "market_landscape", "UBIST", "sales", conn, market)
    get_kpi_extras_from_cache_cause("건카베딜", "market_landscape", "UBIST", "sales", conn, market)

    assert conn.queries
    assert all("market_id = %s" in query for query in conn.queries)
