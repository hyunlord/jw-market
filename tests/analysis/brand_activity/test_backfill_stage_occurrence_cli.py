from __future__ import annotations

import pytest

from pipeline.scripts.analysis.brand_activity.auto_topic import backfill_stage_occurrence as backfill


GENERATION = "a" * 64


def test_backfill_requires_the_requested_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backfill, "_stage_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backfill, "_snapshot_fingerprint", lambda _rows: "b" * 64)

    with pytest.raises(RuntimeError, match="requested stage generation"):
        backfill.backfill_generation(
            object(),  # type: ignore[arg-type]
            schema="jw_brand_activity_stage",
            stage_generation_id=GENERATION,
            batch_size=1000,
            expected_rows=0,
        )


def test_backfill_cli_requires_stage_generation_id() -> None:
    with pytest.raises(SystemExit):
        backfill.parse_args(["--expected-rows", "66556"])


class _Cursor:
    def __init__(self, state: dict[int, dict[str, object]]) -> None:
        self._state = state
        self._row: dict[str, int] = {}

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _sql: str, _params: tuple[object, ...]) -> int:
        self._row = {"row_count": len(self._state)}
        return 0

    def executemany(self, _sql: str, values: list[tuple[object, ...]]) -> int:
        for value in values:
            self._state[int(value[1])] = {
                "stage_row_id": int(value[1]),
                "semantic_event_key_v1": value[2],
                "stage_row_sha256": value[3],
                "source_file": value[4],
                "source_sheet": value[5],
                "source_row_no": value[6],
                "source_file_sha256": value[7],
                "backfill_batch_id": value[8],
            }
        return len(values)

    def fetchone(self) -> dict[str, int]:
        return self._row


class _Connection:
    def __init__(self) -> None:
        self.state: dict[int, dict[str, object]] = {}
        self.commits = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self.state)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        raise AssertionError("rollback was not expected")


def test_backfill_rerun_is_idempotent_and_preserves_duplicate_occurrences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = []
    for row_id in (11, 12):
        row = {name: "same-value" for name in backfill.SEMANTIC_FIELD_NAMES}
        row["period_ym"] = "2024-06"
        row.update(
            {
                "id": row_id,
                "source_file": "keyword.xlsx",
                "source_sheet": "Sheet1",
                "source_row_no": row_id,
                "source_file_sha256": "c" * 64,
                "stage_row_sha256": f"{row_id:064x}",
            }
        )
        rows.append(row)
    connection = _Connection()
    monkeypatch.setattr(backfill, "_stage_rows", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(backfill, "_snapshot_fingerprint", lambda _rows: GENERATION)
    monkeypatch.setattr(
        backfill,
        "_current_snapshot",
        lambda *_args, **_kwargs: (2, GENERATION),
    )
    monkeypatch.setattr(
        backfill,
        "_existing_batch_rows",
        lambda *_args, stage_row_ids, **_kwargs: {
            row_id: connection.state[row_id]
            for row_id in stage_row_ids
            if row_id in connection.state
        },
    )

    first = backfill.backfill_generation(
        connection,  # type: ignore[arg-type]
        schema="jw_brand_activity_stage",
        stage_generation_id=GENERATION,
        batch_size=1,
        expected_rows=2,
    )
    repeated = backfill.backfill_generation(
        connection,  # type: ignore[arg-type]
        schema="jw_brand_activity_stage",
        stage_generation_id=GENERATION,
        batch_size=1,
        expected_rows=2,
    )

    assert first["inserted_rows"] == 2
    assert repeated["inserted_rows"] == 0
    assert repeated["reused_rows"] == 2
    assert len(connection.state) == 2
    assert len({row["semantic_event_key_v1"] for row in connection.state.values()}) == 1
