from __future__ import annotations

from pipeline.scripts.analysis.brand_activity.auto_topic.backfill_topic_is_jw import mark_payload_is_jw
from pipeline.scripts.analysis.brand_activity.auto_topic.data_source import (
    fetch_keyword_rows,
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


def test_fetch_keyword_rows_selects_representing_company() -> None:
    """Given Keyword stage rows, When fetched, Then source company metadata is preserved."""
    connection = _FakeConnection()

    rows = fetch_keyword_rows(connection, ["C10A1"], schema="stage_schema")

    assert connection.cursor_obj.saw_representing_company is True
    assert rows[0].representing_company == "JW PHARMACEUTICAL"


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


class _FakeConnection:
    """Minimal DB-API connection for fetch_keyword_rows regression coverage."""

    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()

    def cursor(self) -> "_FakeCursor":
        return self.cursor_obj


class _FakeCursor:
    """Context-managed cursor that fails if representing_company is not selected."""

    def __init__(self) -> None:
        self.saw_representing_company = False

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, sql: str, params: tuple[str, ...] = ()) -> None:
        if sql.startswith("SELECT id,"):
            self.saw_representing_company = "representing_company" in sql
            assert self.saw_representing_company
            assert params == ("C10A1",)

    def fetchall(self) -> list[dict]:
        return [
            {
                "id": 1,
                "period_ym": "2026-01",
                "visit_location": "clinic",
                "specialty": "IM",
                "product_name": "LIVALO",
                "therapeutic_class": "C10A1",
                "keyword_text": "sample",
                "interest": "",
                "prescription_frequency": "",
                "prescription_evolution": "",
                "abstract_lit": "",
                "patient_lit": "",
                "promotional_lit": "",
                "stage_row_sha256": "sha-1",
                "representing_company": "JW PHARMACEUTICAL",
            }
        ]
