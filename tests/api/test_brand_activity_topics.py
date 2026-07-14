from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from fastapi.testclient import TestClient
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api import brand_activity_topic_matrix as topic_matrix
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetResolution
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice, BrandMeta, ViewConfig
from pipeline.scripts.api.main import app
from pipeline.scripts.api.routes import brand_activity


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


@pytest.fixture(autouse=True)
def topic_period_bounds(monkeypatch) -> None:
    monkeypatch.setattr(
        brand_activity,
        "get_topic_period_bounds",
        lambda: {"available_start": "2024-06", "available_end": "2026-05"},
    )


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


def test_post_topics_route_wraps_filtered_brand_payload(monkeypatch) -> None:
    expected = {"scope": {"view": "general"}, "brands": []}
    captured: dict[str, Any] = {}

    def fake_get_topic_brand_payload(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return expected

    monkeypatch.setattr(brand_activity, "get_topic_brand_payload", fake_get_topic_brand_payload)

    response = TestClient(app).post(
        "/api/brand-activity/topics",
        json={
            "view": "general",
            "market_id": "C10A1",
            "selected_brand": "리바로",
            "filters": {"atc": {"atc4": ["C10A1"]}, "analysis_level": {"iqvia": {"audit_code": ["KHPA"]}}},
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "data": expected,
        "meta": {
            "period": {
                "start_date": "2024-06",
                "end_date": "2026-05",
                "available_start": "2024-06",
                "available_end": "2026-05",
            },
            "request_normalized": True,
        },
    }
    assert "market_id" not in captured
    assert captured["filters"]["atc4"] == ["C10A1"]
    assert captured["filters"]["analysis_level"] == {"iqvia": {"audit_code": ["KHPA"]}}
    assert captured["filters"]["channel_axis"] == {"iqvia": {"audit_code": ["KHPA"]}}


def test_post_topics_route_accepts_list_keyword_filters(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get_topic_brand_payload(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"scope": {"sliced": True}, "brands": []}

    monkeypatch.setattr(brand_activity, "get_topic_brand_payload", fake_get_topic_brand_payload)

    response = TestClient(app).post(
        "/api/brand-activity/topics",
        json={
            "view": "general",
            "market_id": "C10A1",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"]},
            "visit_location": ["의원", "병원"],
            "specialty": ["Cardio"],
            "interest": ["VERY USEFUL", "SOMEWHAT USEFUL"],
            "prescription_evolution": ["increase"],
        },
    )

    assert response.status_code == 200
    assert "market_id" not in captured
    assert captured["visit_location"] == ["의원", "병원"]
    assert captured["specialty"] == ["Cardio"]
    assert captured["interest"] == ["VERY USEFUL", "SOMEWHAT USEFUL"]
    assert captured["prescription_evolution"] == ["increase"]


def test_post_topics_route_accepts_canonical_period_and_returns_applied_bounds(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get_topic_brand_payload(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"scope": {"sliced": True}, "brands": [{"event_count": 4}]}

    monkeypatch.setattr(brand_activity, "get_topic_brand_payload", fake_get_topic_brand_payload)
    monkeypatch.setattr(
        brand_activity,
        "get_topic_period_bounds",
        lambda: {"available_start": "2024-06", "available_end": "2026-05"},
    )

    response = TestClient(app).post(
        "/api/brand-activity/topics",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"]},
            "start_date": "2025-02",
            "end_date": "2025-05",
        },
    )

    assert response.status_code == 200
    assert captured["period_start"] == "2025-02"
    assert captured["period_end"] == "2025-05"
    assert response.json()["meta"]["period"] == {
        "start_date": "2025-02",
        "end_date": "2025-05",
        "available_start": "2024-06",
        "available_end": "2026-05",
    }


def test_post_topics_route_keeps_legacy_period_keys_compatible(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        brand_activity,
        "get_topic_brand_payload",
        lambda payload: captured.update(payload) or {"scope": {"sliced": True}, "brands": [{"event_count": 1}]},
    )
    monkeypatch.setattr(
        brand_activity,
        "get_topic_period_bounds",
        lambda: {"available_start": "2024-06", "available_end": "2026-05"},
    )

    response = TestClient(app).post(
        "/api/brand-activity/topics",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"]},
            "period_start": "2025-02",
            "period_end": "2025-05",
        },
    )

    assert response.status_code == 200
    assert captured["start_date"] == "2025-02"
    assert captured["end_date"] == "2025-05"


def test_post_topics_route_resolves_open_period_against_available_bounds(monkeypatch) -> None:
    monkeypatch.setattr(
        brand_activity,
        "get_topic_brand_payload",
        lambda _payload: {"scope": {"sliced": True}, "brands": [{"event_count": 1}]},
    )
    monkeypatch.setattr(
        brand_activity,
        "get_topic_period_bounds",
        lambda: {"available_start": "2024-06", "available_end": "2026-05"},
    )
    client = TestClient(app)
    base = {"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}}

    start_only = client.post("/api/brand-activity/topics", json={**base, "start_date": "2025-02"}).json()
    end_only = client.post("/api/brand-activity/topics", json={**base, "end_date": "2025-05"}).json()
    unfiltered = client.post("/api/brand-activity/topics", json=base).json()

    assert start_only["meta"]["period"]["end_date"] == "2026-05"
    assert end_only["meta"]["period"]["start_date"] == "2024-06"
    assert unfiltered["meta"]["period"] == {
        "start_date": "2024-06",
        "end_date": "2026-05",
        "available_start": "2024-06",
        "available_end": "2026-05",
    }


def test_post_topics_route_rejects_invalid_or_reversed_period() -> None:
    client = TestClient(app)
    base = {"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}}

    invalid = client.post("/api/brand-activity/topics", json={**base, "start_date": "2025-2"})
    reversed_period = client.post(
        "/api/brand-activity/topics",
        json={**base, "start_date": "2025-06", "end_date": "2025-05"},
    )

    assert invalid.status_code == 422
    assert "YYYY-MM" in invalid.text
    assert reversed_period.status_code == 422


def test_post_topics_route_returns_empty_list_for_period_without_data(monkeypatch) -> None:
    monkeypatch.setattr(
        brand_activity,
        "get_topic_brand_payload",
        lambda _payload: {"scope": {"sliced": True}, "brands": [{"event_count": 0, "topic_shares": []}]},
    )
    monkeypatch.setattr(
        brand_activity,
        "get_topic_period_bounds",
        lambda: {"available_start": "2024-06", "available_end": "2026-05"},
    )

    response = TestClient(app).post(
        "/api/brand-activity/topics",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"]},
            "start_date": "2023-01",
            "end_date": "2023-02",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["brands"] == []
    assert response.json()["meta"]["reason"] == "no_data_in_period"


def test_topic_period_bounds_reads_indexable_month_extrema(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch_one(sql: str, params=None) -> dict[str, str]:
        captured["sql"] = sql
        captured["params"] = params
        return {"available_start": "2024-06", "available_end": "2026-05"}

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_one", fake_fetch_one)

    assert topic_matrix.get_topic_period_bounds() == {
        "available_start": "2024-06",
        "available_end": "2026-05",
    }
    assert "MIN(period_ym)" in captured["sql"]
    assert "MAX(period_ym)" in captured["sql"]
    assert "km_keyword_event_stage" in captured["sql"]
    assert captured["params"] is None


def test_post_topic_service_uses_assignment_rows_without_keyword_filters(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})

    def fake_fetch_all(sql: str, params: tuple[object, ...] | None = None) -> list[dict[str, Any]]:
        if "row_topic_assignment" not in sql:
            return [_post_topic_row()]
        if params and "LIPITOR" in params:
            return []
        assert "k.visit_location IN" not in sql
        assert "k.specialty IN" not in sql
        assert "k.period_ym >=" not in sql
        assert params == ("atc4:C10A1", "LIVALO", "atc4:C10A1", "brand_activity_replay_20260703_125045")
        return [
            {"topic_id": "T02", "affected_row_count": 616, "brand_total_rows": 1307, "share_pct": "47.13"},
            {"topic_id": "T01", "affected_row_count": 283, "brand_total_rows": 1307, "share_pct": "21.65"},
            {"topic_id": "B1", "affected_row_count": 94, "brand_total_rows": 1307, "share_pct": "7.19"},
        ]

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)

    payload = topic_matrix.get_topic_brand_payload({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}, "top_n": 1})

    assert payload is not None
    assert payload["scope"]["applied_filter"] == {"atc4": ["C10A1"]}
    assert payload["scope"]["sliced"] is False
    assert payload["scope"]["filter_effect"]["payload"] == "row_topic_assignment_unfiltered"
    assert payload["brands"][0]["brand_key"] == "리바로"
    assert payload["brands"][0]["event_count"] == 1307
    assert payload["brands"][0]["topics"] == [{"rank": 1, "topic_id": "T02", "label": "LDL 조절", "share_pct": 47.13, "row_count": 616}]
    assert payload["brands"][0]["topic_shares"] == payload["brands"][0]["topics"]
    assert payload["brands"][0]["brand_specific_topics"] == [
        {"topic_id": "B1", "label": "리바로 고유", "share_pct": 7.19, "row_count": 94, "definition": "리바로 특화"}
    ]
    assert payload["brands"][1]["brand_key"] == "리피토"
    assert payload["brands"][1]["topics"] == []


def test_post_topic_service_slices_topics_from_row_assignments(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})
    monkeypatch.setattr(topic_matrix, "_keyword_filter_domain", lambda _column: frozenset({"의원", "내과"}))

    def fake_fetch_all(sql: str, params: tuple[object, ...] | None = None) -> list[dict[str, Any]]:
        if "row_topic_assignment" not in sql:
            return [_post_topic_row()]
        if params and "LIPITOR" in params:
            return []
        assert "k.visit_location IN (%s)" in sql
        assert "k.specialty IN (%s)" in sql
        assert "k.period_ym >= %s" in sql
        assert "k.period_ym <= %s" in sql
        assert params == ("atc4:C10A1", "LIVALO", "의원", "내과", "2026-01", "2026-06", "atc4:C10A1", "brand_activity_replay_20260703_125045")
        return [
            {"topic_id": "T02", "affected_row_count": 3, "brand_total_rows": 4, "share_pct": "75.00"},
            {"topic_id": "B1", "affected_row_count": 2, "brand_total_rows": 4, "share_pct": "50.00"},
        ]

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)

    payload = topic_matrix.get_topic_brand_payload(
        {
            "view": "general",
            "market_id": "C10A1",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"]},
            "visit_location": "의원",
            "specialty": "내과",
            "period_start": "2026-01",
            "period_end": "2026-06",
            "top_n": 5,
        }
    )

    assert payload is not None
    assert payload["scope"]["sliced"] is True
    assert payload["scope"]["topic_set_version"] == "brand_activity_replay_20260703_125045"
    assert payload["scope"]["filter_effect"]["payload"] == "row_topic_assignment_filtered"
    assert payload["brands"][0]["event_count"] == 4
    assert payload["brands"][0]["topics"] == [{"rank": 1, "topic_id": "T02", "label": "LDL 조절", "share_pct": 75.0, "row_count": 3}]
    assert payload["brands"][0]["brand_specific_topics"] == [
        {"topic_id": "B1", "label": "리바로 고유", "share_pct": 50.0, "row_count": 2, "definition": "리바로 특화"}
    ]


def test_post_topic_service_accepts_list_filters_as_or_with_axis_and(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})
    monkeypatch.setattr(
        topic_matrix,
        "_keyword_filter_domain",
        lambda _column: frozenset({"의원", "내과", "순환기", "VERY USEFUL", "SOMEWHAT USEFUL", "increase"}),
    )

    def fake_fetch_all(sql: str, params: tuple[object, ...] | None = None) -> list[dict[str, Any]]:
        if "row_topic_assignment" not in sql:
            return [_post_topic_row()]
        if params and "LIPITOR" in params:
            return []
        assert "k.visit_location IN (%s)" in sql
        assert "k.specialty IN (%s, %s)" in sql
        assert "k.interest IN (%s, %s)" in sql
        assert "k.prescription_evolution IN (%s)" in sql
        assert params == (
            "atc4:C10A1",
            "LIVALO",
            "의원",
            "내과",
            "순환기",
            "VERY USEFUL",
            "SOMEWHAT USEFUL",
            "increase",
            "2026-01",
            "2026-06",
            "atc4:C10A1",
            "brand_activity_replay_20260703_125045",
        )
        return [{"topic_id": "T02", "affected_row_count": 3, "brand_total_rows": 4, "share_pct": "75.00"}]

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)

    payload = topic_matrix.get_topic_brand_payload(
        {
            "view": "general",
            "market_id": "C10A1",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"]},
            "visit_location": "의원",
            "specialty": ["내과", "순환기"],
            "interest": ["VERY USEFUL", "SOMEWHAT USEFUL"],
            "prescription_evolution": ["increase"],
            "period_start": "2026-01",
            "period_end": "2026-06",
        }
    )

    assert payload is not None
    assert payload["scope"]["sliced"] is True
    assert payload["scope"]["applied_topic_filters"] == {
        "visit_location": ["의원"],
        "specialty": ["내과", "순환기"],
        "interest": ["VERY USEFUL", "SOMEWHAT USEFUL"],
        "prescription_evolution": ["increase"],
        "period_start": "2026-01",
        "period_end": "2026-06",
    }
    assert payload["brands"][0]["topics"][0]["share_pct"] == 75.0


def test_post_topic_service_treats_empty_filter_lists_as_unsliced(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})

    def fake_fetch_all(sql: str, _params: tuple[object, ...] | None = None) -> list[dict[str, Any]]:
        if "row_topic_assignment" in sql:
            return [{"topic_id": "T02", "affected_row_count": 3, "brand_total_rows": 4, "share_pct": "75.00"}]
        return [_post_topic_row()]

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)

    payload = topic_matrix.get_topic_brand_payload(
        {
            "view": "general",
            "market_id": "C10A1",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"]},
            "visit_location": [],
            "specialty": [],
            "interest": [],
            "prescription_evolution": [],
        }
    )

    assert payload is not None
    assert payload["scope"]["sliced"] is False
    assert payload["scope"]["applied_topic_filters"] == {}


def test_post_topic_service_reads_keyword_filters_from_filters_envelope(monkeypatch) -> None:
    parsed = topic_matrix._parse_topic_request(
        {
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"], "specialty": ["Cardio", "Nephro"], "interest": "VERY USEFUL"},
        }
    )

    assert parsed["specialty"] == ("Cardio", "Nephro")
    assert parsed["interest"] == ("VERY USEFUL",)


def test_post_topic_service_rejects_unknown_filter_values(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "_keyword_filter_domain", lambda _column: frozenset({"Cardio"}))

    try:
        topic_matrix.get_topic_brand_payload(
            {
                "view": "general",
                "selected_brand": "리바로",
                "filters": {"atc4": ["C10A1"]},
                "specialty": ["Unknown"],
            }
        )
    except topic_matrix.TopicRequestError as exc:
        assert "unsupported specialty filter value: Unknown" in str(exc)
    else:
        raise AssertionError("unknown specialty should be rejected")


def assert_public_brand_contract(payload: dict[str, Any]) -> None:
    """Assert that internal diagnostics are not exposed anywhere under brands."""
    brand = payload["brands"][0]
    assert set(brand) == {"brand", "is_jw", "etc_pct", "topic_shares", "topics", "brand_specific_topics"}
    assert brand["topics"] == brand["topic_shares"]
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
                    {"topic_id": "T02", "label": "복약 편의", "share_pct": 25.0, "affected_row_count": 5},
                    {"topic_id": "T01", "label": "안전성", "share_pct": 70.0, "affected_row_count": 14},
                ],
                "brand_specific_topics": [
                    {"topic_id": "B1", "label": "브랜드 가치", "definition": "특화", "share_pct": 0.0, "affected_row_count": 0, "source": "llm"}
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


def _brand_set() -> BrandSetResolution:
    view = ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    brand_meta = {
        "리바로": BrandMeta("리바로", "리바로", ("LIVALO",), True),
        "리피토": BrandMeta("리피토", "리피토", ("LIPITOR",), False),
    }
    return BrandSetResolution(
        view_name="general",
        market_id="C10A1",
        selected_brand="리바로",
        view=view,
        market_row={"atc4_desc": "STATINS"},
        brand_rows=(),
        brand_meta=brand_meta,
        choices=(
            BrandChoice("리바로", "리바로", 3, True),
            BrandChoice("리피토", "리피토", 1, False),
        ),
        candidates=(),
        ranking_quarter="2025-Q4",
        applied_filter={"atc4": ["C10A1"]},
    )


def _post_topic_row() -> dict[str, str]:
    payload = {
        "scope": {"scope_id": "atc4:C10A1", "atc4_values": ["C10A1"]},
        "axis": {
            "topics": [
                {"topic_id": "T01", "label": "당뇨 안전성", "definition": "당뇨 안전성"},
                {"topic_id": "T02", "label": "LDL 조절", "definition": "LDL 조절"},
            ]
        },
        "brands": [
            {
                "brand": "LIVALO",
                "row_count": 473,
                "top5_topic_shares": [
                    {"topic_id": "T01", "label": "당뇨 안전성", "share_pct": 62.5, "affected_row_count": 616},
                    {"topic_id": "T02", "label": "LDL 조절", "share_pct": 20.0, "affected_row_count": 283},
                ],
                "brand_specific_topics": [
                    {"topic_id": "B1", "label": "리바로 고유", "definition": "리바로 특화", "share_pct": 12.5, "affected_row_count": 123}
                ],
            }
        ]
    }
    return {
        "scope_id": "atc4:C10A1",
        "display_name": "LIVALO Market",
        "quality_grade": "A",
        "source_row_count": "1",
        "run_id": "brand_activity_replay_20260703_125045",
        "payload": json.dumps(payload, ensure_ascii=False),
    }
