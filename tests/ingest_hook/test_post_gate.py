from __future__ import annotations

import sqlite3

import pytest

from pipeline.scripts.ingest_hook.post_gate import (
    PostGateError,
    SigmaEvidence,
    SourceSnapshot,
    TableFingerprint,
    run_post_gates,
    staging_row_count,
)
from pipeline.scripts.ingest_hook.sigma_gate import check_staging


def _snapshot(digest: str = "stable") -> SourceSnapshot:
    return SourceSnapshot((TableFingerprint("iqvia_rows", 11, digest),))


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE staging (period TEXT, level TEXT, value REAL)")
    conn.executemany(
        "INSERT INTO staging VALUES (?, ?, ?)",
        [("2026-07", "brand", 4.0), ("2026-07", "brand", 6.0), ("2026-07", "전체", 10.0)],
    )
    return conn


def _sigma(conn: sqlite3.Connection) -> SigmaEvidence:
    report = check_staging(conn, "staging")
    return SigmaEvidence(len(report.periods), len(report.periods), str(report.periods))


def test_all_post_gates_pass_and_write_report(tmp_path):
    conn = _database()
    report = run_post_gates(
        run_id="run-1", epoch="2026-07", category="ubist",
        sigma_check=lambda: _sigma(conn), expected_rows=3,
        actual_rows=staging_row_count(conn, "staging"),
        untouched_before=_snapshot(), untouched_after=_snapshot(),
        report_path=tmp_path / "post_gate.json",
    )
    assert report.status == "pass"
    assert [gate.status for gate in report.gates] == ["pass", "pass", "pass"]
    assert report.duration_ms >= 0
    assert (tmp_path / "post_gate.json").is_file()


def test_sigma_mismatch_blocks_promotion(tmp_path):
    conn = _database()
    conn.execute("UPDATE staging SET value=99 WHERE level='전체'")
    with pytest.raises(PostGateError) as raised:
        run_post_gates(
            run_id="run-2", epoch="2026-07", category="ubist",
            sigma_check=lambda: _sigma(conn), expected_rows=3, actual_rows=3,
            untouched_before=_snapshot(), untouched_after=_snapshot(),
            report_path=tmp_path / "post_gate.json",
        )
    assert raised.value.report.gates[0].status == "fail"


def test_manifest_row_shortfall_blocks_promotion(tmp_path):
    with pytest.raises(PostGateError) as raised:
        run_post_gates(
            run_id="run-3", epoch="2026-07", category="ubist",
            sigma_check=lambda: SigmaEvidence(1, 1, "ok"),
            expected_rows=5, actual_rows=4,
            untouched_before=_snapshot(), untouched_after=_snapshot(),
            report_path=tmp_path / "post_gate.json",
        )
    assert raised.value.report.gates[1].status == "fail"


def test_cross_source_mutation_blocks_promotion(tmp_path):
    with pytest.raises(PostGateError) as raised:
        run_post_gates(
            run_id="run-4", epoch="2026-07", category="ubist",
            sigma_check=lambda: SigmaEvidence(1, 1, "ok"),
            expected_rows=3, actual_rows=3,
            untouched_before=_snapshot("before"), untouched_after=_snapshot("after"),
            report_path=tmp_path / "post_gate.json",
        )
    assert raised.value.report.gates[2].status == "fail"
