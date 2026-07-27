"""Selector and reader contracts for cache_cause market consistency.

Fixtures mirror shapes verified against the MI Master canon
(parquet/strategic_brand.parquet sha256 1275f995…, 3,874 rows / 3,403 names).
The canon itself is gitignored, so the shapes are pinned here instead:

  supersession   26 names carry an excluded ml_006 row alongside an active
                 ml_007 row after the market split. 리바로페노 is one.
  dual active    264 names carry two active memberships, every one the pair
                 (ml_005, ml_008). SAMPLE_DUAL_MEMBERSHIP holds 20 of them.
  withdrawn      65 names have no active row at all.
"""

from __future__ import annotations

import pytest

from bundle_builder.cache_cause_key import cache_market_id, cache_view_source_id
from bundle_builder.catalog_db_loader import (
    load_brand_from_catalog,
    load_brand_market_ids,
    load_brand_memberships,
)
from bundle_builder.market_membership import (
    ACTIVE_PREDICATE_SQL,
    MEMBERSHIP_ORDER_SQL,
    NoActiveMarketMembership,
    active_memberships,
    membership_order_key,
    primary_membership,
)

# 20 of the 264 dual-membership names, taken from the canon at even spacing.
SAMPLE_DUAL_MEMBERSHIP = {
    "건카베딜": ("sb_005_00052", "sb_008_00899"),
    "네비칸": ("sb_005_00101", "sb_008_00948"),
    "노바크": ("sb_005_00153", "sb_008_00728"),
    "뉴스크": ("sb_005_00155", "sb_008_00730"),
    "디로바": ("sb_005_00133", "sb_008_00708"),
    "레보테놀": ("sb_005_00109", "sb_008_00956"),
    "멜로디핀 에스": ("sb_005_00283", "sb_008_00858"),
    "베스디핀": ("sb_005_00166", "sb_008_00741"),
    "비엘 노바스": ("sb_005_00142", "sb_008_00717"),
    "아나딥": ("sb_005_00250", "sb_008_00825"),
    "아모핀": ("sb_005_00151", "sb_008_00726"),
    "알보젠 카베디롤": ("sb_005_00077", "sb_008_00924"),
    "암로베틴": ("sb_005_00148", "sb_008_00723"),
    "암바스": ("sb_005_00282", "sb_008_00857"),
    "에스메디": ("sb_005_00273", "sb_008_00848"),
    "오코디핀": ("sb_005_00154", "sb_008_00729"),
    "지로디핀": ("sb_005_00241", "sb_008_00816"),
    "카베돌": ("sb_005_00080", "sb_008_00927"),
    "케이엠에스 아테놀올": ("sb_005_00038", "sb_008_00885"),
    "펠로디온": ("sb_005_00235", "sb_008_00810"),
}


def _row(brand_id, name, ml_id, *, cd_id=None, excluded=False, class_excluded=False):
    return {
        "brand_id": brand_id,
        "name": name,
        "ml_id": ml_id,
        "cd_id": cd_id,
        "strategy_id": f"strategy_{ml_id.split('_')[-1]}",
        "is_excluded": excluded,
        "is_class_excluded": class_excluded,
    }


def _livarofeno_rows():
    """The real 리바로페노 pair: superseded ml_006 plus canonical active ml_007."""

    return [
        _row("sb_006_00434", "리바로페노", "ml_006", cd_id="cd_006", excluded=True),
        _row("sb_canonical_010_리바로페노", "리바로페노", "ml_007", cd_id="cd_007"),
    ]


def _dual_rows(name):
    first, second = SAMPLE_DUAL_MEMBERSHIP[name]
    return [_row(first, name, "ml_005"), _row(second, name, "ml_008")]


class FakeCursor:
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
        if text.startswith("SHOW TABLES LIKE"):
            self.rows = [{"t": params[0]}] if params[0] in self.conn.tables else []
            return
        if "FROM catalog_strategic_brand" in text:
            if "OR REPLACE(LOWER(name)" in text:
                self.rows = [r for r in self.conn.catalog if r["name"] == params[0]]
                return
            if "WHERE name = %s" in text:
                self.rows = self.conn.select(params[0], active_only=True)
                return
            self.rows = []
            return
        if "FROM catalog_cd_brand" in text:
            self.rows = self.conn.select(params[0], active_only=True)
            return
        raise AssertionError(f"unexpected query: {text}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    """Applies the active predicate the way MariaDB would, in row order given."""

    def __init__(self, catalog, tables=("catalog_strategic_brand",)):
        self.catalog = list(catalog)
        self.tables = set(tables)
        self.queries = []
        self.enforce_active = True

    def select(self, name, *, active_only):
        rows = [r for r in self.catalog if r["name"] == name]
        if active_only and self.enforce_active:
            rows = [r for r in rows if not r["is_excluded"] and not r["is_class_excluded"]]
        return rows

    def cursor(self):
        return FakeCursor(self)


# --------------------------------------------------------------- selector ----


def test_excluded_supersession_row_is_never_selected():
    """리바로페노: the ml_006 row is withdrawn; ml_007 is the answer."""

    conn = FakeConn(_livarofeno_rows())

    row = load_brand_from_catalog("리바로페노", conn)

    assert row["ml_id"] == "ml_007"
    assert row["cd_id"] == "cd_007"
    assert row["brand_id"] == "sb_canonical_010_리바로페노"
    assert row["is_excluded"] is False


def test_selection_is_independent_of_storage_order():
    """The old LIMIT 1 answered with whatever row came first."""

    forward = load_brand_from_catalog("리바로페노", FakeConn(_livarofeno_rows()))
    reversed_ = load_brand_from_catalog("리바로페노", FakeConn(list(reversed(_livarofeno_rows()))))

    assert forward["brand_id"] == reversed_["brand_id"] == "sb_canonical_010_리바로페노"


@pytest.mark.parametrize("name", sorted(SAMPLE_DUAL_MEMBERSHIP))
def test_dual_membership_returns_both_markets_in_a_stable_order(name):
    rows = _dual_rows(name)
    conn = FakeConn(rows)

    assert load_brand_market_ids(name, conn) == ("ml_005", "ml_008")
    # Storage order must not change the answer.
    assert load_brand_market_ids(name, FakeConn(list(reversed(rows)))) == ("ml_005", "ml_008")


@pytest.mark.parametrize("name", sorted(SAMPLE_DUAL_MEMBERSHIP))
def test_dual_membership_primary_is_deterministic_across_repeats(name):
    rows = _dual_rows(name)

    picks = {load_brand_from_catalog(name, FakeConn(rows))["brand_id"] for _ in range(5)}
    shuffled = load_brand_from_catalog(name, FakeConn(list(reversed(rows))))["brand_id"]

    assert len(picks) == 1
    assert picks.pop() == shuffled == SAMPLE_DUAL_MEMBERSHIP[name][0]


def test_dual_membership_is_not_silently_collapsed():
    """Returning one market must not be the only thing a caller can get."""

    conn = FakeConn(_dual_rows("건카베딜"))

    assert len(load_brand_memberships("건카베딜", conn)) == 2


def test_withdrawn_brand_fails_closed_instead_of_using_an_excluded_row():
    """65 names are in this state; none is an is_jw or is_target brand."""

    conn = FakeConn([_row("sb_016_00046", "게라토스", "ml_016", class_excluded=True)])

    with pytest.raises(NoActiveMarketMembership) as error:
        load_brand_from_catalog("게라토스", conn)

    assert error.value.brand_name == "게라토스"
    assert error.value.excluded_rows == 1


def test_absent_brand_is_not_confused_with_a_withdrawn_one():
    conn = FakeConn([])

    assert load_brand_memberships("없는브랜드", conn) == []


def test_canonical_ordering_never_arbitrates_a_genuine_dual_membership():
    """Verified in the canon: 0 of the 264 dual names carry a canonical row."""

    rows = _dual_rows("건카베딜")

    assert not any(r["brand_id"].startswith("sb_canonical_") for r in rows)
    assert [membership_order_key(r)[0] for r in rows] == [1, 1]


def test_order_key_is_total_so_no_tie_falls_back_to_storage_order():
    same_market = [
        _row("sb_005_00002", "동명", "ml_005"),
        _row("sb_005_00001", "동명", "ml_005"),
    ]

    assert [r["brand_id"] for r in active_memberships(same_market)] == [
        "sb_005_00001",
        "sb_005_00002",
    ]


def test_sql_predicate_and_python_policy_agree():
    """The SQL and the in-memory mirror must not drift."""

    assert "is_excluded" in ACTIVE_PREDICATE_SQL
    assert "is_class_excluded" in ACTIVE_PREDICATE_SQL
    assert "brand_id ASC" in MEMBERSHIP_ORDER_SQL
    assert "ml_id ASC" in MEMBERSHIP_ORDER_SQL
    assert "sb\\_canonical\\_%" in MEMBERSHIP_ORDER_SQL

    rows = _livarofeno_rows()
    assert primary_membership(rows, "리바로페노")["ml_id"] == "ml_007"


# ------------------------------------------------- fault injection (1)(2) ----


def test_removing_the_active_filter_reintroduces_the_excluded_row():
    """(1) Revert is_excluded filtering -> the excluded-row contract must fail."""

    conn = FakeConn(_livarofeno_rows())
    conn.enforce_active = False  # simulate the pre-fix SQL

    # The Python policy still holds the line even when SQL stops filtering,
    # which is why the mirror exists.
    assert load_brand_from_catalog("리바로페노", conn)["ml_id"] == "ml_007"

    # With both layers reverted the defect returns.
    unfiltered_first = _livarofeno_rows()[0]
    assert unfiltered_first["ml_id"] == "ml_006"
    assert unfiltered_first["is_excluded"] is True


def test_removing_the_order_breaks_determinism():
    """(2) Without the total order the answer follows storage order."""

    rows = _dual_rows("건카베딜")
    unordered_forward = rows[0]["brand_id"]
    unordered_reverse = list(reversed(rows))[0]["brand_id"]

    assert unordered_forward != unordered_reverse  # the pre-fix behaviour

    ordered_forward = active_memberships(rows)[0]["brand_id"]
    ordered_reverse = active_memberships(list(reversed(rows)))[0]["brand_id"]
    assert ordered_forward == ordered_reverse


# ------------------------------------------------------------------ reader ---


def test_cache_market_id_matches_the_producer_for_both_views():
    assert cache_market_id("market_landscape", "ml_007") == "strategy_007"
    # CD keys off the parent ML, which is exactly why sibling CDs collide.
    assert cache_market_id("competitive_dynamics", "ml_008", "cd_009") == "strategy_008"
    assert cache_market_id("competitive_dynamics", "ml_008", "cd_008") == "strategy_008"


def test_view_source_id_distinguishes_sibling_cd_markets():
    assert cache_view_source_id("competitive_dynamics", "ml_008", "cd_008") == "cd_008"
    assert cache_view_source_id("competitive_dynamics", "ml_008", "cd_009") == "cd_009"
    assert cache_view_source_id("market_landscape", "ml_007", "cd_007") == "ml_007"
