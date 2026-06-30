from __future__ import annotations

from datetime import datetime

from phase_zeta_runner.evidence_pool import build_evidence_pool
from stage3a7_create_and_insert_ai_analysis import (
    JW25_BRANDS,
    SelectedRun,
    build_ai_analysis,
    build_variant_ai_analysis,
    insert_ai_analysis,
)


class _RecordingCursor:
    rowcount = 1

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.calls.append((sql, params))


class _RecordingConnection:
    def __init__(self) -> None:
        self.cursor_obj = _RecordingCursor()

    def cursor(self) -> _RecordingCursor:
        return self.cursor_obj

    def commit(self) -> None:
        return None


def test_build_ai_analysis_preserves_stage_evidence_as_evidence_pool():
    run = SelectedRun(
        brand="테스트",
        run_id=123,
        status="ok",
        model_version="genos_workflow_217",
        created_at=datetime(2026, 6, 29),
        bundle_hash="bundle-hash",
        input_bundle={
            "event_bundle": {
                "events_brand_centric": [
                    {"title": f"뉴스 {idx}", "source": "뉴스", "summary": f"요약 {idx}"}
                    for idx in range(1, 8)
                ]
            }
        },
    )
    parsed = {
        "phenomenon": {
            "title": "현상",
            "body": "본문",
            "bullets": ["bullet"],
            "evidence": [{"title": "현상 근거", "basis": "수치 근거"}],
        },
        "cause": {
            "title": "원인",
            "body": "본문",
            "bullets": ["bullet"],
            "evidence": [{"title": "원인 근거", "source": "뉴스"}],
        },
        "prediction": {"title": "예측", "body": "본문", "bullets": ["bullet"]},
        "recommendation": {
            "title": "권고",
            "body": "본문",
            "bullets": ["bullet"],
            "evidence": [{"title": "권고 근거", "basis": "bundle 근거"}],
        },
    }

    payload = build_ai_analysis(run, parsed)

    assert len(payload["evidence_pool"]) >= 8
    assert payload["evidence_pool"][0]["title"] == "현상 근거"
    assert payload["phenomenon"]["evidence"][0]["title"] == "현상 근거"


def test_variant_evidence_pool_filters_forecast_horizons():
    parsed = {
        "phenomenon": {"title": "현상", "body": "본문", "bullets": [], "evidence": []},
        "cause": {"title": "원인", "body": "본문", "bullets": [], "evidence": []},
        "prediction": {"title": "예측", "body": "본문", "bullets": [], "evidence": []},
        "recommendation": {"title": "권고", "body": "본문", "bullets": [], "evidence": []},
    }
    bundle = {
        "forecast_simulation": {
            "by_view": {
                "ML.UBIST.sales": {
                    "horizon_1y": {"period": "2027-03", "base": 1000},
                    "horizon_3y": {"period": "2029-03", "base": 3000},
                    "horizon_5y": {"period": "2031-03", "base": 5000},
                }
            }
        }
    }

    short_pool = build_evidence_pool(parsed, bundle, analysis_variant="short", min_items=12)
    long_pool = build_evidence_pool(parsed, bundle, analysis_variant="long", min_items=12)

    assert any("2027-03" in item["title"] for item in short_pool)
    assert not any("2031-03" in item["title"] for item in short_pool)
    assert any("2031-03" in item["title"] for item in long_pool)
    assert any("2029-03" in item["title"] for item in long_pool)


def test_build_variant_ai_analysis_keeps_legacy_stage_shape_with_variant_metadata():
    run = SelectedRun(
        brand="테스트",
        run_id=456,
        status="ok",
        model_version="genos_workflow_217",
        created_at=datetime(2026, 6, 30),
        bundle_hash="bundle-hash",
        input_bundle={},
        analysis_variant="short",
    )
    parsed = {
        "phenomenon": {"title": "현상", "body": "본문", "bullets": ["bullet"]},
        "cause": {"title": "원인", "body": "본문", "bullets": ["bullet"]},
        "prediction": {"title": "예측", "body": "본문", "bullets": ["bullet"]},
        "recommendation": {"title": "권고", "body": "본문", "bullets": ["bullet"]},
    }

    payload = build_variant_ai_analysis(run, parsed, "short")

    assert payload["analysis_variant"] == "short"
    assert set(["phenomenon", "cause", "prediction", "recommendation"]) <= set(payload)
    assert payload["prediction"]["title"] == "예측"


def test_insert_ai_analysis_does_not_touch_variant_columns_without_variant_payloads():
    conn = _RecordingConnection()
    payloads = {
        brand: {
            "run_id_phase_zeta": idx,
            "phase_zeta_stage": "stage3a7",
        }
        for idx, brand in enumerate(JW25_BRANDS, start=1)
    }

    insert_ai_analysis(conn, payloads, {brand: "ml_001" for brand in JW25_BRANDS})

    sql, params = conn.cursor_obj.calls[0]
    assert "ai_analysis_short_json" not in sql
    assert "ai_analysis_long_json" not in sql
    assert len(params) == 3


def test_insert_ai_analysis_writes_variant_columns_when_variant_payloads_are_present():
    conn = _RecordingConnection()
    payloads = {
        brand: {
            "run_id_phase_zeta": idx,
            "phase_zeta_stage": "stage3a7",
        }
        for idx, brand in enumerate(JW25_BRANDS, start=1)
    }

    insert_ai_analysis(
        conn,
        payloads,
        {brand: "ml_001" for brand in JW25_BRANDS},
        short_payloads=payloads,
        long_payloads=payloads,
    )

    sql, params = conn.cursor_obj.calls[0]
    assert "ai_analysis_short_json" in sql
    assert "ai_analysis_long_json" in sql
    assert len(params) == 5
