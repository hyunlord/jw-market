from __future__ import annotations

import json

import pymysql


def _conn():
    return pymysql.connect(
        host="127.0.0.1",
        port=3308,
        user="root",
        password="",
        database="jw_mart",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _deep_payload(brand: str) -> dict:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT response_json FROM cache_deep_analysis WHERE brand=%s", [brand])
        row = cur.fetchone()
    finally:
        conn.close()
    assert row, brand
    return json.loads(row["response_json"])


def test_phase302_simulation_uses_95_ci_natural_methods() -> None:
    payload = _deep_payload("가드메트")
    sim = payload["data"]["simulation"]["by_combo"]["UBIST.sales"]["by_brand"]["가드메트"]

    assert sim["horizon_ci_levels"] == {
        "1y": 0.95,
        "3y": 0.95,
        "5y": 0.95,
        "10y": 0.95,
        "method": "natural_accumulation_95_only",
        "note": "Phase 30.2: horizon 차등 제거, 모든 horizon 95% CI 자연 누적",
    }
    assert sim["scenarios"]["upper"]["method"] == "selected_model_ci_upper_95_natural"
    assert sim["scenarios"]["lower"]["method"] == "selected_model_ci_lower_95_natural"
    assert "anomaly_signals" not in sim
    assert "stress" not in sim


def test_phase302_guardmet_lower_no_longer_sticks_to_history_lowest() -> None:
    payload = _deep_payload("가드메트")
    sim = payload["data"]["simulation"]["by_combo"]["UBIST.sales"]["by_brand"]["가드메트"]
    base = sim["scenarios"]["base"]["values"]
    upper = sim["scenarios"]["upper"]["values"]
    lower = sim["scenarios"]["lower"]["values"]
    history_min = min(value for value in sim["history_values"] if value > 0)

    assert lower[11] < base[11] < upper[11]
    assert lower[35] < base[35] < upper[35]
    assert lower[11] > history_min * 1.5
    assert lower[35] > history_min * 1.5
