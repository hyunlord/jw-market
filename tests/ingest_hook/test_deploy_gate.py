"""배포 전 원장 테이블 게이트.

두 strict 플래그가 기본 1 이라 어떤 이유로 배포하든 strict 가 켜진다.
런타임 preflight 는 배포가 끝난 뒤에야 막으므로, 배포 ★전에 전제를 확인한다.
"""

from __future__ import annotations

import pytest

from pipeline.scripts.ingest_hook import deploy_gate

DB = "jw_mart_d2_stage_20260630_r2"


class _FakeCursor:
    """INFORMATION_SCHEMA.COLUMNS 응답. 값이 Exception 이면 조회 자체가 실패한다."""

    def __init__(self, tables):
        self.tables = tables
        self._rows: list[dict] = []

    def execute(self, sql, params=()):
        _, table = params
        value = self.tables.get(table)
        if isinstance(value, Exception):
            raise value
        self._rows = [{"COLUMN_NAME": column} for column in (value or [])]

    def fetchall(self):
        return self._rows


@pytest.fixture
def healthy() -> dict[str, list[str]]:
    """커밋된 DDL 에서 뽑은 정상 컬럼 집합."""
    return {
        table: list(deploy_gate.expected_columns(table))
        for table in deploy_gate.REQUIRED_TABLES
    }


def _run(tables):
    verdicts = deploy_gate.check_tables(_FakeCursor(tables), DB)
    return verdicts, deploy_gate.render(verdicts, DB)


def _verdict(verdicts, table):
    return next(verdict for verdict in verdicts if verdict.table == table)


def test_expected_columns_come_from_the_committed_ddl(healthy):
    # 기대 컬럼을 코드에 다시 적지 않는다. 운영자가 실제로 적용할 파일에서 읽는다.
    assert set(healthy) == set(deploy_gate.REQUIRED_TABLES)
    assert "manifest_sha" in healthy["ingest_signal_event"]
    assert all(columns for columns in healthy.values())


def test_all_tables_present_passes(healthy):
    verdicts, text = _run(healthy)
    assert all(verdict.ok for verdict in verdicts)
    assert "VERDICT: ok" in text


def test_missing_signal_table_blocks_and_names_the_ddl(healthy):
    del healthy["ingest_signal_event"]  # 라이브 d2 의 실제 상태
    verdicts, text = _run(healthy)
    target = _verdict(verdicts, "ingest_signal_event")
    assert not target.present and not target.ok
    assert "VERDICT: BLOCKED" in text
    assert "ingest-signal-event.sql" in text
    # 왜 지금 배포하면 안 되는지도 함께 말한다.
    assert "stops ingestion" in text
    # 나머지는 통과로 남아야 한다 — 과잉 차단은 게이트를 끄고 싶게 만든다.
    assert sum(1 for verdict in verdicts if verdict.ok) == 3


def test_schema_mismatch_blocks(healthy):
    dropped = healthy["ingest_stage_event"].pop()
    healthy["ingest_ledger"].append("bogus_extra_column")
    verdicts, text = _run(healthy)
    stage = _verdict(verdicts, "ingest_stage_event")
    ledger = _verdict(verdicts, "ingest_ledger")
    assert dropped in stage.missing_columns and not stage.ok
    assert "bogus_extra_column" in ledger.unexpected_columns and not ledger.ok
    assert "VERDICT: BLOCKED" in text


def test_unreadable_schema_is_unknown_not_missing(healthy):
    healthy["ingest_status_transition"] = RuntimeError("SELECT command denied to user")
    verdicts, text = _run(healthy)
    target = _verdict(verdicts, "ingest_status_transition")
    # 모른다와 없다를 같은 출구로 내보내지 않는다.
    assert not target.ok
    assert target.error is not None
    assert "UNKNOWN" in target.describe()
    assert "MISSING" not in target.describe()
    assert "VERDICT: BLOCKED" in text
    # 있을지도 모르는 테이블에 대해 DDL 적용을 권하지 않는다.
    assert "ingest-status-transition.sql" not in text


def test_gate_has_no_disable_switch():
    # 전제 확인이지 정책이 아니다. 끌 수 있으면 전제가 아니다.
    with pytest.raises(SystemExit) as excinfo:
        deploy_gate.main(["--skip"], connect=lambda: pytest.fail("must not connect"))
    assert excinfo.value.code == 2


def test_unreachable_database_blocks(capsys):
    def boom():
        raise RuntimeError("connection refused")

    assert deploy_gate.main([], connect=boom) == 2
    assert "BLOCKED" in capsys.readouterr().err
