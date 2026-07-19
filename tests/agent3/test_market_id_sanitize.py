from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pipeline.scripts.agent3.json_util import canonical_json
from pipeline.scripts.agent3.market_loader import (
    Agent3MarketLoader,
    ExistingMarketState,
    MarketStrengthRecord,
    _count_market_id_keys,
    _reject_market_id_contamination,
    _strip_market_id_keys,
    canonical_market_content_matches,
    compute_market_input_hash,
    make_market_record,
)


def _contaminated_profile() -> dict:
    return {
        "brand": "가나플럭스",
        "brand_key": "가나플럭스",
        "market_id": "ml_001",
        "market_scope": {"latest_period": "2026-05", "member_count": 390},
        "source": "ubist",
        "sources": ["ubist"],
        "view_kind": "market_landscape",
    }


def _contaminated_candidates() -> list[dict]:
    return [
        {
            "brand": "가나플럭스",
            "market_id": "ml_001",
            "market_key": "market_landscape:ml_001:ubist",
            "latest_value": 81159532.03,
            "evidence": "strategic_scope.market_landscape.ml_001.ubist.2026-05",
        }
    ]


def _contaminated_summary() -> dict:
    return {
        "brand": "가나플럭스",
        "candidate_count": 1,
        "market_id": "ml_001",
        "profile_display": _contaminated_profile(),  # nested market_id
        "strength_items": [],
        "limitations": [],
    }


def test_strip_removes_market_id_at_all_depths_and_preserves_rest() -> None:
    original = _contaminated_summary()
    assert _count_market_id_keys(original) == 2  # top-level + nested profile_display
    cleaned = _strip_market_id_keys(original)
    assert _count_market_id_keys(cleaned) == 0
    # everything except market_id preserved, byte-for-byte under canonical serialization
    expected = {
        "brand": "가나플럭스",
        "candidate_count": 1,
        "profile_display": {
            "brand": "가나플럭스",
            "brand_key": "가나플럭스",
            "market_scope": {"latest_period": "2026-05", "member_count": 390},
            "source": "ubist",
            "sources": ["ubist"],
            "view_kind": "market_landscape",
        },
        "strength_items": [],
        "limitations": [],
    }
    assert canonical_json(cleaned) == canonical_json(expected)


def test_strip_removes_market_id_inside_list_elements() -> None:
    cleaned = _strip_market_id_keys(_contaminated_candidates())
    assert _count_market_id_keys(cleaned) == 0
    assert cleaned[0]["market_key"] == "market_landscape:ml_001:ubist"  # non-market_id key kept
    assert cleaned[0]["evidence"].endswith("2026-05")


def test_make_market_record_stores_clean_payloads() -> None:
    record = make_market_record(
        brand_key="가나플럭스",
        source="ubist",
        market_id="ml_001",
        view_kind="market_landscape",
        brand_name="가나플럭스",
        serving_brand_name="가나플럭스",
        profile=_contaminated_profile(),
        candidates=_contaminated_candidates(),
        summary=_contaminated_summary(),
        workflow_id=316,
        workflow_rev=5692,
        generation_status="market_position",
    )
    assert _count_market_id_keys(record.profile_json) == 0
    assert _count_market_id_keys(record.strength_candidates_json) == 0
    assert _count_market_id_keys(record.strength_summary_json) == 0
    # market_id COLUMN is untouched (structural PK)
    assert record.market_id == "ml_001"


def test_make_market_record_input_hash_unchanged_by_strip() -> None:
    profile, candidates, summary = _contaminated_profile(), _contaminated_candidates(), _contaminated_summary()
    record = make_market_record(
        brand_key="가나플럭스",
        source="ubist",
        market_id="ml_001",
        view_kind="market_landscape",
        brand_name="가나플럭스",
        serving_brand_name="가나플럭스",
        profile=profile,
        candidates=candidates,
        summary=summary,
        workflow_id=316,
        workflow_rev=5692,
        generation_status="market_position",
    )
    # hash must equal the hash of the ORIGINAL (pre-strip) profile/candidates so existing
    # stored input_hash values stay identical (no mass rewrite; same-hash skip preserved).
    expected = compute_market_input_hash(
        view_kind="market_landscape",
        market_id="ml_001",
        brand_key="가나플럭스",
        source="ubist",
        profile=profile,
        candidates=candidates,
        workflow_rev=5692,
    )
    assert record.input_hash == expected


def test_content_matches_is_clean_vs_clean_after_strip() -> None:
    record = make_market_record(
        brand_key="가나플럭스", source="ubist", market_id="ml_001", view_kind="market_landscape",
        brand_name="가나플럭스", serving_brand_name="가나플럭스",
        profile=_contaminated_profile(), candidates=_contaminated_candidates(), summary=_contaminated_summary(),
        workflow_id=316, workflow_rev=5692, generation_status="market_position",
    )
    # a prior row already sanitized in the DB (clean) must match the newly-built clean record
    old_clean = ExistingMarketState(
        view_kind="market_landscape",
        input_hash=record.input_hash,
        workflow_rev=5692,
        profile_json=_strip_market_id_keys(_contaminated_profile()),
        strength_candidates_json=_strip_market_id_keys(_contaminated_candidates()),
        strength_summary_json=_strip_market_id_keys(_contaminated_summary()),
    )
    assert canonical_market_content_matches(old_clean, record) is True


def _bypassed_contaminated_record() -> MarketStrengthRecord:
    """A record built WITHOUT make_market_record, so market_id keys survive in payloads."""
    return MarketStrengthRecord(
        brand_key="가나플럭스",
        source="ubist",
        market_id="ml_001",
        view_kind="market_landscape",
        brand_name="가나플럭스",
        serving_brand_name="가나플럭스",
        profile_json=_contaminated_profile(),  # NOT stripped
        strength_candidates_json=_contaminated_candidates(),
        strength_summary_json=_contaminated_summary(),
        workflow_id=316,
        workflow_rev=5692,
        input_hash="0" * 64,
        generation_status="market_position",
        generated_at=datetime.now(timezone.utc),
    )


def test_prewrite_gate_rejects_contaminated_record() -> None:
    with pytest.raises(ValueError, match="refusing market_id-contaminated write"):
        _reject_market_id_contamination([_bypassed_contaminated_record()])


def test_prewrite_gate_passes_clean_records() -> None:
    clean = make_market_record(
        brand_key="가나플럭스", source="ubist", market_id="ml_001", view_kind="market_landscape",
        brand_name="가나플럭스", serving_brand_name="가나플럭스",
        profile=_contaminated_profile(), candidates=_contaminated_candidates(), summary=_contaminated_summary(),
        workflow_id=316, workflow_rev=5692, generation_status="market_position",
    )
    _reject_market_id_contamination([clean])  # must not raise


def test_upsert_many_invokes_gate_before_db(monkeypatch: pytest.MonkeyPatch) -> None:
    # upsert_many must reject a contaminated record before opening any DB connection
    loader = Agent3MarketLoader.__new__(Agent3MarketLoader)

    def _boom(*_a, **_k):  # connect must never be reached
        raise AssertionError("DB connection attempted despite contaminated record")

    monkeypatch.setattr("pipeline.scripts.agent3.market_loader.connect", _boom)
    with pytest.raises(ValueError, match="refusing market_id-contaminated write"):
        loader.upsert_many([_bypassed_contaminated_record()])
