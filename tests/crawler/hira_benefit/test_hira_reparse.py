from __future__ import annotations

from typing import Self

import pytest

from pipeline.scripts.crawler.hira_benefit.reparse import (
    ReparseSource,
    apply_reparse_plan,
    build_reparse_plan,
    load_reparse_rows,
    summarize_reparse_plan,
)


class FakeCursor:
    def __init__(
        self,
        rows: list[dict[str, str]],
        *,
        fail_update: bool = False,
        update_rowcount: int = 1,
    ) -> None:
        self.rows = rows
        self.fail_update = fail_update
        self.update_rowcount = update_rowcount
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> int:
        self.calls.append((sql, params))
        if self.fail_update and sql.lstrip().startswith("UPDATE"):
            raise RuntimeError("injected update failure")
        if sql.lstrip().startswith("UPDATE"):
            return self.update_rowcount
        return 0

    def fetchall(self) -> list[dict[str, str]]:
        return self.rows

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeConnection:
    def __init__(
        self,
        rows: list[dict[str, str]],
        *,
        fail_update: bool = False,
        update_rowcount: int = 1,
    ) -> None:
        self.fake_cursor = FakeCursor(
            rows,
            fail_update=fail_update,
            update_rowcount=update_rowcount,
        )
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        return self.fake_cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_dry_run_loads_only_identity_and_raw_text_without_writes() -> None:
    conn = FakeConnection(
        [{"source_notice_id": "1", "raw_text": "보험인정기준 상세내용"}]
    )

    rows = load_reparse_rows(conn)

    sql = "\n".join(statement for statement, _params in conn.fake_cursor.calls)
    assert rows[0].source_notice_id == "1"
    assert "SELECT source_notice_id, raw_text" in sql
    assert "UPDATE " not in sql
    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_reparse_summary_reports_status_fields_and_target_ratio() -> None:
    plan = build_reparse_plan(
        (
            ReparseSource(
                source_notice_id="1",
                raw_text="보험인정기준 상세내용 1. 투여대상: 특정 환자군",
            ),
            ReparseSource(
                source_notice_id="2",
                raw_text="보험인정기준 상세내용 고시를 개정한다.",
            ),
        )
    )

    summary = summarize_reparse_plan(plan)

    assert summary.population == 2
    assert summary.parse_status == {"NOT_APPLICABLE": 1, "OK": 1}
    assert summary.field_nonempty == {
        "target_condition": 1,
        "exclusion_rule": 0,
        "dosage_limit": 0,
    }
    assert summary.field_status == {
        "target_status": {"EXTRACTED": 1, "NOT_APPLICABLE": 1},
        "exclusion_status": {"NOT_APPLICABLE": 2},
        "dosage_status": {"NOT_APPLICABLE": 2},
    }
    assert summary.target_suffix_count == 1
    assert summary.target_raw_ratio_median < 0.5


def test_execute_updates_only_typed_parse_fields_in_one_transaction() -> None:
    conn = FakeConnection([])
    plan = build_reparse_plan(
        (
            ReparseSource(
                source_notice_id="1",
                raw_text=(
                    "보험인정기준 상세내용 1. 투여대상: 특정 환자군 "
                    "2. 제외기준: 투여 금기 환자"
                ),
            ),
        )
    )

    apply_reparse_plan(conn, plan)

    sql = "\n".join(statement for statement, _params in conn.fake_cursor.calls)
    assert "UPDATE hira_benefit_notice" in sql
    assert "target_condition=%s" in sql
    assert "target_status=%s" in sql
    assert "exclusion_status=%s" in sql
    assert "dosage_status=%s" in sql
    assert "parse_failed_fields_json=%s" in sql
    assert "raw_text=" not in sql
    assert "source_url=" not in sql
    assert "collected_at=" not in sql
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_execute_rolls_back_the_whole_plan_when_any_update_fails() -> None:
    conn = FakeConnection([], fail_update=True)
    plan = build_reparse_plan(
        (
            ReparseSource(
                source_notice_id="1",
                raw_text="보험인정기준 상세내용",
            ),
        )
    )

    with pytest.raises(RuntimeError, match="injected update failure"):
        apply_reparse_plan(conn, plan)

    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_execute_rolls_back_when_a_notice_identity_is_not_updated_once() -> None:
    conn = FakeConnection([], update_rowcount=0)
    plan = build_reparse_plan(
        (
            ReparseSource(
                source_notice_id="missing",
                raw_text="보험인정기준 상세내용",
            ),
        )
    )

    with pytest.raises(RuntimeError, match="expected one updated row"):
        apply_reparse_plan(conn, plan)

    assert conn.commits == 0
    assert conn.rollbacks == 1
