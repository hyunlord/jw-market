from __future__ import annotations

from pipeline.scripts.analysis.brand_activity.auto_topic.backfill_topic_is_jw import mark_payload_is_jw
from pipeline.scripts.analysis.brand_activity.auto_topic.data_source import (
    is_jw_representing_company,
    load_alias_descriptions,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.models import KeywordRow


def test_jw_company_detection_uses_source_company_prefix() -> None:
    assert is_jw_representing_company("JW PHARMACEUTICAL")
    assert is_jw_representing_company("jw shinyak")
    assert not is_jw_representing_company("YUHAN CO.")


def test_alias_descriptions_fall_back_to_keyword_representing_company() -> None:
    descriptions = load_alias_descriptions(
        {},
        [
            _row(1, brand="LIVALO", company="JW PHARMACEUTICAL"),
            _row(2, brand="LIVALO", company="YUHAN CO."),
            _row(3, brand="COMPETITOR", company="YUHAN CO."),
        ],
    )

    assert descriptions["C10A1:LIVALO"].is_jw is True
    assert descriptions["C10A1:LIVALO"].representing_company == ("JW PHARMACEUTICAL", "YUHAN CO.")
    assert descriptions["C10A1:COMPETITOR"].is_jw is False


def test_mark_payload_is_jw_changes_only_brand_flags() -> None:
    payload = {
        "scope": {"scope_id": "group:livalo_family"},
        "brands": [
            {"brand": "LIVALO", "is_jw": False, "topic_shares": [{"topic_id": "T1"}]},
            {"brand": "COMPETITOR", "is_jw": False, "topic_shares": [{"topic_id": "T2"}]},
        ],
    }

    updated, changed, brands_seen, true_brands = mark_payload_is_jw(payload, {"LIVALO": True})

    assert changed is True
    assert brands_seen == {"LIVALO", "COMPETITOR"}
    assert true_brands == {"LIVALO"}
    assert updated["scope"] == payload["scope"]
    assert updated["brands"][0]["is_jw"] is True
    assert updated["brands"][0]["topic_shares"] == [{"topic_id": "T1"}]
    assert updated["brands"][1] == payload["brands"][1]


def _row(row_id: int, *, brand: str, company: str) -> KeywordRow:
    return KeywordRow(
        row_id=row_id,
        period_ym="2026-01",
        atc4="C10A1",
        brand=brand,
        keyword_text="sample",
        interest="",
        prescription_frequency="",
        prescription_evolution="",
        promotional_lit="",
        abstract_lit="",
        patient_lit="",
        specialty="",
        visit_location="",
        stage_row_sha256=f"sha-{row_id}",
        representing_company=company,
    )
