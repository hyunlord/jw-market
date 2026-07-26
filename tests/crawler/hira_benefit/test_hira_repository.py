from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Self

from pipeline.scripts.crawler.hira_benefit.models import (
    FieldParseStatus,
    ParsedNotice,
    ParseStatus,
)
from pipeline.scripts.crawler.hira_benefit.repository import (
    PersistableNotice,
    latest_notice_id,
    persist_batch,
)


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
        target_status=FieldParseStatus.EXTRACTED,
        exclusion_status=FieldParseStatus.FAILED,
        dosage_status=FieldParseStatus.FAILED,
    )

    persist_batch(
        conn,
        notices=(
            PersistableNotice(
                parsed=parsed,
                listing_fingerprint="b" * 64,
                brand_names=("리바로",),
            ),
        ),
        run_id="hira-20260725",
        index_tag_signature_sha256="c" * 64,
        mapping_revision="cache_brands:build-sha",
        collected_at=datetime(2026, 7, 25, tzinfo=UTC),
    )

    sql = "\n".join(call[0] for call in conn.fake_cursor.calls)
    assert "INSERT INTO hira_benefit_notice" in sql
    assert "target_status" in sql
    assert "exclusion_status" in sql
    assert "dosage_status" in sql
    assert "INSERT INTO hira_benefit_notice_brand" in sql
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


def test_latest_notice_id_orders_numeric_ids_numerically() -> None:
    assert latest_notice_id(("99", "100", "53026")) == "53026"


def test_latest_notice_id_keeps_opaque_ids_deterministic() -> None:
    assert latest_notice_id(("notice-b", "notice-a")) == "notice-b"
