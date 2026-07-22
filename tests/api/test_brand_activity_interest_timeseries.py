from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api import brand_activity_interest_timeseries as service
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetResolution
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice, BrandMeta, ViewConfig


def _brand_set(view_name: str = "general") -> BrandSetResolution:
    cfg = {
        "general": ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False),
        "strategic_ml": ViewConfig("mart_strategic_ml_brand_metric", "mart_strategic_ml_market_metric", "ml_id", "ml_name", "brand_ranking_stacked", True),
        "strategic_cd": ViewConfig("mart_strategic_cd_brand_metric", "mart_strategic_cd_market_metric", "cd_market_id", "cd_market_name", "brand_ranking_stacked", True),
    }[view_name]
    market_id = {"general": "C10A1", "strategic_ml": "ml_006", "strategic_cd": "cd_006"}[view_name]
    return BrandSetResolution(
        view_name=view_name, market_id=market_id, selected_brand="리바로", view=cfg,
        market_row={cfg.market_key: market_id, cfg.market_name_column: "STATINS"},
        brand_rows=(),
        brand_meta={
            "리바로": BrandMeta("리바로", "리바로", ("LIVALO",), True),
            "크레스토": BrandMeta("크레스토", "크레스토", ("CRESTOR",), False),
        },
        choices=(BrandChoice("리바로", "리바로", 1, True), BrandChoice("크레스토", "크레스토", 2, False)),
        candidates=(), ranking_quarter="2025-Q4", applied_filter={"atc4": ["C10A1"]},
    )


@pytest.fixture(autouse=True)
def _stub_common(monkeypatch):
    monkeypatch.setattr(service, "_alias_lookup", lambda: {})
    monkeypatch.setattr(service, "_keyword_filter_domain",
                        lambda col: frozenset({"HOSPITAL", "PRIV. PRACTICE"}) if col == "visit_location" else frozenset({"Cardio", "IM/FM"}))
    monkeypatch.setattr(service, "resolve_brand_set", lambda **_k: _brand_set(_k.get("view_name", "general")))
    # keyword join uses IQVIA-reloaded codes by brand name; mock the reload from brand_meta.
    _meta = _brand_set().brand_meta
    monkeypatch.setattr(service, "iqvia_product_codes_by_brand",
                        lambda names: {k: tuple(_meta[k].product_codes) for k in names if k in _meta})
    # bounds: exactly 36 months 2023-06..2026-05
    monkeypatch.setattr(service.db, "fetch_one", lambda *_a, **_k: {"available_start": "2023-06", "available_end": "2026-05"})
    monkeypatch.setattr(
        service,
        "_cached_manufacturer_by_product",
        lambda: {
            "LIVALO": frozenset({"제이더블유중외제약"}),
            "CRESTOR": frozenset({"아스트라제네카"}),
            "LIPITOR": frozenset({"비아트리스"}),
            "LIPILOU": frozenset({"종근당"}),
        },
    )


def _rows(rows):
    return lambda *_a, **_k: list(rows)


def test_within_brand_month_pct_and_total_count(monkeypatch):
    monkeypatch.setattr(service.db, "fetch_all", _rows([
        {"product_name": "LIVALO", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 3},
        {"product_name": "LIVALO", "period_ym": "2026-05", "interest": "SOMEWHAT USEFUL", "event_count": 1},
        {"product_name": "CRESTOR", "period_ym": "2026-05", "interest": "NOT AT ALL", "event_count": 2},
    ]))
    out = service.get_interest_timeseries({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}})
    livalo = next(b for b in out["brands"] if b["brand_key"] == "리바로")["series"]["2026-05"]
    assert livalo["total_count"] == 4  # denominator = 3+1+0
    assert livalo["VERY USEFUL"] == {"count": 3, "pct": 75.0}
    assert livalo["SOMEWHAT USEFUL"] == {"count": 1, "pct": 25.0}
    assert livalo["NOT AT ALL"] == {"count": 0, "pct": 0.0}
    assert round(sum(livalo[l]["pct"] for l in service.INTEREST_LEVELS), 1) == 100.0
    crestor = next(b for b in out["brands"] if b["brand_key"] == "크레스토")["series"]["2026-05"]
    assert crestor["total_count"] == 2 and crestor["NOT AT ALL"]["pct"] == 100.0


def test_companies_axis_merges_sellers_by_manufacturer_and_preserves_natural_pct(monkeypatch):
    # Two LIVALO sellers collapse to the one IQVIA manufacturer. CRESTOR stays separate.
    monkeypatch.setattr(service.db, "fetch_all", _rows([
        {"product_name": "LIVALO", "representing_company": "JW PHARMACEUTICAL", "period_ym": "2026-05", "interest": "NOT AT ALL", "event_count": 1},
        {"product_name": "LIVALO", "representing_company": "JW PHARMACEUTICAL", "period_ym": "2026-05", "interest": "SOMEWHAT USEFUL", "event_count": 19},
        {"product_name": "LIVALO", "representing_company": "JW PHARMACEUTICAL", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 3},
        {"product_name": "LIVALO", "representing_company": "JW PHARMACEUTICAL", "period_ym": "2026-04", "interest": "SOMEWHAT USEFUL", "event_count": 10},
        {"product_name": "LIVALO", "representing_company": "JW SHINYAK", "period_ym": "2026-04", "interest": "SOMEWHAT USEFUL", "event_count": 8},
        {"product_name": "CRESTOR", "representing_company": "DAE WOONG", "period_ym": "2026-05", "interest": "SOMEWHAT USEFUL", "event_count": 8},
    ]))
    out = service.get_interest_timeseries({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}})
    companies = {c["company_name"]: c for c in out["companies"]}
    assert set(companies) == {"제이더블유중외제약", "아스트라제네카"}
    assert [c["company_name"] for c in out["companies"]] == ["제이더블유중외제약", "아스트라제네카"]
    jw_series = companies["제이더블유중외제약"]["series"]
    jw = jw_series["2026-05"]
    assert jw["total_count"] == 23
    assert jw["NOT AT ALL"] == {"count": 1, "pct": 4.3}
    assert jw["SOMEWHAT USEFUL"] == {"count": 19, "pct": 82.6}
    assert jw["VERY USEFUL"] == {"count": 3, "pct": 13.0}
    assert round(sum(jw[l]["pct"] for l in service.INTEREST_LEVELS), 1) == 99.9  # no forced 100
    assert jw_series["2026-04"]["total_count"] == 18
    assert jw_series["2023-06"] is None
    assert len(jw_series) == 36


def test_companies_use_null_manufacturer_without_dropping_rows(monkeypatch):
    monkeypatch.setattr(service, "_cached_manufacturer_by_product", lambda: {})
    monkeypatch.setattr(service.db, "fetch_all", _rows([
        {"product_name": "LIVALO", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 3},
    ]))
    out = service.get_interest_timeseries({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}})
    assert len(out["companies"]) == 1
    assert out["companies"][0]["company_name"] is None
    assert out["companies"][0]["series"]["2026-05"]["total_count"] == 3
    assert any(b["series"]["2026-05"] for b in out["brands"])


def test_companies_denominator_moves_with_filter(monkeypatch):
    # Only in-window matched rows feed a company; a non-brand product is excluded from
    # the company set (companies come from the resolved brand rows only).
    monkeypatch.setattr(service.db, "fetch_all", _rows([
        {"product_name": "LIVALO", "representing_company": "JW PHARMACEUTICAL", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 5},
        {"product_name": "UNRELATED", "representing_company": "OTHER CO", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 99},
    ]))
    out = service.get_interest_timeseries({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}})
    assert [c["company_name"] for c in out["companies"]] == ["제이더블유중외제약"]
    assert out["companies"][0]["series"]["2026-05"]["total_count"] == 5


def test_company_manufacturer_aggregation_matches_5211_row_census() -> None:
    rows = [
        {"product_name": "LIVALO", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 3},
        {"product_name": "LIVALO", "period_ym": "2026-05", "interest": "SOMEWHAT USEFUL", "event_count": 19},
        {"product_name": "LIVALO", "period_ym": "2026-05", "interest": "NOT AT ALL", "event_count": 1},
        {"product_name": "LIVALO", "period_ym": "2023-06", "interest": "SOMEWHAT USEFUL", "event_count": 1306},
        {"product_name": "LIPITOR", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 15},
        {"product_name": "LIPITOR", "period_ym": "2026-05", "interest": "SOMEWHAT USEFUL", "event_count": 54},
        {"product_name": "LIPITOR", "period_ym": "2026-05", "interest": "NOT AT ALL", "event_count": 1},
        {"product_name": "LIPITOR", "period_ym": "2023-06", "interest": "SOMEWHAT USEFUL", "event_count": 2919},
        {"product_name": "CRESTOR", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 1},
        {"product_name": "CRESTOR", "period_ym": "2026-05", "interest": "SOMEWHAT USEFUL", "event_count": 18},
        {"product_name": "CRESTOR", "period_ym": "2023-06", "interest": "SOMEWHAT USEFUL", "event_count": 572},
        {"product_name": "LIPILOU", "period_ym": "2023-06", "interest": "SOMEWHAT USEFUL", "event_count": 302},
    ]
    code_sets = {
        "리바로": frozenset({"LIVALO"}),
        "리피토": frozenset({"LIPITOR"}),
        "크레스토": frozenset({"CRESTOR"}),
        "리피로우": frozenset({"LIPILOU"}),
    }

    counts, totals = service._counts_by_company_month(_brand_set(), rows, code_sets, {})

    assert totals == {
        "비아트리스": 2989,
        "제이더블유중외제약": 1329,
        "아스트라제네카": 591,
        "종근당": 302,
    }
    assert sum(totals.values()) == 5211
    period = service.PeriodWindow(
        "2023-06", "2026-05", "2023-06", "2026-05", tuple(service._month_range("2023-06", "2026-05")), True
    )
    may = {
        company: service._series_for_counts(by_month, period)["2026-05"]
        for company, by_month in counts.items()
    }
    assert may["비아트리스"] == {
        "total_count": 70,
        "VERY USEFUL": {"count": 15, "pct": 21.4},
        "SOMEWHAT USEFUL": {"count": 54, "pct": 77.1},
        "NOT AT ALL": {"count": 1, "pct": 1.4},
    }
    assert may["제이더블유중외제약"] == {
        "total_count": 23,
        "VERY USEFUL": {"count": 3, "pct": 13.0},
        "SOMEWHAT USEFUL": {"count": 19, "pct": 82.6},
        "NOT AT ALL": {"count": 1, "pct": 4.3},
    }
    assert may["아스트라제네카"] == {
        "total_count": 19,
        "VERY USEFUL": {"count": 1, "pct": 5.3},
        "SOMEWHAT USEFUL": {"count": 18, "pct": 94.7},
        "NOT AT ALL": {"count": 0, "pct": 0.0},
    }
    assert may["종근당"] is None


def test_missing_month_is_null(monkeypatch):
    monkeypatch.setattr(service.db, "fetch_all", _rows([
        {"product_name": "LIVALO", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 2},
    ]))
    out = service.get_interest_timeseries({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}})
    series = next(b for b in out["brands"] if b["brand_key"] == "리바로")["series"]
    assert series["2026-05"] is not None
    assert series["2026-04"] is None  # no data -> null, not zero-filled
    assert series["2023-06"] is None


def test_period_window_is_fixed_three_years(monkeypatch):
    monkeypatch.setattr(service.db, "fetch_all", _rows([]))
    out = service.get_interest_timeseries({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}})
    p = out["period"]
    assert p["start"] == "2023-06" and p["end"] == "2026-05"
    assert len(p["months"]) == 36 and p["full_window"] is True
    assert p["months"][0] == "2023-06" and p["months"][-1] == "2026-05"
    # every brand series spans exactly the 36 window months
    assert all(len(b["series"]) == 36 for b in out["brands"])


def test_window_shorter_than_three_years_flags_not_full(monkeypatch):
    monkeypatch.setattr(service.db, "fetch_one", lambda *_a, **_k: {"available_start": "2025-01", "available_end": "2026-05"})
    monkeypatch.setattr(service.db, "fetch_all", _rows([]))
    out = service.get_interest_timeseries({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}})
    p = out["period"]
    assert p["start"] == "2025-01" and p["end"] == "2026-05" and p["full_window"] is False
    assert len(p["months"]) == 17


def test_filter_in_clause_and_전체_is_all(monkeypatch):
    captured = {}
    def cap(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []
    monkeypatch.setattr(service.db, "fetch_all", cap)
    service.get_interest_timeseries({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]},
                                     "visit_location": ["HOSPITAL"], "specialty": ["Cardio", "IM/FM"]})
    assert "visit_location IN (%s)" in captured["sql"]
    assert "specialty IN (%s, %s)" in captured["sql"]
    assert "HOSPITAL" in captured["params"] and "Cardio" in captured["params"] and "IM/FM" in captured["params"]
    # 전체 -> no filter clause
    service.get_interest_timeseries({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]},
                                     "visit_location": "전체", "specialty": "전체"})
    assert "visit_location IN" not in captured["sql"] and "specialty IN" not in captured["sql"]


def test_unknown_filter_value_is_422(monkeypatch):
    monkeypatch.setattr(service.db, "fetch_all", _rows([]))
    with pytest.raises(service.InterestTimeseriesInputError) as ei:
        service.get_interest_timeseries({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]},
                                         "visit_location": "MARS_CLINIC"})
    assert ei.value.status_code == 422


@pytest.mark.parametrize("view,extra", [
    ("general", {"filters": {"atc4": ["C10A1"]}}),
    ("strategic_ml", {"market_id": "ml_006"}),
    ("strategic_cd", {"market_id": "cd_006"}),
])
def test_three_views_and_brand_set_matches_choices(monkeypatch, view, extra):
    monkeypatch.setattr(service.db, "fetch_all", _rows([
        {"product_name": "LIVALO", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 1},
    ]))
    out = service.get_interest_timeseries({"view": view, "selected_brand": "리바로", **extra})
    assert out["scope"]["view"] == view
    assert [b["brand_key"] for b in out["brands"]] == ["리바로", "크레스토"]  # == resolve_brand_set choices
    assert out["levels"] == list(service.INTEREST_LEVELS)


def test_deterministic_repeated_calls(monkeypatch):
    monkeypatch.setattr(service.db, "fetch_all", _rows([
        {"product_name": "LIVALO", "period_ym": "2026-05", "interest": "VERY USEFUL", "event_count": 3},
        {"product_name": "LIVALO", "period_ym": "2025-11", "interest": "SOMEWHAT USEFUL", "event_count": 2},
    ]))
    req = {"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}}
    hashes = {json.dumps(service.get_interest_timeseries(dict(req)), ensure_ascii=False, sort_keys=True) for _ in range(5)}
    assert len(hashes) == 1
