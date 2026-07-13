from __future__ import annotations

import json
from typing import Any

import pytest

from pipeline.scripts.api import deep_analysis_runtime, deep_analysis_serving
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice
from pipeline.scripts.api.deep_analysis_context import DeepAnalysisContext
from pipeline.scripts.api.routes import deep_analysis


def _context(source: str = "ubist") -> DeepAnalysisContext:
    return DeepAnalysisContext(
        brand_key="마운자로",
        brand_name="마운자로",
        view_kind="strategic_ml",
        market_id="ml_003",
        market_name="당뇨 OAD",
        source="iqvia" if source == "iqvia_nsa" else "ubist",
        db_source=source,
        in_catalog=True,
        has_market_data=True,
        market_allowed_sources=("iqvia",),
        brand_available_sources=("iqvia_nsa",),
    )


def _block_row(*, simulation_available: int = 1, simulation_json: str | None = None) -> dict[str, Any]:
    return {
        "brand_key": "마운자로",
        "source": "iqvia_nsa",
        "market_id": "ml_003",
        "view_kind": "market_landscape",
        "forecast_json": json.dumps({"by_combo": {"IQVIA.sales": {"forecast_periods": ["2026-Q1"]}}}),
        "simulation_json": simulation_json
        if simulation_json is not None
        else json.dumps({"by_combo": {"IQVIA.sales": {"available_brands": [{"brand": "마운자로"}]}}}),
        "generation_status": "generated",
        "no_history_fallback": json.dumps({"IQVIA.sales": {"applied": False, "reason": "history_present"}}),
        "simulation_available": simulation_available,
    }


def test_forecast_block_rejects_simulation_marker_mismatch() -> None:
    row = _block_row(simulation_available=0)

    with pytest.raises(deep_analysis_serving.ForecastBlockInvariantError):
        deep_analysis_serving.parse_forecast_block(row)


def test_forecast_block_exposes_marker_reason_when_not_generated() -> None:
    row = _block_row(simulation_available=0)
    row["simulation_json"] = None
    row["generation_status"] = "no_history"

    block = deep_analysis_serving.parse_forecast_block(row)

    assert block.forecast["by_combo"]["IQVIA.sales"]
    assert block.simulation == {"available": False, "reason": "no_history"}


def test_forecast_block_uses_applied_no_history_fallback_reason() -> None:
    row = _block_row(simulation_available=0)
    row["simulation_json"] = None
    row["no_history_fallback"] = json.dumps(
        {"IQVIA.sales": {"applied": True, "reason": "no_history"}}
    )

    block = deep_analysis_serving.parse_forecast_block(row)

    assert block.simulation == {"available": False, "reason": "no_history"}


def test_forecast_block_preserves_anchor_and_source_horizons() -> None:
    ubist_periods = ["2026-05", *[f"future-month-{index:02d}" for index in range(1, 60)]]
    iqvia_periods = ["2026-Q2", *[f"future-quarter-{index:02d}" for index in range(1, 20)]]
    forecast = {
        "by_combo": {
            "UBIST.sales": {
                "baseline": {"last_history_period": "2026-05", "value_recent": 80.0},
                "forecast_periods": ubist_periods,
                "forecast_values": [80.0, *range(1, 60)],
            },
            "IQVIA.sales": {
                "baseline": {"last_history_period": "2026-Q2", "value_recent": 120.0},
                "forecast_periods": iqvia_periods,
                "forecast_values": [120.0, *range(1, 20)],
            },
        }
    }
    row = _block_row()
    row["forecast_json"] = json.dumps(forecast)

    block = deep_analysis_serving.parse_forecast_block(row)

    for combo_name, expected_count in (("UBIST.sales", 60), ("IQVIA.sales", 20)):
        combo = block.forecast["by_combo"][combo_name]
        assert len(combo["forecast_periods"]) == expected_count
        assert len(combo["forecast_values"]) == expected_count
        assert combo["forecast_periods"][0] == combo["baseline"]["last_history_period"]
        assert combo["forecast_values"][0] == combo["baseline"]["value_recent"]


def test_legacy_runtime_uses_precomputed_block_without_market_recalculation(monkeypatch) -> None:
    brand_row = {
        "brand_name": "마운자로",
        "brand_key": "마운자로",
        "ml_id": "ml_003",
        "source": "iqvia_nsa",
        "measure": "sales",
        "is_jw": 0,
        "is_target": 1,
        "computed_at": None,
    }
    block = deep_analysis_serving.parse_forecast_block(_block_row())
    monkeypatch.setattr(deep_analysis_runtime, "_brand_rows", lambda _brand: [brand_row])
    monkeypatch.setattr(
        deep_analysis_runtime,
        "_market_catalog",
        lambda _ml_id: {"name": "당뇨 OAD", "data_source": "iqvia"},
    )
    monkeypatch.setattr(deep_analysis_runtime, "_event_payload", lambda _brand: {"cut_a": [], "cut_b": []})
    monkeypatch.setattr(deep_analysis_runtime.builder, "atc_codes_from_market_catalog", lambda _market: ["A10S0"])
    monkeypatch.setattr(deep_analysis_runtime.builder, "source_list", lambda _source: ["IQVIA"])
    monkeypatch.setattr(deep_analysis_runtime, "load_forecast_block_by_key", lambda **_kwargs: block)
    monkeypatch.setattr(
        deep_analysis_runtime.builder,
        "combo_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy serving must not recalculate forecast")
        ),
    )

    row = deep_analysis_runtime.build_strategic_row("마운자로")

    assert row is not None
    data = json.loads(row["response_json"])["data"]
    assert data["forecast"] == block.forecast
    assert data["simulation"] == block.simulation
    assert set(data["forecast"]["by_combo"]) == set(data["simulation"]["by_combo"])


def test_dual_source_merge_preserves_unavailable_simulation_per_combo() -> None:
    iqvia = deep_analysis_serving.parse_forecast_block(_block_row())
    ubist_row = _block_row(simulation_available=0)
    ubist_row.update(
        {
            "source": "ubist",
            "forecast_json": json.dumps(
                {"by_combo": {"UBIST.sales": {"forecast_periods": ["2026-05"]}}}
            ),
            "simulation_json": None,
            "generation_status": "no_history",
        }
    )
    ubist = deep_analysis_serving.parse_forecast_block(ubist_row)

    forecast, simulation = deep_analysis_runtime._merge_block_payloads([iqvia, ubist])

    assert set(forecast["by_combo"]) == {"IQVIA.sales", "UBIST.sales"}
    assert simulation["by_combo"]["IQVIA.sales"]["available_brands"]
    assert simulation["by_combo"]["UBIST.sales"] == {
        "available": False,
        "reason": "no_history",
    }


def test_legacy_factor_resolution_reuses_formal_context_and_does_not_self_fallback(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = _context("iqvia_nsa")
    context_calls: list[dict[str, Any]] = []
    set_calls: list[dict[str, Any]] = []

    def fake_context(**kwargs: Any) -> DeepAnalysisContext:
        context_calls.append(kwargs)
        if kwargs["source"] == "ubist":
            raise deep_analysis.DeepAnalysisContextError(422, "source_not_available", "no UBIST")
        return context

    def fake_brand_set(**kwargs: Any) -> Any:
        set_calls.append(kwargs)
        return type(
            "Resolution",
            (),
            {
                "choices": (
                    BrandChoice("마운자로", "마운자로", 66, True),
                    BrandChoice("다이아벡스", "다이아벡스", 1, False),
                )
            },
        )()

    monkeypatch.setattr(deep_analysis, "resolve_deep_analysis_context", fake_context)
    monkeypatch.setattr(deep_analysis, "resolve_brand_set", fake_brand_set)

    choices, meta = deep_analysis._resolve_brand_factor_choices(
        {"brand": "마운자로", "brand_key": "마운자로", "market_id": "strategy_003"},
        "마운자로",
        None,
        {"atc": ["A10C1", "A10S0"]},
    )

    assert [choice.brand_key for choice in choices["iqvia"]] == ["마운자로", "다이아벡스"]
    assert choices["ubist"] == ()
    assert meta["ubist"] == {"available": False, "reason": "market_resolve_failed"}
    assert set_calls[0]["resolved_context"] is context
    assert set_calls[0]["view_name"] == "strategic_ml"
    assert set_calls[0]["market_id"] == "ml_003"
    assert all(call["market_id"] == "ml_003" for call in context_calls)
    assert "deep_analysis_brand_factor_market_resolve_failed" in caplog.text


def test_legacy_factor_resolution_logs_empty_strategic_result(
    monkeypatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(deep_analysis, "resolve_deep_analysis_context", lambda **_kwargs: _context("iqvia_nsa"))
    monkeypatch.setattr(
        deep_analysis,
        "resolve_brand_set",
        lambda **_kwargs: type("Resolution", (), {"choices": ()})(),
    )

    choices, meta = deep_analysis._resolve_brand_factor_choices(
        {"brand": "마운자로", "brand_key": "마운자로", "market_id": "strategy_003"},
        "마운자로",
        None,
        {"atc": ["A10C1", "A10S0"]},
    )

    assert choices == {"iqvia": (), "ubist": ()}
    assert all(value == {"available": False, "reason": "market_resolve_failed"} for value in meta.values())
    assert caplog.text.count("reason=no_choices") == 2
