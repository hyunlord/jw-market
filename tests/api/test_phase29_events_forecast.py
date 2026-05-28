from __future__ import annotations

import json
import subprocess

import pymysql

from pipeline.scripts.etl.cache_build_common import CANONICAL_25


def _conn():
    return pymysql.connect(
        host="127.0.0.1",
        port=3308,
        user="root",
        password="",
        database="jw_mart",
        cursorclass=pymysql.cursors.DictCursor,
    )


def test_phase29_agent1_tables_are_loaded() -> None:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM news_raw")
        news_count = int(cur.fetchone()["cnt"])
        cur.execute("SELECT COUNT(*) AS cnt FROM events_raw")
        events_raw_count = int(cur.fetchone()["cnt"])
        cur.execute("SELECT COUNT(*) AS cnt FROM event_brand_scores")
        score_count = int(cur.fetchone()["cnt"])
    finally:
        conn.close()

    assert news_count >= 21_000
    assert events_raw_count == news_count
    assert score_count >= 46_000


def test_phase29_cut_a_and_cut_b_contracts() -> None:
    from pipeline.scripts.etl.phase29_events import get_brand_events_cut_a, get_brand_events_cut_b

    conn = _conn()
    try:
        for brand in CANONICAL_25:
            cut_a, _, _ = get_brand_events_cut_a(conn, brand)
            assert len(cut_a) <= 50
            assert len(cut_a) >= 5, brand
            assert all(int(event["score"]) >= int(event["cut_threshold"]) for event in cut_a)

        # Cut B is intentionally strict and may be empty in the 6-month UI window.
        # The POC model may still use all-history Cut B events for backtesting.
        livalo_cut_b = get_brand_events_cut_b(conn, "리바로")
        assert all(event["derivation"] == "llm_direct" for event in livalo_cut_b)
        assert all(int(event["score"]) >= 80 for event in livalo_cut_b)

        livalo_all_history_cut_b = get_brand_events_cut_b(conn, "리바로", lookback_months=None)
        assert livalo_all_history_cut_b
        assert all(event["derivation"] == "llm_direct" for event in livalo_all_history_cut_b)
        assert all(int(event["score"]) >= 80 for event in livalo_all_history_cut_b)
    finally:
        conn.close()


def test_phase29_sarimax_poc_backtest_outputs_metrics() -> None:
    from pipeline.scripts.forecast.backtest import run_phase29_poc

    report = run_phase29_poc(use_llm=False, persist=False)
    assert set(report["brands"]) == {"리바로", "헴리브라"}

    for brand, result in report["brands"].items():
        assert result["history_points"] >= 18
        assert result["holdout_points"] > 0
        for model_key in ["baseline", "with_llm"]:
            metrics = result[model_key]["metrics"]
            assert metrics["rmse"] >= 0
            assert metrics["mae"] >= 0
            assert metrics["mape"] >= 0
            assert 0 <= metrics["direction_acc"] <= 1


def test_phase29_cache_contains_cut_a_b_and_phase30_simulation_for_livalo() -> None:
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT response_json FROM cache_deep_analysis WHERE brand='리바로'")
        payload = json.loads(cur.fetchone()["response_json"])
    finally:
        conn.close()

    events = payload["data"]["events"]
    assert set(events.keys()) >= {"cut_a", "cut_b"}
    assert 5 <= len(events["cut_a"]) <= 50
    # Recent Cut B markers are strict score>=80 direct-only events, so zero
    # markers is valid when no score-80 event exists in the last six months.
    assert all(event["score"] >= 80 and event["derivation"] == "llm_direct" for event in events["cut_b"])

    simulation = payload["data"]["simulation"]["by_combo"]["UBIST.sales"]
    assert simulation["phase30_baseline"] is True
    assert "리바로" in simulation["by_brand"]
    target = simulation["by_brand"]["리바로"]
    assert target["model"]["selection_policy"] == "data_size_dispatch_v1"
    assert target["model"]["event_regressor"]["enabled"] is False


def test_phase29_validation_pipeline_passes() -> None:
    result = subprocess.run(
        ["python3", "pipeline/scripts/validation/phase29_pipeline.py"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
