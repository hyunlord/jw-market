from __future__ import annotations

from datetime import datetime

from stage3a7_create_and_insert_ai_analysis import SelectedRun, build_ai_analysis


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
