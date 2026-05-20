from __future__ import annotations

from pipeline.scripts.api.cache.keys import (
    cache_key_brands,
    cache_key_cause,
    cache_key_deep_analysis,
    cache_key_market_status,
)
from pipeline.scripts.api.cache.loader import compute_168_variants
from pipeline.scripts.api.drivers import compute_drivers
from pipeline.scripts.api.models.cause import ExtendedMetricBlock


def test_compute_168_variants_matches_source_measure_view_matrix() -> None:
    variants = compute_168_variants()

    assert len(variants) == 168
    assert len({(v.brand_name, v.source, v.measure, v.view) for v in variants}) == 168
    assert sum(1 for v in variants if v.source == "UBIST") == 56
    assert sum(1 for v in variants if v.source == "IQVIA") == 112
    assert {v.view for v in variants} == {"market_landscape", "competitive_dynamics"}
    assert {"sales", "volume"} >= {v.measure for v in variants if v.source == "UBIST"}
    assert "volume" not in {v.measure for v in variants if v.source == "IQVIA"}


def test_cache_key_policy_stays_stable_for_phase_16f1b_design() -> None:
    assert cache_key_brands() == "brands_list"
    assert cache_key_market_status("2026-04") == "market_status:2026-04"
    assert (
        cache_key_cause("리바로", "market_landscape", "UBIST", "sales", "2026-04")
        == "cause:리바로:market_landscape:UBIST:sales:2026-04"
    )
    assert cache_key_deep_analysis("리바로", "2026-04") == "deep_analysis:리바로:2026-04"


def test_driver_mapping_uses_extended_metrics_and_skips_nulls() -> None:
    drivers = compute_drivers(
        {
            "ei_5y": 305.0,
            "momentum_score": -0.04,
            "growth_contribution": None,
            "hhi": 387.22,
            "market_cagr_5y": 0.0084,
        },
        view="market_landscape",
    )

    driver_types = {driver["type"] for driver in drivers}
    assert "evolution_outperform" in driver_types
    assert "momentum_down" in driver_types
    assert "growth_contributor" not in driver_types
    assert all(driver["value"] is not None for driver in drivers)


def test_extended_metric_block_defaults_are_nullable_and_canonical() -> None:
    block = ExtendedMetricBlock()

    assert block.metric_basis == "canonical_value"
    assert block.cagr_1y is None
    assert block.ei_5y is None
    assert block.growth_contribution is None
