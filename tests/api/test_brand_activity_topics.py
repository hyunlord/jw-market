from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.main import app


INTERNAL_BRAND_KEYS = {
    "batching",
    "classified_row_count",
    "cross_insights",
    "denominator",
    "evidence_note",
    "partial_failure",
    "qc",
    "sample_key",
    "status",
    "topic_id_backfill_count",
    "unmatched_missing_topic_labels",
    "brand_specific_dedup_count",
    "brand_specific_dedup_log",
}


def test_topics_endpoint_projects_public_contract_when_rows_exist(monkeypatch) -> None:
    rows = [_row(f"scope:{index}") for index in range(11)]
    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", lambda _sql, _params=None: rows)

    response = TestClient(app).get("/api/brand-activity/topics")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data"}
    payload = body["data"]
    assert len(payload) == 11
    first = payload[0]
    assert set(first) == {"scope", "axis", "brands", "quality"}
    assert first["scope"] == {
        "scope_id": "scope:0",
        "display_name": "PPI Market",
        "atc4_values": ["A02B2"],
        "scope_type": "atc4",
        "quality_grade": "A",
        "avg_etc_pct": 4.2,
        "source_row_count": 123,
    }
    assert [topic["topic_id"] for topic in first["axis"]["topics"]] == ["T01", "T02"]
    assert [share["share_pct"] for share in first["brands"][0]["topic_shares"]] == [70.0, 25.0]
    assert first["brands"][0]["is_jw"] is False
    assert_public_brand_contract(first)


def test_topic_endpoint_returns_single_scope_when_scope_exists(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", lambda _sql, _params=None: [_row("atc4:A02B2")])

    response = TestClient(app).get("/api/brand-activity/topics/atc4:A02B2")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["scope"]["scope_id"] == "atc4:A02B2"
    assert payload["brands"][0]["brand"] == "JAQBO"


def test_topic_endpoint_returns_null_data_when_scope_is_missing(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", lambda _sql, _params=None: [])

    response = TestClient(app).get("/api/brand-activity/topics/missing-scope")

    assert response.status_code == 200
    assert response.json() == {"data": None, "reason": "scope_not_found", "scope_id": "missing-scope"}


def assert_public_brand_contract(payload: dict[str, Any]) -> None:
    """Assert that internal diagnostics are not exposed anywhere under brands."""
    brand = payload["brands"][0]
    assert set(brand) == {"brand", "is_jw", "etc_pct", "topic_shares", "brand_specific_topics"}
    assert set(brand["topic_shares"][0]) == {"topic_id", "label", "share_pct", "row_count"}
    assert set(brand["brand_specific_topics"][0]) == {"topic_id", "label", "definition", "share_pct", "row_count"}
    serialized = json.dumps(payload, ensure_ascii=False)
    for key in INTERNAL_BRAND_KEYS:
        assert key not in serialized


def _row(scope_id: str) -> dict[str, str]:
    payload = {
        "scope": {
            "scope_id": scope_id,
            "display_name": "PPI Market",
            "atc4_values": ["A02B2"],
            "scope_type": "atc4",
            "quality_grade": "A",
            "avg_etc_pct": 4.2,
            "axis_row_count": 123,
            "scope_key": scope_id,
            "reasons": ["ok"],
        },
        "axis": {
            "axis_version": "v1",
            "source_row_count": 123,
            "chunking": {"internal": True},
            "topics": [
                {"topic_id": "T02", "label": "복약 편의", "definition": "편의", "keywords": ["편의"], "single_concept_rewritten": False},
                {"topic_id": "T01", "label": "안전성", "definition": "안전", "keywords": ["안전"], "single_concept_rewritten": False},
            ],
        },
        "brands": [
            {
                "brand": "JAQBO",
                "is_jw": None,
                "etc_pct": 5.0,
                "topic_shares": [
                    {"topic_id": "T02", "label": "복약 편의", "share_pct": 25.0, "row_count": 5},
                    {"topic_id": "T01", "label": "안전성", "share_pct": 70.0, "row_count": 14},
                ],
                "brand_specific_topics": [
                    {"topic_id": "B1", "label": "브랜드 가치", "definition": "특화", "share_pct": 0.0, "row_count": 0, "source": "llm"}
                ],
                "qc": {"guard": "pass"},
                "denominator": 20,
                "cross_insights": {"internal": True},
                "topic_id_backfill_count": 1,
                "unmatched_missing_topic_labels": [],
                "brand_specific_dedup_count": 0,
                "brand_specific_dedup_log": [],
            }
        ],
        "quality": {"grade": "A", "avg_etc_pct": 4.2, "reasons": ["ok"]},
        "generated_from": {"run_id": "hidden"},
    }
    return {
        "scope_id": scope_id,
        "display_name": "PPI Market",
        "quality_grade": "A",
        "source_row_count": 123,
        "payload": json.dumps(payload, ensure_ascii=False),
    }
