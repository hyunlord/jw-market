from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.etl import general_forecast_full_generation as fullgen


def _payload(*, periods: int = 121, history: list[float] | None = None) -> dict:
    values = list(range(periods))
    history_values = [10.0, 20.0] if history is None else history
    return {
        "brand_key": "brand-key",
        "market_meta": {"atc4_code": "A01"},
        "data": {
            "forecast": {
                "by_combo": {
                    "UBIST.sales": {
                        "period_unit": "월",
                        "forecast_periods": [f"m-{index}" for index in values],
                        "brands": [
                            {
                                "brand": "브랜드",
                                "is_target": True,
                                "history_values": history_values,
                                "forecast_values": values,
                                "forecast_intervals": {
                                    "upper_95_natural": values,
                                    "upper_horizon_adaptive": values,
                                    "lower_95_natural": values,
                                    "lower_horizon_adaptive": values,
                                },
                            }
                        ],
                    }
                }
            },
            "simulation": {
                "by_combo": {
                    "UBIST.sales": {
                        "period_unit": "월",
                        "by_brand": {
                            "브랜드": {
                                "history_values": history_values,
                                "forecast_periods": [f"m-{index}" for index in values],
                                "forecast_values": values,
                                "scenarios": {
                                    "base": {"values": values},
                                    "upper": {"values": values},
                                    "lower": {"values": values},
                                },
                            }
                        },
                    }
                }
            },
        },
    }


def test_optimize_and_mark_payload_uses_api_horizon_and_emits_root_markers() -> None:
    payload = _payload()

    optimized = fullgen.optimize_and_mark_payload(payload)

    forecast_brand = optimized["data"]["forecast"]["by_combo"]["UBIST.sales"]["brands"][0]
    simulation_brand = optimized["data"]["simulation"]["by_combo"]["UBIST.sales"]["by_brand"]["브랜드"]
    assert len(forecast_brand["forecast_values"]) == 60
    assert len(simulation_brand["scenarios"]["base"]["values"]) == 60
    assert "upper_horizon_adaptive" not in forecast_brand["forecast_intervals"]
    assert "lower_horizon_adaptive" not in forecast_brand["forecast_intervals"]
    assert optimized["generation_status"] == "generated"
    assert optimized["history_length"] == {"UBIST.sales": 2}
    assert optimized["no_history_fallback"] == {
        "UBIST.sales": {"applied": False, "reason": "history_present"}
    }
    assert optimized["forecast_has_nonzero"] == {"UBIST.sales": True}
    assert optimized["simulation_available"] == {"UBIST.sales": True}
    assert optimized["payload_optimization"] == {
        "horizon": "5y_api_contract",
        "encoding": "compact_json",
        "deduplication": "identical_interval_aliases",
    }


def test_zero_history_marker_distinguishes_actual_zero_from_no_history_fallback() -> None:
    actual_zero = fullgen.optimize_and_mark_payload(_payload(history=[0.0, 0.0]))
    no_history = fullgen.optimize_and_mark_payload(_payload(history=[]))

    assert actual_zero["no_history_fallback"]["UBIST.sales"] == {
        "applied": False,
        "reason": "actual_zero_history",
    }
    assert no_history["no_history_fallback"]["UBIST.sales"] == {
        "applied": True,
        "reason": "no_history",
    }


def test_contract_gate_rejects_missing_forecast_and_scenario() -> None:
    missing_forecast = _payload(periods=0)
    missing_scenario = _payload()
    del missing_scenario["data"]["simulation"]["by_combo"]["UBIST.sales"]["by_brand"]["브랜드"]["scenarios"]["lower"]

    with pytest.raises(fullgen.ContractGateError, match="forecast_missing"):
        fullgen.optimize_validate_and_serialize(missing_forecast)
    with pytest.raises(fullgen.ContractGateError, match="scenario_missing:lower"):
        fullgen.optimize_validate_and_serialize(missing_scenario)


def test_completion_gate_rejects_partial_success() -> None:
    requested = {("a", "A01"), ("b", "B01")}

    with pytest.raises(fullgen.CompletionGateError, match="requested=2 generated=1 validated=1"):
        fullgen.assert_completion(requested, {("a", "A01")}, {("a", "A01")})


def test_checkpoint_round_trip_and_resume_filters_completed_keys(tmp_path: Path) -> None:
    checkpoint = fullgen.CheckpointStore(tmp_path / "checkpoint.json")
    checkpoint.record_batch({("a", "A01"), ("b", "B01")})

    reloaded = fullgen.CheckpointStore(tmp_path / "checkpoint.json")

    assert reloaded.completed_keys == {("a", "A01"), ("b", "B01")}
    assert reloaded.pending([("a", "A01"), ("c", "C01")]) == [("c", "C01")]


def test_resume_uses_validated_staging_rows_when_local_checkpoint_is_lost() -> None:
    requested = [("a", "A01"), ("b", "B01")]

    state = fullgen.resume_state(
        requested,
        staged_validated={("a", "A01")},
        checkpoint_keys=set(),
    )

    assert state.validated == {("a", "A01")}
    assert state.pending == [("b", "B01")]


def test_load_worklist_preserves_exact_physical_grain(tmp_path: Path) -> None:
    path = tmp_path / "worklist.tsv"
    path.write_text("brand_key\tatc4_code\nb\tB01\na\tA01\n", encoding="utf-8")

    assert fullgen.load_worklist(path) == [("b", "B01"), ("a", "A01")]


def test_atomic_swap_plan_renames_both_tables_and_has_reverse_command() -> None:
    plan = fullgen.atomic_swap_plan(
        live_main="cache_deep_analysis_general",
        stage_main="cache_deep_analysis_general_stage_x",
        backup_main="cache_deep_analysis_general_backup_x",
        live_helper="cache_market_forecast_general",
        stage_helper="cache_market_forecast_general_stage_x",
        backup_helper="cache_market_forecast_general_backup_x",
    )

    assert plan.forward.count("RENAME TABLE") == 1
    assert plan.forward.count(" TO ") == 4
    assert "cache_deep_analysis_general_stage_x" in plan.forward
    assert "cache_market_forecast_general_stage_x" in plan.forward
    assert plan.reverse.count("RENAME TABLE") == 1
    assert "cache_deep_analysis_general_backup_x" in plan.reverse
    assert "cache_market_forecast_general_backup_x" in plan.reverse


def test_atomic_swap_requires_explicit_confirmation() -> None:
    plan = fullgen.SwapPlan(forward="RENAME TABLE a TO b", reverse="RENAME TABLE b TO a")

    with pytest.raises(SystemExit, match="confirmed=True"):
        fullgen.execute_atomic_swap(object(), plan, confirmed=False)


def test_transform_cache_row_recomputes_payload_size() -> None:
    original = fullgen.builder.GeneralCacheRow(
        brand_key="brand-key",
        brand="브랜드",
        atc4_code="A01",
        market_id="general:A01",
        response_json=json.dumps(_payload(), ensure_ascii=False),
        payload_size=1,
        brand_factors="{}",
        source_computed_at=None,
        expires_at=None,
        is_stale=0,
        stale_reason=None,
        stale_marked_at=None,
    )

    transformed = fullgen.transform_cache_row(original)

    assert transformed.payload_size == len(transformed.response_json.encode("utf-8"))
    assert json.loads(transformed.response_json)["generation_status"] == "generated"
