from __future__ import annotations

from datetime import datetime
import json

import pytest

from pipeline.scripts.etl import migrate_unified_forecast_tables as migration


def _payload() -> dict:
    return {
        "available_combos": ["IQVIA.sales", "UBIST.sales"],
        "brand_key": "brand-key",
        "data": {
            "forecast": {
                "method": "method",
                "by_combo": {
                    "IQVIA.sales": {"brands": [{"forecast_values": [1.0]}]},
                    "UBIST.sales": {"brands": [{"forecast_values": [2.0]}]},
                },
            },
            "simulation": {
                "by_combo": {
                    "IQVIA.sales": {"by_brand": {"brand": {"scenarios": {"base": {"values": [1]}}}}},
                    "UBIST.sales": {"by_brand": {"brand": {"scenarios": {"base": {"values": [2]}}}}},
                }
            },
            "events": [{"title": "preserved shell"}],
        },
        "generation_status": "generated",
        "no_history_fallback": {
            "IQVIA.sales": {"applied": False, "reason": "history_present"},
            "UBIST.sales": {"applied": False, "reason": "history_present"},
        },
        "simulation_available": {"IQVIA.sales": True, "UBIST.sales": True},
    }


def test_split_and_overlay_reassembly_preserves_original_payload_bytes() -> None:
    original = migration.compact_json(_payload())

    rows = migration.split_block_payload(
        brand_key="brand-key",
        market_id="A10A1",
        response_json=original,
        source_computed_at=datetime(2026, 6, 30),
        updated_at=datetime(2026, 7, 13),
    )

    assert [row.source for row in rows] == ["iqvia_nsa", "ubist"]
    assert json.loads(rows[0].forecast_json)["by_combo"] == {
        "IQVIA.sales": {"brands": [{"forecast_values": [1.0]}]}
    }
    assert json.loads(rows[1].simulation_json or "null")["by_combo"] == {
        "UBIST.sales": {"by_brand": {"brand": {"scenarios": {"base": {"values": [2]}}}}}
    }
    assert migration.reassemble_block_payload(original, rows) == original


def test_unavailable_simulation_is_stored_as_null() -> None:
    payload = _payload()
    payload["simulation_available"] = {"IQVIA.sales": False, "UBIST.sales": True}
    payload["data"]["simulation"]["by_combo"]["IQVIA.sales"] = {"by_brand": {}}

    rows = migration.split_block_payload(
        brand_key="brand-key",
        market_id="A10A1",
        response_json=migration.compact_json(payload),
        source_computed_at=None,
        updated_at=datetime(2026, 7, 13),
    )

    iqvia = rows[0]
    assert iqvia.simulation_available is False
    assert iqvia.simulation_json is None
    assert migration.reassemble_block_payload(migration.compact_json(payload), rows) == migration.compact_json(payload)


def test_payload_generated_at_precedes_updated_at_fallback() -> None:
    payload = _payload()
    payload["generated_at"] = "2026-07-12T03:04:05"

    rows = migration.split_block_payload(
        brand_key="brand-key",
        market_id="A10A1",
        response_json=migration.compact_json(payload),
        source_computed_at=None,
        updated_at=datetime(2026, 7, 13, 1, 2, 3),
    )

    assert {row.generated_at for row in rows} == {datetime(2026, 7, 12, 3, 4, 5)}
    assert {row.generated_at_source for row in rows} == {"payload.generated_at"}


def test_view_kind_is_derived_from_market_id() -> None:
    assert migration.derive_view_kind("ml_011") == "market_landscape"
    assert migration.derive_view_kind("cd_007") == "competitive_dynamics"
    assert migration.derive_view_kind("A10A1") == "general"
    with pytest.raises(ValueError, match="unsupported market_id"):
        migration.derive_view_kind("general:A10A1")


def test_ddl_uses_confirmed_primary_keys_and_checks() -> None:
    block, horizon = migration.create_table_statements()

    assert "PRIMARY KEY (brand_key, source, market_id)" in block
    assert "PRIMARY KEY (market_id, source, measure)" in horizon
    assert "source_epoch VARCHAR(64) NOT NULL" in block
    assert "source_computed_at DATETIME NULL" in block
    assert "generated_at DATETIME NOT NULL COMMENT" in block
    assert "simulation_available = 0 AND simulation_json IS NULL" in block
    assert "simulation_available = 1 AND simulation_json IS NOT NULL" in block
    assert "view_kind = 'market_landscape' AND market_id LIKE 'ml\\_%'" in block
    assert "view_kind = 'competitive_dynamics' AND market_id LIKE 'cd\\_%'" in block


def test_migration_never_uses_duplicate_key_update_or_insert_ignore() -> None:
    source = migration.Path(migration.__file__).read_text(encoding="utf-8")

    assert "ON DUPLICATE KEY" not in source.upper()
    assert "INSERT IGNORE" not in source.upper()


def test_completion_gate_rejects_partial_load() -> None:
    checks = migration.expected_completion_checks()
    checks["block_rows"] -= 1

    with pytest.raises(RuntimeError, match="block_rows"):
        migration.assert_completion(checks)


def test_completion_gate_accepts_exact_contract() -> None:
    migration.assert_completion(migration.expected_completion_checks())
