from bundle_builder.mat_computer import compute_mat_12m_absolute, find_latest_actual_period


def test_mat_12m_absolute_full_12m():
    history = {f"2025-{m:02d}": {"raw_value": 100} for m in range(5, 13)}
    history.update({f"2026-{m:02d}": {"raw_value": 100} for m in range(1, 5)})
    history["2026-04"]["mat"] = 5.0
    result = compute_mat_12m_absolute(history, "2026-04")
    assert result["value"] == 1200
    assert result["growth_yoy_pct"] == 5.0
    assert result["missing_months"] == []


def test_mat_12m_with_missing():
    history = {"2026-04": {"raw_value": 100, "mat": 5.0}}
    result = compute_mat_12m_absolute(history, "2026-04")
    assert result["value"] == 100
    assert len(result["missing_months"]) == 11


def test_find_latest_actual_period():
    history = {"2025-12": {}, "2026-01": {}, "2026-04": {}}
    assert find_latest_actual_period(history) == "2026-04"


def test_find_latest_empty():
    assert find_latest_actual_period({}) is None
