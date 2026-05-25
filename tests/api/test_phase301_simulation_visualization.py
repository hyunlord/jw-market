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


def test_phase301_simulation_payload_has_no_anomaly_or_stress() -> None:
    payload = _deep_payload("가드메트")
    sim = payload["data"]["simulation"]["by_combo"]["UBIST.sales"]["by_brand"]["가드메트"]

    assert "anomaly_signals" not in sim
    assert "stress" not in sim


def test_phase301_guardmet_ci_lower_stays_near_base_across_horizons() -> None:
    payload = _deep_payload("가드메트")
    sim = payload["data"]["simulation"]["by_combo"]["UBIST.sales"]["by_brand"]["가드메트"]
    base = sim["scenarios"]["base"]["values"]
    lower = sim["scenarios"]["lower"]["values"]
    upper = sim["scenarios"]["upper"]["values"]

    for label, idx, min_lower_pct, max_upper_pct in (
        ("1y", 11, -50.0, 50.0),
        ("3y", 35, -50.0, 50.0),
        ("5y", 59, -60.0, 60.0),
        ("10y", 119, -30.0, 30.0),
    ):
        delta_lower = (lower[idx] - base[idx]) / base[idx] * 100
        delta_upper = (upper[idx] - base[idx]) / base[idx] * 100
        assert delta_lower > min_lower_pct, (label, delta_lower)
        assert delta_upper < max_upper_pct, (label, delta_upper)
        assert lower[idx] < base[idx] < upper[idx], label


def test_phase301_event_regressor_remains_disabled() -> None:
    payload = _deep_payload("가드메트")
    sim = payload["data"]["simulation"]["by_combo"]["UBIST.sales"]["by_brand"]["가드메트"]

    assert sim["model"]["event_regressor"]["enabled"] is False
