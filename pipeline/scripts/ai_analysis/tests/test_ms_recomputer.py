import pytest

from bundle_builder.ms_recomputer import recompute_ms_pct


def test_recompute_ms_pct():
    assert recompute_ms_pct(100, 1000) == pytest.approx(10.0)
    assert recompute_ms_pct(11687229691.75, 131340000000.0) == pytest.approx(8.9, abs=0.1)


def test_recompute_ms_pct_zero_market():
    assert recompute_ms_pct(100, 0) is None
    assert recompute_ms_pct(100, None) is None
