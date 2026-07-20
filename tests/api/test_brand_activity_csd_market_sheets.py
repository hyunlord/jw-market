"""Contract for the CSD ChannelDynamics market axis = workbook Market *sheet*.

The brand-activity Channel Dynamics market dropdown must be driven by the CSD
Market sheets a brand belongs to (the ``market``/``source_sheet`` axis of
``csd_channel_dynamics_stage``), not by the general-view ATC4 dimension.  A brand
bound to multiple sheets must surface all of them as selectable options instead
of collapsing to one and raising ``csd_market_ambiguous``.
"""

from __future__ import annotations

import csv
from pathlib import Path

from pipeline.scripts.api.brand_activity_csd_shared import (
    CsdCrosswalk,
    csd_markets_for_products,
)
from pipeline.scripts.api.brand_activity_csd_timeseries import _select_active_markets


def _rows(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"market": market, "master_product": product} for market, product in pairs]


# --- csd_markets_for_products: brand -> belonging CSD Market sheets -----------


def test_single_sheet_brand_returns_one_market() -> None:
    rows = _rows(("LIVALO Market", "LIVALO"), ("LIVALOZET Market", "LIVALOZET"))

    markets = csd_markets_for_products({"LIVALO"}, rows)

    assert [m.market for m in markets] == ["LIVALO Market"]
    assert markets[0].display_market == "LIVALO"  # " Market" suffix stripped for label


def test_multi_sheet_brand_returns_all_bound_sheets_ranked_by_overlap() -> None:
    rows = _rows(
        ("GUARDLET Market", "GUARDMET"),
        ("GUARDLET Market", "TENELIA"),
        ("GANAKHAN Market", "GANAKHAN"),
    )

    # A brand whose products span two sheets (2 in GUARDLET, 1 in GANAKHAN).
    markets = csd_markets_for_products({"GUARDMET", "TENELIA", "GANAKHAN"}, rows)

    assert [m.market for m in markets] == ["GUARDLET Market", "GANAKHAN Market"]  # higher overlap first
    assert markets[0].score == 2 and markets[1].score == 1


def test_tie_returns_both_sheets_without_error_sorted_by_market() -> None:
    rows = _rows(("B Market", "PRODB"), ("A Market", "PRODA"))

    markets = csd_markets_for_products({"PRODA", "PRODB"}, rows)

    # No ambiguity error: both are returned, deterministic order by market name.
    assert [m.market for m in markets] == ["A Market", "B Market"]


def test_no_overlap_returns_empty() -> None:
    rows = _rows(("LIVALO Market", "LIVALO"))

    assert csd_markets_for_products({"UNRELATED"}, rows) == ()


# --- selection: request csd_market wins; default is all sheets ("시장 전체") ---


def _avail(*markets: str) -> tuple[CsdCrosswalk, ...]:
    return tuple(
        CsdCrosswalk(market=market, display_market=market.removesuffix(" Market"), overlap=("x",), score=1)
        for market in markets
    )


def test_specific_market_selection_narrows_to_one() -> None:
    available = _avail("GUARDLET Market", "GANAKHAN Market")

    active, selected = _select_active_markets(available, "GANAKHAN Market")

    assert [m.market for m in active] == ["GANAKHAN Market"]
    assert selected == "GANAKHAN Market"


def test_selection_accepts_display_label() -> None:
    available = _avail("GUARDLET Market", "GANAKHAN Market")

    active, selected = _select_active_markets(available, "GANAKHAN")  # sheet-derived label

    assert [m.market for m in active] == ["GANAKHAN Market"]
    assert selected == "GANAKHAN Market"


def test_default_selection_is_all_sheets() -> None:
    available = _avail("GUARDLET Market", "GANAKHAN Market")

    for requested in (None, "", "전체", "ALL"):
        active, selected = _select_active_markets(available, requested)
        assert [m.market for m in active] == ["GUARDLET Market", "GANAKHAN Market"]
        assert selected is None


def test_unknown_selection_falls_back_to_all_sheets() -> None:
    available = _avail("GUARDLET Market", "GANAKHAN Market")

    active, selected = _select_active_markets(available, "C10A1")  # stray ATC4 code

    assert [m.market for m in active] == ["GUARDLET Market", "GANAKHAN Market"]
    assert selected is None


# --- real loaded CSD data: multi-sheet brands resolve to >1 sheet -------------


def test_real_stage_data_multi_sheet_brand_lists_all_sheets() -> None:
    csv_path = Path("output/brand_activity_csd/csd_channel_dynamics_stage.csv")
    if not csv_path.exists():
        import pytest

        pytest.skip("CSD stage CSV fixture not present")
    with csv_path.open(encoding="utf-8") as handle:
        rows = [{"market": r["market"], "master_product": r["master_product"]} for r in csv.DictReader(handle)]

    markets = csd_markets_for_products({"LIVALO", "LIVALOZET", "LIVALO V"}, rows)

    assert {m.market for m in markets} == {"LIVALO Market", "LIVALOZET Market", "LIVALO V Market"}
    # labels are sheet-derived, suffix stripped
    assert {m.display_market for m in markets} == {"LIVALO", "LIVALOZET", "LIVALO V"}


# --- end-to-end get_csd_timeseries for a multi-sheet brand (가드메트 case) -----


def _install_timeseries_db(monkeypatch, *, product_rows, activity_by_market):
    """Patch the service's brand-set + db so only CSD wiring is exercised."""

    from pipeline.scripts.api import brand_activity_csd_timeseries as service
    from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetResolution
    from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice, BrandMeta, ViewConfig

    view = ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    selected = BrandMeta("가드메트", "가드메트", ("GUARDMET", "TENELIA", "GANAKHAN"), True)
    resolution = BrandSetResolution(
        view_name="general",
        market_id="A10N1",
        selected_brand="가드메트",
        view=view,
        market_row={"atc4_code": "A10N1", "atc4_desc": "DPP4", "market_size_series": {}, "brand_ranking": {}},
        brand_rows=(),
        brand_meta={"가드메트": selected},
        choices=(BrandChoice("가드메트", "가드메트", 1, True),),
        candidates=(),
        ranking_quarter="2025-Q1",
        applied_filter={},
    )
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: resolution)

    months = [{"period_ym": f"2025-{month:02d}"} for month in (1, 2, 3)]

    def fetch_all(sql, params=None):
        squashed = " ".join(sql.split())
        if "SUM(product_details)" in squashed:  # _sql_csd_activity, params = queried markets
            rows: list[dict] = []
            for market in params or ():
                rows.extend(activity_by_market.get(market, []))
            return rows
        if "GROUP BY market, master_product" in squashed:  # _sql_csd_products
            return list(product_rows)
        if "DISTINCT period_ym" in squashed:  # _sql_csd_months
            return months
        return []  # rx rows, market totals

    monkeypatch.setattr(service.db, "fetch_all", fetch_all)
    return service


def _multi_sheet_fixture():
    product_rows = [
        {"market": "GUARDLET Market", "master_product": "GUARDMET"},
        {"market": "GUARDLET Market", "master_product": "TENELIA"},
        {"market": "GANAKHAN Market", "master_product": "GANAKHAN"},
    ]
    activity_by_market = {
        "GUARDLET Market": [{"period_ym": f"2025-{m:02d}", "master_product": "GUARDMET", "value": 10} for m in (1, 2, 3)],
        "GANAKHAN Market": [{"period_ym": f"2025-{m:02d}", "master_product": "GANAKHAN", "value": 4} for m in (1, 2, 3)],
    }
    return product_rows, activity_by_market


def test_timeseries_lists_all_sheets_and_defaults_to_all(monkeypatch) -> None:
    product_rows, activity_by_market = _multi_sheet_fixture()
    service = _install_timeseries_db(monkeypatch, product_rows=product_rows, activity_by_market=activity_by_market)

    result = service.get_csd_timeseries({"view": "general", "market_id": "A10N1", "selected_brand": "가드메트"})

    assert result is not None  # no csd_market_ambiguous blank-out
    scope = result["scope"]
    assert [m["market"] for m in scope["csd_markets"]] == ["GUARDLET Market", "GANAKHAN Market"]
    assert [m["label"] for m in scope["csd_markets"]] == ["GUARDLET", "GANAKHAN"]
    assert scope["selected_csd_market"] is None  # 시장 전체 default
    # all sheets aggregated: (10 + 4) * 3 months
    assert result["brands"][0]["series"]["activity"]["absolute"]["2025-Q1"] == 42.0


def test_timeseries_selection_narrows_aggregation_to_one_sheet(monkeypatch) -> None:
    product_rows, activity_by_market = _multi_sheet_fixture()
    service = _install_timeseries_db(monkeypatch, product_rows=product_rows, activity_by_market=activity_by_market)

    result = service.get_csd_timeseries(
        {"view": "general", "market_id": "A10N1", "selected_brand": "가드메트", "csd_market": "GANAKHAN Market"}
    )

    scope = result["scope"]
    assert scope["selected_csd_market"] == "GANAKHAN Market"
    # only the GANAKHAN sheet: 4 * 3 months
    assert result["brands"][0]["series"]["activity"]["absolute"]["2025-Q1"] == 12.0


# --- MI Master market grouping: sibling brands in separate sheets (리바로 case) --
#
# The strategic (MI Master) market ml_006 holds BOTH 리바로 (IQVIA product LIVALO)
# and 리바로젯 (LIVALOZET), which live in two *separate* workbook Market sheets.
# Scoping the CSD axis to the selected brand's own product codes dropped the sibling
# sheet, so the competitor 리바로젯 surfaced with zero channel activity.  The market
# axis must be the union of the resolved market's member brands.


def _order_selected_first_helper():
    from pipeline.scripts.api.brand_activity_csd_timeseries import _order_selected_first

    return _order_selected_first


def _install_family_timeseries_db(
    monkeypatch, *, selected_brand, members, product_rows, activity_by_market
):
    """Patch brand-set + db for a *multi-brand* market (production-shaped).

    ``members`` maps ``brand_key -> product_codes`` for every brand in the resolved
    market; each brand owns its own single IQVIA product code (unlike the 가드메트
    fixture where one brand owned three sheets' products, an operationally
    impossible condition).
    """

    from pipeline.scripts.api import brand_activity_csd_timeseries as service
    from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetResolution
    from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice, BrandMeta, ViewConfig

    view = ViewConfig(
        "mart_strategic_ml_brand_metric", "mart_strategic_ml_market_metric", "ml_id", "ml_name", "brand_ranking_stacked", True
    )
    brand_meta = {
        key: BrandMeta(key, key, tuple(codes), True) for key, codes in members.items()
    }
    # selected brand leads; remaining members follow (sales-ranked in production).
    ordered_keys = [selected_brand] + [key for key in members if key != selected_brand]
    choices = tuple(
        BrandChoice(key, key, rank + 1, key == selected_brand)
        for rank, key in enumerate(ordered_keys)
    )
    resolution = BrandSetResolution(
        view_name="strategic_ml",
        market_id="ml_006",
        selected_brand=selected_brand,
        view=view,
        market_row={"ml_id": "ml_006", "ml_name": "리바로 패밀리", "market_size_series": {}, "brand_ranking_stacked": {}},
        brand_rows=(),
        brand_meta=brand_meta,
        choices=choices,
        candidates=(),
        ranking_quarter="2025-Q1",
        applied_filter={},
    )
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_kwargs: resolution)

    months = [{"period_ym": f"2025-{month:02d}"} for month in (1, 2, 3)]

    def fetch_all(sql, params=None):
        squashed = " ".join(sql.split())
        if "SUM(product_details)" in squashed:  # _sql_csd_activity, params = queried markets
            rows: list[dict] = []
            for market in params or ():
                rows.extend(activity_by_market.get(market, []))
            return rows
        if "GROUP BY market, master_product" in squashed:  # _sql_csd_products
            return list(product_rows)
        if "DISTINCT period_ym" in squashed:  # _sql_csd_months
            return months
        return []  # rx rows, market totals

    monkeypatch.setattr(service.db, "fetch_all", fetch_all)
    return service


def _livalo_family_fixture():
    """리바로 (LIVALO) + 리바로젯 (LIVALOZET): two brands, two sheets, one market.

    Each brand owns exactly its own IQVIA product code (matching the real mart
    ``by_dimension`` where 리바로 -> ["LIVALO"], 리바로젯 -> ["LIVALOZET"]); the two
    products live in two distinct workbook Market sheets.
    """

    members = {"리바로": ("LIVALO",), "리바로젯": ("LIVALOZET",)}
    product_rows = [
        {"market": "LIVALO Market", "master_product": "LIVALO"},
        {"market": "LIVALOZET Market", "master_product": "LIVALOZET"},
    ]
    activity_by_market = {
        "LIVALO Market": [{"period_ym": f"2025-{m:02d}", "master_product": "LIVALO", "value": 100} for m in (1, 2, 3)],
        "LIVALOZET Market": [{"period_ym": f"2025-{m:02d}", "master_product": "LIVALOZET", "value": 60} for m in (1, 2, 3)],
    }
    return members, product_rows, activity_by_market


def test_family_market_lists_both_sibling_sheets(monkeypatch) -> None:
    members, product_rows, activity_by_market = _livalo_family_fixture()
    service = _install_family_timeseries_db(
        monkeypatch, selected_brand="리바로", members=members, product_rows=product_rows, activity_by_market=activity_by_market
    )

    result = service.get_csd_timeseries({"view": "strategic_ml", "market_id": "ml_006", "selected_brand": "리바로"})

    scope = result["scope"]
    # both sibling sheets present — the sibling is NOT dropped.
    assert {m["market"] for m in scope["csd_markets"]} == {"LIVALO Market", "LIVALOZET Market"}
    assert len(scope["csd_markets"]) == 2
    assert scope["selected_csd_market"] is None  # 시장 전체 default


def test_family_keeps_legacy_selected_market_as_primary(monkeypatch) -> None:
    """Selected brand's own sheet stays primary even when a sibling scores higher.

    A generic combo brand (뉴바로젯) adds a second product to the LIVALOZET sheet, so
    the LIVALOZET sheet's overlap-with-the-market (2) outranks the LIVALO sheet (1).
    Pure score-sort would surface LIVALOZET first; the selected-primary rule must
    keep the selected 리바로's own LIVALO sheet leading.
    """

    members = {"리바로": ("LIVALO",), "리바로젯": ("LIVALOZET",), "뉴바로젯": ("NEWBAROZET",)}
    product_rows = [
        {"market": "LIVALO Market", "master_product": "LIVALO"},
        {"market": "LIVALOZET Market", "master_product": "LIVALOZET"},
        {"market": "LIVALOZET Market", "master_product": "NEWBAROZET"},
    ]
    activity_by_market = {
        "LIVALO Market": [{"period_ym": f"2025-{m:02d}", "master_product": "LIVALO", "value": 100} for m in (1, 2, 3)],
        "LIVALOZET Market": [{"period_ym": f"2025-{m:02d}", "master_product": "LIVALOZET", "value": 60} for m in (1, 2, 3)],
    }
    service = _install_family_timeseries_db(
        monkeypatch, selected_brand="리바로", members=members, product_rows=product_rows, activity_by_market=activity_by_market
    )

    result = service.get_csd_timeseries({"view": "strategic_ml", "market_id": "ml_006", "selected_brand": "리바로"})

    scope = result["scope"]
    labels = [m["market"] for m in scope["csd_markets"]]
    assert set(labels) == {"LIVALO Market", "LIVALOZET Market"}
    # LIVALOZET Market has the higher overlap score, but LIVALO (selected) leads.
    assert labels[0] == "LIVALO Market"
    assert scope["csd_market"] == "LIVALO"  # backward-compatible primary label unchanged


def test_family_sibling_competitor_gets_nonzero_activity(monkeypatch) -> None:
    """Regression guard: 리바로젯 must carry real channel activity, not zero.

    Before the fix the axis was scoped to 리바로's own codes → only LIVALO Market
    was queried → the competitor 리바로젯 (LIVALOZET Market) matched nothing and
    surfaced with a flat-zero series.
    """

    members, product_rows, activity_by_market = _livalo_family_fixture()
    service = _install_family_timeseries_db(
        monkeypatch, selected_brand="리바로", members=members, product_rows=product_rows, activity_by_market=activity_by_market
    )

    result = service.get_csd_timeseries({"view": "strategic_ml", "market_id": "ml_006", "selected_brand": "리바로"})

    by_key = {brand["brand_key"]: brand for brand in result["brands"]}
    assert by_key["리바로"]["series"]["activity"]["absolute"]["2025-Q1"] == 300.0  # 100 * 3 months
    # 리바로젯 now matches the LIVALOZET sheet instead of a zero series.
    assert by_key["리바로젯"]["series"]["activity"]["absolute"]["2025-Q1"] == 180.0  # 60 * 3 months
    assert by_key["리바로젯"]["csd_matched"] is True
    # 시장 전체 default aggregates both sheets: (100 + 60) * 3 months.
    assert result["brands"][0]["series"]["activity"]["ratio"]["2025-Q1"] == 100.0 * 300.0 / 480.0


def test_family_sheet_selection_narrows_to_one_sibling(monkeypatch) -> None:
    members, product_rows, activity_by_market = _livalo_family_fixture()
    service = _install_family_timeseries_db(
        monkeypatch, selected_brand="리바로", members=members, product_rows=product_rows, activity_by_market=activity_by_market
    )

    result = service.get_csd_timeseries(
        {"view": "strategic_ml", "market_id": "ml_006", "selected_brand": "리바로", "csd_market": "LIVALOZET Market"}
    )

    scope = result["scope"]
    assert scope["selected_csd_market"] == "LIVALOZET Market"
    by_key = {brand["brand_key"]: brand for brand in result["brands"]}
    # only LIVALOZET sheet aggregated: 리바로 has no LIVALOZET product → zero,
    # 리바로젯 carries the sheet's 60 * 3.
    assert by_key["리바로"]["series"]["activity"]["absolute"]["2025-Q1"] == 0.0
    assert by_key["리바로젯"]["series"]["activity"]["absolute"]["2025-Q1"] == 180.0


def test_single_market_brand_is_unchanged(monkeypatch) -> None:
    """Regression: a lone-member market still yields exactly one sheet, primary."""

    members = {"리바로": ("LIVALO",)}
    product_rows = [
        {"market": "LIVALO Market", "master_product": "LIVALO"},
        {"market": "LIVALOZET Market", "master_product": "LIVALOZET"},  # unrelated sheet
    ]
    activity_by_market = {
        "LIVALO Market": [{"period_ym": f"2025-{m:02d}", "master_product": "LIVALO", "value": 100} for m in (1, 2, 3)],
    }
    service = _install_family_timeseries_db(
        monkeypatch, selected_brand="리바로", members=members, product_rows=product_rows, activity_by_market=activity_by_market
    )

    result = service.get_csd_timeseries({"view": "strategic_ml", "market_id": "ml_006", "selected_brand": "리바로"})

    scope = result["scope"]
    assert [m["market"] for m in scope["csd_markets"]] == ["LIVALO Market"]  # sibling not pulled in
    assert scope["csd_market"] == "LIVALO"
    assert result["brands"][0]["series"]["activity"]["absolute"]["2025-Q1"] == 300.0


def test_order_selected_first_preserves_relative_order() -> None:
    _order_selected_first = _order_selected_first_helper()

    markets = (
        CsdCrosswalk(market="LIVALOZET Market", display_market="LIVALOZET", overlap=("LIVALOZET", "NEWBAROZET"), score=2),
        CsdCrosswalk(market="LIVALO Market", display_market="LIVALO", overlap=("LIVALO",), score=1),
    )

    # Selected brand owns LIVALO only → its sheet leads despite the lower score.
    ordered = _order_selected_first(markets, {"LIVALO"})
    assert [m.market for m in ordered] == ["LIVALO Market", "LIVALOZET Market"]

    # Empty selected set → no reordering.
    assert _order_selected_first(markets, set()) == markets
