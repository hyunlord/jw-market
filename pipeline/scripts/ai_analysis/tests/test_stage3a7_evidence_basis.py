from __future__ import annotations

from stage3a7_create_and_insert_ai_analysis import STAGES, align_stage_evidence_basis


def _payload():
    payload = {
        "analysis_variant": "short",
        "evidence_pool": [
            {"source": "event_brand_scores", "basis": "pool source has a separate meaning"},
        ],
    }
    for stage in STAGES:
        payload[stage] = {
            "title": f"{stage} title",
            "body": f"{stage} body",
            "evidence": [],
        }
    return payload


def test_align_stage_evidence_renames_source_only_items_to_basis():
    payload = _payload()
    payload["recommendation"]["evidence"] = [{"title": "근거", "source": "원문 근거"}]

    aligned = align_stage_evidence_basis(payload)

    assert aligned["recommendation"]["evidence"] == [{"title": "근거", "basis": "원문 근거"}]


def test_align_stage_evidence_keeps_existing_basis_when_source_is_also_present():
    payload = _payload()
    payload["cause"]["evidence"] = [{"title": "근거", "basis": "기존 basis", "source": "버릴 source"}]

    aligned = align_stage_evidence_basis(payload)

    assert aligned["cause"]["evidence"] == [{"title": "근거", "basis": "기존 basis"}]


def test_align_stage_evidence_does_not_mutate_input_or_evidence_pool():
    payload = _payload()
    payload["phenomenon"]["evidence"] = [{"title": "근거", "source": "stage source"}]

    aligned = align_stage_evidence_basis(payload)

    assert payload["phenomenon"]["evidence"] == [{"title": "근거", "source": "stage source"}]
    assert aligned["evidence_pool"] == payload["evidence_pool"]
    assert aligned["evidence_pool"][0]["source"] == "event_brand_scores"
    assert aligned["analysis_variant"] == "short"


def test_align_stage_evidence_ignores_non_stage_evidence_lists():
    payload = _payload()
    payload["recommendation_summary"] = {"evidence": [{"source": "not a stage"}]}

    aligned = align_stage_evidence_basis(payload)

    assert aligned["recommendation_summary"] == {"evidence": [{"source": "not a stage"}]}
