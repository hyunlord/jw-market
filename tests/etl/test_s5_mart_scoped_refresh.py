from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from pipeline.etl.stages import s5_mart


def test_s5_scoped_refresh_seeds_all_tables_and_recomputes_only_affected_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a scoped MI Master refresh affecting one ML and one CD market.
    statements: list[tuple[str, object]] = []
    ml_calls: list[str | None] = []
    cd_calls: list[str | None] = []

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, params: object = None) -> int:
            statements.append((sql, params))
            return 2

        def fetchone(self) -> dict[str, int]:
            return {"row_count": 0, "max_id": 2}

    class Connection:
        def __init__(self) -> None:
            self.commits = 0
            self.closed = False

        def cursor(self) -> Cursor:
            return Cursor()

        def commit(self) -> None:
            self.commits += 1

        def close(self) -> None:
            self.closed = True

    conn = Connection()

    def compute_ml(_dry_run: bool, _insert: bool, _output_dir: Path, *, ml: str | None):
        ml_calls.append(ml)
        return [], [], {"brand_rows": 1, "market_rows": 1, "ml_count": 1}

    def compute_cd(
        _dry_run: bool,
        _insert: bool,
        _output_dir: Path,
        *,
        cd_market: str | None,
    ):
        cd_calls.append(cd_market)
        return [], [], {"brand_rows": 1, "market_rows": 1, "cd_market_count": 1}

    strategic_ml = ModuleType("pipeline.etl.io.mart.strategic_ml")
    strategic_ml.compute_strategic_ml = compute_ml  # type: ignore[attr-defined]
    strategic_cd = ModuleType("pipeline.etl.io.mart.strategic_cd")
    strategic_cd.compute_strategic_cd = compute_cd  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, strategic_ml.__name__, strategic_ml)
    monkeypatch.setitem(sys.modules, strategic_cd.__name__, strategic_cd)
    monkeypatch.setattr(s5_mart, "_env", lambda: {})
    monkeypatch.setattr(s5_mart, "_admin_connect", lambda _env: conn)
    monkeypatch.setattr(s5_mart, "_configure_mart_env", lambda *_args: None)

    # When: s5 runs with explicit affected IDs.
    rc = s5_mart.run(
        {
            "target_db": "jw_mart_ingest_shadow_build_run1",
            "source_db": "jw_mart",
            "affected_ml_ids": ("ml_002",),
            "affected_cd_ids": ("cd_002",),
        }
    )

    # Then: the candidate keeps source baseline rows and recomputes only affected IDs.
    assert rc == 0
    sql = "\n".join(statement for statement, _params in statements)
    assert "DROP TABLE" not in sql
    for table in (
        "mart_strategic_ml_brand_metric",
        "mart_strategic_ml_market_metric",
        "mart_strategic_cd_brand_metric",
        "mart_strategic_cd_market_metric",
    ):
        assert f"CREATE TABLE IF NOT EXISTS `jw_mart_ingest_shadow_build_run1`.`{table}`" in sql
        assert (
            f"INSERT INTO `jw_mart_ingest_shadow_build_run1`.`{table}` "
            f"SELECT * FROM `jw_mart`.`{table}`"
        ) in sql
    assert (
        "DELETE FROM `jw_mart_ingest_shadow_build_run1`.`mart_strategic_ml_brand_metric` "
        "WHERE `ml_id` IN (%s)"
    ) in sql
    assert (
        "DELETE FROM `jw_mart_ingest_shadow_build_run1`.`mart_strategic_cd_market_metric` "
        "WHERE `cd_market_id` IN (%s)"
    ) in sql
    assert ml_calls == ["ml_002"]
    assert cd_calls == ["cd_002"]
    assert conn.closed is True


def test_s5_scoped_refresh_keeps_unaffected_scope_signatures_stable() -> None:
    # Given: live strategic rows already copied into an isolated candidate.
    rows = {
        ("target", "mart_strategic_ml_brand_metric"): [
            {"id": 1, "ml_id": "ml_001", "payload": "same"},
            {"id": 2, "ml_id": "ml_002", "payload": "old"},
        ],
        ("target", "mart_strategic_ml_market_metric"): [
            {"id": 1, "ml_id": "ml_001", "payload": "same"},
            {"id": 2, "ml_id": "ml_002", "payload": "old"},
        ],
        ("target", "mart_strategic_cd_brand_metric"): [
            {"id": 1, "cd_market_id": "cd_001", "payload": "same"},
            {"id": 2, "cd_market_id": "cd_002", "payload": "old"},
        ],
        ("target", "mart_strategic_cd_market_metric"): [
            {"id": 1, "cd_market_id": "cd_001", "payload": "same"},
            {"id": 2, "cd_market_id": "cd_002", "payload": "old"},
        ],
    }
    before = s5_mart._unaffected_strategic_signatures(
        rows,
        affected_ml_ids=("ml_002",),
        affected_cd_ids=("cd_002",),
    )

    # When: affected rows are removed from the copied candidate.
    s5_mart._delete_affected_rows(
        rows,
        affected_ml_ids=("ml_002",),
        affected_cd_ids=("cd_002",),
    )
    after = s5_mart._unaffected_strategic_signatures(
        rows,
        affected_ml_ids=("ml_002",),
        affected_cd_ids=("cd_002",),
    )

    # Then: unaffected ML/CD IDs survive with identical row counts and hashes.
    assert after == before
    assert rows[("target", "mart_strategic_ml_brand_metric")] == [
        {"id": 1, "ml_id": "ml_001", "payload": "same"}
    ]
    assert rows[("target", "mart_strategic_cd_market_metric")] == [
        {"id": 1, "cd_market_id": "cd_001", "payload": "same"}
    ]
