from __future__ import annotations

from argparse import Namespace
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
import json

import pytest

from pipeline.scripts.etl import ops_forecast_builder
from pipeline.scripts.etl.ops_forecast_builder import (
    EXPECTED_BLOCKS,
    EXPECTED_HORIZONS,
    RuntimePinError,
    assert_runtime_pins,
    stride_order,
)
from pipeline.scripts.etl.ops_forecast_scope import Scope, Unit, row_cache_id
from pipeline.scripts.etl.ops_forecast_store import contamination_count, epoch_is_current


@dataclass
class _Cursor:
    rows: list[dict[str, int | str]]
    executed: list[tuple[str, tuple[str, ...]]]

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[str, ...] = ()) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> dict[str, int | str]:
        return self.rows[0]


@dataclass
class _Connection:
    rows: list[dict[str, int | str]]
    executed: list[tuple[str, tuple[str, ...]]]

    def cursor(self) -> _Cursor:
        return _Cursor(self.rows, self.executed)


def _runtime_env() -> dict[str, str]:
    return {
        "NPY_DISABLE_CPU_FEATURES": "X86_V3,X86_V4",
        "OPENBLAS_CORETYPE": "Nehalem",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }


def test_forecast_block_gate_tracks_the_current_published_mart_contract() -> None:
    # Given the current published mart exposes 43,790 forecast units
    # When the static completion gate contract is inspected
    # Then agent refresh accepts that exact unit count without weakening the gate
    assert EXPECTED_BLOCKS == 43_790


def test_forecast_horizon_gate_tracks_the_current_published_mart_contract() -> None:
    # Given the current 942 source scopes produce 3,002 source/measure horizons
    # When the static completion gate contract is inspected
    # Then agent refresh accepts that exact horizon count without weakening the gate
    assert EXPECTED_HORIZONS == 3_002


def test_row_cache_id_namespaces_overlapping_ids_by_scope_kind() -> None:
    # Given the same auto-increment id from three mart families
    raw_id = 17

    # When each id crosses the forecast cache boundary
    identities = {
        row_cache_id(Scope.general("A10A", "ubist"), raw_id),
        row_cache_id(Scope.market_landscape("ml_001", "ubist"), raw_id),
        row_cache_id(Scope.competitive_dynamics("cd_001", "ubist"), raw_id),
    }

    # Then every cache identity remains distinct
    assert identities == {"gen:17", "ml:17", "cd:17"}


def test_epoch_is_current_is_a_noop_only_for_complete_matching_staging() -> None:
    # Given a staging table with the expected epoch and exact completion count
    connection = _Connection(
        rows=[{"row_count": EXPECTED_BLOCKS, "epoch_count": 1, "source_epoch": "epoch-a"}],
        executed=[],
    )

    # When the monthly no-op gate evaluates it
    current = epoch_is_current(connection, "deep_forecast_block_stage", "epoch-a", EXPECTED_BLOCKS)

    # Then the run can stop without rebuilding
    assert current is True
    assert "COUNT(*)" in connection.executed[0][0]


def test_complete_matching_staging_runs_gates_without_recalculating_scopes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given complete staging at the current mart epoch
    scope = Scope.general("A10A", "ubist")
    units = [Unit("brand-a", scope), Unit("brand-b", scope)]
    block_keys = {unit.key for unit in units}
    horizon_keys = {(scope.market_id, scope.source, measure) for measure in ("sales", "volume")}
    connection = type("Connection", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()
    prepared: list[tuple[str, str, str]] = []
    for key, value in _runtime_env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ops_forecast_builder, "mariadb_connect", lambda: connection)
    monkeypatch.setattr(ops_forecast_builder.general_builder, "assert_d2_database", lambda _connection: None)
    monkeypatch.setattr(ops_forecast_builder, "mart_source_epoch", lambda _connection: "epoch-a")
    monkeypatch.setattr(ops_forecast_builder, "load_units", lambda _connection: units)
    monkeypatch.setattr(ops_forecast_builder, "epoch_is_current", lambda *_args: False)
    monkeypatch.setattr(
        ops_forecast_builder,
        "prepare_staging",
        lambda _connection, block, horizon, epoch: prepared.append((block, horizon, epoch)),
    )
    monkeypatch.setattr(ops_forecast_builder, "existing_block_keys", lambda *_args: set(block_keys))
    monkeypatch.setattr(ops_forecast_builder, "existing_horizon_keys", lambda *_args: set(horizon_keys))
    monkeypatch.setattr(ops_forecast_builder, "_generated_at", lambda *_args: datetime(2026, 8, 9, 0, 33, 40))
    monkeypatch.setattr(
        ops_forecast_builder,
        "load_scope",
        lambda *_args: pytest.fail("complete staging must not reload a native scope"),
    )
    monkeypatch.setattr(
        ops_forecast_builder,
        "build_scope_rows",
        lambda *_args, **_kwargs: pytest.fail("complete staging must not rebuild forecast rows"),
    )
    monkeypatch.setattr(
        ops_forecast_builder,
        "insert_blocks",
        lambda *_args: pytest.fail("complete staging must not insert forecast blocks"),
    )
    monkeypatch.setattr(
        ops_forecast_builder,
        "insert_horizons",
        lambda *_args: pytest.fail("complete staging must not insert forecast horizons"),
    )
    monkeypatch.setattr(
        ops_forecast_builder,
        "completion_gate",
        lambda *_args: {"blocks": 2, "horizons": 2},
    )
    monkeypatch.setattr(
        ops_forecast_builder,
        "verify_stride_replay",
        lambda *_args, **_kwargs: {"sampled": 2, "byte_identical": 2, "stride": 1},
    )
    monkeypatch.setattr(ops_forecast_builder, "contamination_count", lambda *_args: 0)

    # When reuse mode enters the builder without --force
    ops_forecast_builder.run(
        Namespace(
            block_table="block_stage",
            horizon_table="horizon_stage",
            workers=1,
            expected_blocks=2,
            expected_horizons=2,
            verify_sample=2,
            force=False,
        )
    )

    # Then only validation gates run and the output exposes zero recalculation calls
    output = json.loads(capsys.readouterr().out.strip())
    assert output["stage"] == "complete"
    assert output["scope_build_calls"] == 0
    assert output["inserted_blocks"] == 0
    assert output["inserted_horizons"] == 0
    assert output["reused_blocks"] == 2
    assert output["reused_horizons"] == 2
    assert prepared == [("block_stage", "horizon_stage", "epoch-a")]
    assert connection.closed is True


def test_stride_order_differs_from_generation_order_and_covers_all_items() -> None:
    # Given an ordered generation sequence
    generated = list(range(17))

    # When replay order uses a coprime stride
    replayed = stride_order(generated, stride=5)

    # Then order differs while membership remains exact
    assert replayed != generated
    assert sorted(replayed) == generated


def test_runtime_pin_gate_rejects_an_unpinned_numeric_environment() -> None:
    # Given one numeric runtime pin with an unsafe value
    environment = _runtime_env()
    environment["OPENBLAS_NUM_THREADS"] = "4"

    # When the pre-I/O runtime gate executes
    with pytest.raises(RuntimePinError, match="OPENBLAS_NUM_THREADS"):
        assert_runtime_pins(environment)


def test_runtime_pin_gate_accepts_the_reproducible_contract() -> None:
    # Given the six-value reproducibility contract
    environment = _runtime_env()

    # When the pre-I/O runtime gate executes
    assert_runtime_pins(environment)


def test_contamination_gate_normalizes_json_table_brand_collation() -> None:
    # Given MariaDB returns JSON_TABLE strings under its default utf8mb4 collation
    connection = _Connection(
        rows=[{"invalid_count": 0}],
        executed=[],
    )

    # When the native-scope contamination gate builds its SQL
    assert contamination_count(connection, "deep_forecast_block_stage") == 0

    # Then the JSON-derived brand is explicitly normalized to the mart collation
    sql = connection.executed[0][0]
    assert "payload_brand.brand_name COLLATE utf8mb4_unicode_ci" in sql
