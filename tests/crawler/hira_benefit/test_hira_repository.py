from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Self

from pipeline.scripts.crawler.hira_benefit.models import ParsedNotice, ParseStatus
from pipeline.scripts.crawler.hira_benefit.repository import (
    PersistableNotice,
    latest_notice_id,
    load_serving_brand_scope,
    persist_batch,
)
from pipeline.scripts.crawler.hira_benefit.scope import BrandMatch


class FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.calls.append((sql, params))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(self) -> None:
        self.fake_cursor = FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return self.fake_cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class ScopeCursor(FakeCursor):
    def __init__(self) -> None:
        super().__init__()
        self._result_index = 0
        self._results = (
            (
                {
                    "brand_key": "리바로",
                    "brand_name": "리바로",
                    "atc4_code": "C10A1",
                },
                {
                    "brand_key": "리바로",
                    "brand_name": "리바로",
                    "atc4_code": "C10C0",
                },
                {
                    "brand_key": "아일리아",
                    "brand_name": "아일리아",
                    "atc4_code": "S01P0",
                },
                {
                    "brand_key": "위너프에이플러스",
                    "brand_name": "위너프에이플러스",
                    "atc4_code": "B05B0",
                },
                {
                    "brand_key": "",
                    "brand_name": "위너프A+",
                    "atc4_code": "",
                },
                {
                    "brand_key": "",
                    "brand_name": "신규브랜드",
                    "atc4_code": "",
                },
            ),
            (
                {
                    "alias_name": "위너프A+",
                    "brand_key": "위너프에이플러스",
                },
            ),
            (
                {
                    "molecule_norm": "pitavastatin",
                    "brand_key": "리바로",
                    "brand_name": "리바로",
                    "atc4_code": "C10A1",
                },
                {
                    "molecule_norm": "aflibercept",
                    "brand_key": "아일리아",
                    "brand_name": "아일리아",
                    "atc4_code": "S01P0",
                },
            ),
            ({"raw_text": "기존 고시 원문"},),
        )

    def fetchall(self) -> tuple[dict[str, object], ...]:
        result = self._results[self._result_index]
        self._result_index += 1
        return result


class ScopeConnection(FakeConnection):
    def __init__(self) -> None:
        super().__init__()
        self.fake_cursor = ScopeCursor()


def test_serving_scope_uses_mart_universe_and_canonical_keys() -> None:
    brands, molecules, raw_texts, revision = load_serving_brand_scope(
        ScopeConnection(),
        minimum_brand_count=2,
    )

    assert [(row.brand_key, row.brand_name, row.atc4_codes) for row in brands] == [
        ("리바로", "리바로", ("C10A1", "C10C0")),
        ("신규브랜드", "신규브랜드", ()),
        ("아일리아", "아일리아", ("S01P0",)),
        ("위너프에이플러스", "위너프A+", ("B05B0",)),
        ("위너프에이플러스", "위너프에이플러스", ("B05B0",)),
    ]
    assert [(row.molecule_norm, row.brand_key) for row in molecules] == [
        ("aflibercept", "아일리아"),
        ("pitavastatin", "리바로"),
    ]
    assert any(
        row.brand_key == "위너프에이플러스" and row.brand_name == "위너프A+"
        for row in brands
    )
    assert raw_texts == ("기존 고시 원문",)
    assert revision.startswith("serving_mart:")


def test_persist_batch_commits_notices_brand_links_and_state_together() -> None:
    conn = FakeConnection()
    parsed = ParsedNotice(
        source_notice_id="53026",
        source_url="https://www.hira.or.kr/detail?brdBltNo=53026",
        title="고시 안내",
        notice_no="제2025-189호",
        notice_date=date(2025, 11, 28),
        target_condition="대상",
        exclusion_rule=None,
        dosage_limit=None,
        raw_text="리바로 대상",
        raw_html_sha256="a" * 64,
        parse_status=ParseStatus.PARTIAL,
        failed_fields=("exclusion_rule", "dosage_limit"),
    )

    persist_batch(
        conn,
        notices=(
            PersistableNotice(
                parsed=parsed,
                listing_fingerprint="b" * 64,
                brand_matches=(
                    BrandMatch(
                        brand_key="리바로",
                        brand_name="리바로",
                        match_method="exact_boundary_name",
                        confidence="high",
                        evidence_start=0,
                        evidence_end=3,
                        matched_text="리바로",
                    ),
                ),
            ),
        ),
        run_id="hira-20260725",
        index_tag_signature_sha256="c" * 64,
        mapping_revision="cache_brands:build-sha",
        collected_at=datetime(2026, 7, 25, tzinfo=UTC),
    )

    sql = "\n".join(call[0] for call in conn.fake_cursor.calls)
    assert "INSERT INTO hira_benefit_notice" in sql
    assert "INSERT INTO hira_benefit_notice_brand" in sql
    brand_params = next(
        params
        for statement, params in conn.fake_cursor.calls
        if "INSERT INTO hira_benefit_notice_brand" in statement
    )
    assert brand_params[:4] == (
        "53026",
        "리바로",
        "리바로",
        "exact_boundary_name",
    )
    assert "INSERT INTO hira_benefit_crawl_run" in sql
    crawl_run_sql = next(
        statement
        for statement, _params in conn.fake_cursor.calls
        if "INSERT INTO hira_benefit_crawl_run" in statement
    )
    assert "ON DUPLICATE KEY UPDATE" in crawl_run_sql
    assert "INSERT INTO hira_benefit_crawl_state" in sql
    assert sql.index("hira_benefit_notice") < sql.index("hira_benefit_crawl_state")
    assert conn.commits == 1
    assert conn.rollbacks == 0
    crawl_run_params = next(
        params
        for statement, params in conn.fake_cursor.calls
        if "INSERT INTO hira_benefit_crawl_run" in statement
    )
    assert '"confidence": "high"' in crawl_run_params[-1]
    assert '"evidence_start": 0' in crawl_run_params[-1]


def test_latest_notice_id_orders_numeric_ids_numerically() -> None:
    assert latest_notice_id(("99", "100", "53026")) == "53026"


def test_latest_notice_id_keeps_opaque_ids_deterministic() -> None:
    assert latest_notice_id(("notice-b", "notice-a")) == "notice-b"
