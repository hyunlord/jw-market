from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api import brand_activity_topic_matrix as topic_matrix
from pipeline.scripts.api import manufacturer_resolver
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetResolution
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice, BrandMeta, ViewConfig


@pytest.fixture(autouse=True)
def _reset_manufacturer_cache():
    """The product->manufacturer map is a long-lived module cache; reset it around each test
    so cache state never leaks across tests (order-independent, full-suite deterministic)."""
    manufacturer_resolver._manufacturer_cache = None
    yield
    manufacturer_resolver._manufacturer_cache = None


def test_post_topic_service_emits_event_count_from_assignment_rows(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _confidence_brand_set())
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})
    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", _confidence_fetch_all)

    payload = topic_matrix.get_topic_brand_payload({"view": "general", "selected_brand": "플라주오피", "filters": {"atc4": ["K01A3"]}})

    assert payload is not None
    brands = {brand["brand_key"]: brand for brand in payload["brands"]}
    assert brands["플라주오피"]["event_count"] == 1
    assert brands["엔커버"]["event_count"] == 4
    assert brands["가스모틴"]["event_count"] == 31
    assert brands["가나칸"]["event_count"] == 34
    assert brands["리바로브이"]["event_count"] == 50
    assert brands["리바로"]["event_count"] == 473
    assert brands["리피토"]["event_count"] == 990
    assert brands["미매칭"]["event_count"] == 0
    assert brands["미매칭"]["topics"] == []
    assert brands["플라주오피"]["topics"] == [{"rank": 1, "topic_id": "T01", "label": "수액", "share_pct": 100.0, "row_count": 1}]


def test_company_names_by_brand_joins_copromotion_and_nulls_unmapped(monkeypatch) -> None:
    view = ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    brand_set = BrandSetResolution(
        view_name="general",
        market_id="C10A1",
        selected_brand="리바로",
        view=view,
        market_row={"atc4_desc": "STATIN"},
        brand_rows=(),
        brand_meta={
            "리바로": BrandMeta("리바로", "리바로", ("LIVALO",), True),
            "미매칭": BrandMeta("미매칭", "미매칭", ("NOKW",), False),
        },
        choices=(
            BrandChoice("리바로", "리바로", 1, True),
            BrandChoice("미매칭", "미매칭", 2, False),
        ),
        candidates=(),
        ranking_quarter="2026-Q1",
        applied_filter={"atc4": ["C10A1"]},
    )
    monkeypatch.setattr(
        topic_matrix,
        "iqvia_product_codes_by_brand",
        lambda _brands: {"리바로": ("LIVALO", "LIVALOZET"), "미매칭": ("NOKW",)},
    )
    # Manufacturer (제조사, MFR NAME KOR) map. Multi-manufacturer retained for CMO/repackaging:
    # JW SHINYAK is hit by both codes (count 2) so it sorts before JW PHARMACEUTICAL (count 1);
    # NOKW has no manufacturer -> null.
    manufacturer_map = {
        "LIVALO": frozenset({"JW SHINYAK"}),
        "LIVALOZET": frozenset({"JW PHARMACEUTICAL", "JW SHINYAK"}),
    }
    monkeypatch.setattr(topic_matrix, "get_manufacturer_by_product", lambda: manufacturer_map)

    result = topic_matrix._company_names_by_brand(brand_set, {})

    assert result == {"리바로": "JW SHINYAK, JW PHARMACEUTICAL", "미매칭": None}


def test_company_names_tie_breaks_on_name_ascending(monkeypatch) -> None:
    view = ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    brand_set = BrandSetResolution(
        view_name="general", market_id="C10A1", selected_brand="리바로", view=view,
        market_row={}, brand_rows=(),
        brand_meta={"리바로": BrandMeta("리바로", "리바로", ("LIVALO",), True)},
        choices=(BrandChoice("리바로", "리바로", 1, True),),
        candidates=(), ranking_quarter="2026-Q1", applied_filter={},
    )
    monkeypatch.setattr(topic_matrix, "iqvia_product_codes_by_brand", lambda _b: {"리바로": ("LIVALO",)})
    # Equal counts (one code hits both once) -> deterministic name ascending (BETA before GAMMA).
    manufacturer_map = {"LIVALO": frozenset({"GAMMA", "BETA"})}
    monkeypatch.setattr(topic_matrix, "get_manufacturer_by_product", lambda: manufacturer_map)
    assert topic_matrix._company_names_by_brand(brand_set, {}) == {"리바로": "BETA, GAMMA"}


def test_fetch_manufacturer_by_product_builds_kor_map_and_skips_null(monkeypatch) -> None:
    """Source = iqvia_nsa_quarterly_raw MFR NAME KOR; key normalized; null/empty skipped."""
    rows = [
        {"product": "LIVALO", "manufacturer": "제이더블유중외제약"},
        {"product": "CRESTOR", "manufacturer": "아스트라제네카"},
        {"product": "ATORVA", "manufacturer": "유한양행"},
        {"product": "NOMFR", "manufacturer": ""},        # empty -> skipped
        {"product": "NULLMFR", "manufacturer": None},    # null -> skipped
    ]
    captured = {}

    def fake_fetch_all(sql, params=None):
        captured["sql"] = sql
        return rows

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)
    result = manufacturer_resolver.fetch_manufacturer_by_product()
    assert result == {
        "LIVALO": frozenset({"제이더블유중외제약"}),
        "CRESTOR": frozenset({"아스트라제네카"}),
        "ATORVA": frozenset({"유한양행"}),
    }
    # confirm the source table + Korean manufacturer column
    assert "iqvia_nsa_quarterly_raw" in captured["sql"]
    assert "MFR NAME KOR" in captured["sql"]


def test_post_topic_service_keeps_topic_brand_contract(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _confidence_brand_set())
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})
    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", _confidence_fetch_all)

    payload = topic_matrix.get_topic_brand_payload({"view": "general", "selected_brand": "플라주오피", "filters": {"atc4": ["K01A3"]}})

    assert payload is not None
    assert set(payload["brands"][0]) == {
        "brand_key",
        "brand_name",
        "company_name",
        "is_jw",
        "is_selected",
        "sales_rank",
        "topics",
        "topic_shares",
        "event_count",
        "etc_pct",
        "brand_specific_topics",
    }


def _confidence_brand_set() -> BrandSetResolution:
    view = ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    brand_meta = {
        "플라주오피": BrandMeta("플라주오피", "플라주오피", ("PLAJU OP",), True),
        "엔커버": BrandMeta("엔커버", "엔커버", ("ENCOVER",), False),
        "가스모틴": BrandMeta("가스모틴", "가스모틴", ("GASMOTIN",), False),
        "가나칸": BrandMeta("가나칸", "가나칸", ("GANAKHAN",), False),
        "리바로브이": BrandMeta("리바로브이", "리바로브이", ("LIVALO V",), False),
        "리바로": BrandMeta("리바로", "리바로", ("LIVALO",), False),
        "리피토": BrandMeta("리피토", "리피토", ("LIPITOR",), False),
        "미매칭": BrandMeta("미매칭", "미매칭", ("NO STORED TOPIC",), False),
    }
    return BrandSetResolution(
        view_name="general",
        market_id="K01A3",
        selected_brand="플라주오피",
        view=view,
        market_row={"atc4_desc": "CONFIDENCE"},
        brand_rows=(),
        brand_meta=brand_meta,
        choices=(
            BrandChoice("플라주오피", "플라주오피", 1, True),
            BrandChoice("엔커버", "엔커버", 2, False),
            BrandChoice("가스모틴", "가스모틴", 3, False),
            BrandChoice("가나칸", "가나칸", 4, False),
            BrandChoice("리바로브이", "리바로브이", 5, False),
            BrandChoice("리바로", "리바로", 6, False),
            BrandChoice("리피토", "리피토", 7, False),
            BrandChoice("미매칭", "미매칭", 8, False),
        ),
        candidates=(),
        ranking_quarter="2025-Q4",
        applied_filter={"atc4": ["K01A3"]},
    )


def _confidence_topic_row() -> dict[str, str]:
    payload = {
        "scope": {"scope_id": "atc4:K01A3", "atc4_values": ["K01A3"]},
        "axis": {
            "topics": [
                {"topic_id": "T01", "label": "수액", "definition": "수액"},
            ]
        },
        "brands": [
            {"brand": "PLAJU OP", "row_count": 1, "topic_shares": [_topic("T01", "수액", 100.0)]},
            {"brand": "ENCOVER", "row_count": 4, "topic_shares": [_topic("T01", "영양공급", 75.0)]},
            {"brand": "GASMOTIN", "row_count": 31, "topic_shares": [_topic("T01", "위장운동", 64.5)]},
            {"brand": "GANAKHAN", "row_count": 34, "topic_shares": [_topic("T01", "위장운동", 67.7)]},
            {"brand": "LIVALO V", "row_count": 50, "topic_shares": [_topic("T01", "당뇨 안전성", 50.0)]},
            {"brand": "LIVALO", "row_count": 473, "topic_shares": [_topic("T01", "당뇨 안전성", 62.5)]},
            {"brand": "LIPITOR", "row_count": 990, "topic_shares": [_topic("T01", "LDL 조절", 36.3)]},
        ]
    }
    return {
        "scope_id": "atc4:K01A3",
        "display_name": "PLAJU OP Market",
        "quality_grade": "A",
        "source_row_count": "1",
        "run_id": "brand_activity_replay_20260703_125045",
        "payload": json.dumps(payload, ensure_ascii=False),
    }


def _topic(topic_id: str, label: str, share_pct: float) -> dict[str, str | float | int]:
    return {"topic_id": topic_id, "label": label, "share_pct": share_pct, "row_count": 1}


def _confidence_fetch_all(sql: str, params: tuple[object, ...] | None = None) -> list[dict[str, str | int]]:
    if "row_topic_assignment" not in sql:
        return [_confidence_topic_row()]
    product = str(params[1]) if params else ""
    counts = {
        "PLAJU OP": 1,
        "ENCOVER": 4,
        "GASMOTIN": 31,
        "GANAKHAN": 34,
        "LIVALO V": 50,
        "LIVALO": 473,
        "LIPITOR": 990,
    }
    count = counts.get(product)
    if count is None:
        return []
    return [{"topic_id": "T01", "affected_row_count": count, "brand_total_rows": count, "share_pct": "100.00"}]
