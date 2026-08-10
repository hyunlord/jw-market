from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any

from fastapi.testclient import TestClient
import pymysql
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


def test_post_topics_route_preserves_general_market_scope_without_atc4(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get_topic_brand_payload(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"scope": {"view": "general"}, "brands": [{"event_count": 1}]}

    monkeypatch.setattr(brand_activity, "get_topic_brand_payload", fake_get_topic_brand_payload)

    response = TestClient(app).post(
        "/api/brand-activity/topics",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "filters": {
                "market_scope": {
                    "option_id": "group:livalo_family",
                    "member": "리바로",
                }
            },
        },
    )

    assert response.status_code == 200
    assert captured["filters"]["market_scope"] == {
        "option_id": "group:livalo_family",
        "member": "리바로",
    }


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


def test_post_topics_route_preserves_brand_skeleton_for_period_without_data(monkeypatch) -> None:
    skeleton = {
        "brand_key": "brand:livalo",
        "brand_name": "리바로",
        "company_name": "JW중외제약",
        "is_jw": True,
        "is_selected": True,
        "sales_rank": 1,
        "event_count": 0,
        "topic_shares": [],
        "topics": [],
        "brand_specific_topics": [],
    }
    monkeypatch.setattr(
        brand_activity,
        "get_topic_brand_payload",
        lambda _payload: {"scope": {"sliced": True}, "brands": [skeleton]},
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
    assert response.json()["data"]["brands"] == [skeleton]
    for brand in response.json()["data"]["brands"]:
        assert brand["event_count"] == 0
        assert brand["topic_shares"] == []
        assert brand["topics"] == []
        assert brand["brand_specific_topics"] == []
        assert brand["brand_name"]
    assert response.json()["meta"]["reason"] == "no_data_in_period"


def test_post_topics_route_does_not_report_period_filter_failure_for_unsliced_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        brand_activity,
        "get_topic_brand_payload",
        lambda _payload: {
            "scope": {"sliced": False},
            "brands": [{"brand_name": "크레스토", "event_count": 0}],
        },
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
            "start_date": "2025-04",
            "end_date": "2026-03",
        },
    )

    assert response.status_code == 200
    assert "reason" not in response.json()["meta"]


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


def test_sliced_topic_rows_bridge_reloaded_stage_ids_by_classified_hash(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch_all(sql: str, params=None) -> list[dict[str, Any]]:
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)

    result = topic_matrix._fetch_sliced_topic_rows(
        scope_id="atc4:C10A1",
        topic_set_version="topic-version",
        product_codes=("LIVALO",),
        visit_locations=(),
        specialties=(),
        interests=(),
        prescription_evolutions=(),
        period_start="2025-06",
        period_end="2026-05",
    )

    assert result == []
    sql = captured["sql"]
    assert "row_topic_assignment_status" in sql
    assert "a.topic_set_version = status.topic_set_version" in sql
    assert "a.scope_id = status.scope_id" in sql
    assert "status.row_id = scoped_rows.row_id" not in sql
    assert "scoped_rows.row_id = a.row_id" not in sql
    assert "a.row_id = status.row_id" in sql
    assert "status.status = 'classified'" in sql
    assert "status.stage_row_sha256 = scoped_rows.stage_row_sha256" in sql
    assert "COUNT(DISTINCT scoped_rows.row_id) AS affected_row_count" in sql
    assert "source_row_count" in sql
    assert "classified_row_count" in sql
    assert "guard_valid_row_count" in sql
    assert "LEFT JOIN topic_totals" in sql


def test_sliced_topic_identity_mismatch_reports_filter_unavailable(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        topic_matrix,
        "_fetch_sliced_topic_rows",
        lambda **_kwargs: [
            {
                "topic_id": None,
                "affected_row_count": 0,
                "brand_total_rows": 686,
                "share_pct": None,
                "source_row_count": 686,
                "classified_row_count": 686,
                "guard_valid_row_count": 0,
            }
        ],
    )

    with caplog.at_level("WARNING"):
        item = topic_matrix._sliced_topic_brand_item(
            _brand_set(),
            choice_key="리바로",
            topic_scope=_group_topic_row(),
            topic_index={},
            request={},
            aliases={},
            product_codes=("LIVALO",),
            top_n=5,
        )

    assert item["event_count"] == 0
    assert item["topic_shares"] == []
    assert item["data_status"] == {
        "code": "identity_mismatch",
        "label": "필터 적용 불가",
        "source_row_count": 686,
        "classified_row_count": 686,
        "guard_valid_row_count": 0,
    }
    assert "brand=리바로" in caplog.text
    assert "source_rows=686" in caplog.text
    assert "classified_rows=686" in caplog.text
    assert "guard_valid_rows=0" in caplog.text


def test_post_topic_service_uses_stored_payload_without_keyword_filters(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})

    topic_row = _post_topic_row()
    stored_payload = json.loads(topic_row["payload"])
    stored_brand = stored_payload["brands"][0]
    stored_brand["topic_shares"] = stored_brand.pop("top5_topic_shares")
    topic_row["payload"] = json.dumps(stored_payload, ensure_ascii=False)

    def fake_fetch_all(sql: str, params: tuple[object, ...] | None = None) -> list[dict[str, Any]]:
        assert "row_topic_assignment" not in sql
        return [topic_row]

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)

    payload = topic_matrix.get_topic_brand_payload({"view": "general", "selected_brand": "리바로", "filters": {"atc4": ["C10A1"]}, "top_n": 1})

    assert payload is not None
    assert payload["scope"]["applied_filter"] == {"atc4": ["C10A1"]}
    assert payload["scope"]["sliced"] is False
    assert payload["scope"]["filter_effect"]["payload"] == "mart_brand_activity_topics_unfiltered"
    assert payload["brands"][0]["brand_key"] == "리바로"
    assert payload["brands"][0]["event_count"] == 473
    assert payload["brands"][0]["topics"] == [{"rank": 1, "topic_id": "T01", "label": "당뇨 안전성", "share_pct": 62.5, "row_count": 616}]
    assert payload["brands"][0]["topic_shares"] == payload["brands"][0]["topics"]
    assert payload["brands"][0]["brand_specific_topics"] == [
        {"topic_id": "B1", "label": "리바로 고유", "share_pct": 12.5, "row_count": 123, "definition": "리바로 특화"}
    ]
    assert payload["brands"][1]["brand_key"] == "리피토"
    assert payload["brands"][1]["topics"] == []
    assert payload["brands"][1]["data_status"] == {"code": "source_absent", "label": "데이터 없음"}


def test_post_topic_service_treats_default_period_as_unsliced(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})

    def fake_fetch_all(sql: str, _params: tuple[object, ...] | None = None) -> list[dict[str, Any]]:
        assert "row_topic_assignment" not in sql
        return [_post_topic_row()]

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)

    payload = topic_matrix.get_topic_brand_payload(
        {
            "view": "general",
            "selected_brand": "리바로",
            "filters": {"atc4": ["C10A1"]},
            "visit_location": "전체",
            "specialty": "전체",
            "interest": "전체",
            "prescription_evolution": "전체",
            "period_start": "2025-04",
            "period_end": "2026-03",
        }
    )

    assert payload is not None
    assert payload["scope"]["sliced"] is False
    assert payload["scope"]["applied_topic_filters"] == {}
    assert payload["scope"]["filter_effect"] == {
        "brand_set": "base",
        "payload": "mart_brand_activity_topics_unfiltered",
        "period": "not_applied_to_unfiltered_payload",
    }


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
        assert params == (
            "atc4:C10A1",
            "LIVALO",
            "의원",
            "내과",
            "2026-01",
            "2026-06",
            "brand_activity_replay_20260703_125045",
            "atc4:C10A1",
            "brand_activity_replay_20260703_125045",
            "atc4:C10A1",
            "atc4:C10A1",
            "brand_activity_replay_20260703_125045",
        )
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
            "brand_activity_replay_20260703_125045",
            "atc4:C10A1",
            "brand_activity_replay_20260703_125045",
            "atc4:C10A1",
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


@pytest.mark.parametrize(
    ("option_id", "member"),
    (
        ("group:livalo_family", "리바로"),
        ("group:gardlet_family", "가드렛"),
    ),
)
def test_post_topic_service_accepts_general_market_scope_without_atc4(option_id: str, member: str) -> None:
    parsed = topic_matrix._parse_topic_request(
        {
            "view": "general",
            "selected_brand": member,
            "filters": {
                "market_scope": {
                    "option_id": option_id,
                    "member": member,
                }
            },
        }
    )

    assert parsed["market_id"] == ""
    assert parsed["filter"]["market_scope"] == {
        "option_id": option_id,
        "member": member,
    }


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


def _strategic_brand_set(
    *,
    empty_product_codes: bool = False,
    include_crestor: bool = False,
) -> BrandSetResolution:
    base = _brand_set()
    brand_meta = {
        key: BrandMeta(
            meta.brand_key,
            meta.brand_name,
            () if empty_product_codes else meta.product_codes,
            meta.is_jw,
        )
        for key, meta in base.brand_meta.items()
    }
    choices = list(base.choices)
    if include_crestor:
        brand_meta["크레스토"] = BrandMeta("크레스토", "크레스토", ("CRESTOR",), False)
        choices.append(BrandChoice("크레스토", "크레스토", 8, False))
    return BrandSetResolution(
        view_name="strategic_ml",
        market_id="ml_006",
        selected_brand=base.selected_brand,
        view=ViewConfig(
            "mart_strategic_ml_brand_metric",
            "mart_strategic_ml_market_metric",
            "ml_id",
            "ml_name",
            "brand_ranking_stacked",
            True,
        ),
        market_row={"ml_name": "리바로 리바로젯"},
        brand_rows=(),
        brand_meta=brand_meta,
        choices=tuple(choices),
        candidates=(),
        ranking_quarter=base.ranking_quarter,
        applied_filter={},
    )


def test_topic_scope_uses_catalog_atc_membership_not_payload_brand_membership(monkeypatch) -> None:
    brand_set = _brand_set()
    strategic = BrandSetResolution(
        view_name="strategic_ml",
        market_id="ml_003",
        selected_brand=brand_set.selected_brand,
        view=ViewConfig(
            "mart_strategic_ml_brand_metric",
            "mart_strategic_ml_market_metric",
            "ml_id",
            "ml_name",
            "brand_ranking_stacked",
            True,
        ),
        market_row={"ml_name": "GUARDLET Market"},
        brand_rows=(),
        brand_meta=brand_set.brand_meta,
        choices=brand_set.choices,
        candidates=(),
        ranking_quarter="2025-Q4",
        applied_filter={},
    )
    monkeypatch.setattr(topic_matrix, "_catalog_atc4_values", lambda _brand_set: ("A10N1", "A10N3"))
    rows = [
        {
            "scope_id": "group:gardlet_family",
            "atc4_values": json.dumps(["A10N1", "A10N3"]),
            "payload": json.dumps(
                {"scope": {"scope_id": "group:gardlet_family"}, "brands": [{"brand": "UNRELATED"}]},
                ensure_ascii=False,
            ),
        }
    ]

    scope = topic_matrix._topic_scope(brand_set=strategic, topic_rows=rows)

    assert scope["scope_id"] == "group:gardlet_family"


def test_general_topic_scope_uses_full_applied_atc_membership() -> None:
    brand_set = replace(
        _brand_set(),
        applied_filter={"atc4": ["C10A1", "C10C0"]},
    )

    scope = topic_matrix._topic_scope(
        brand_set=brand_set,
        topic_rows=[_group_topic_row()],
    )

    assert scope["scope_id"] == "group:livalo_family"


def test_general_group_topic_scope_wins_over_member_scope() -> None:
    brand_set = replace(
        _brand_set(),
        applied_filter={"atc4": ["C10A1", "C10C0"]},
    )
    member_row = {
        "scope_id": "atc4:C10A1",
        "atc4_values": json.dumps(["C10A1"]),
        "payload": json.dumps(
            {"scope": {"scope_id": "atc4:C10A1"}, "brands": [{"brand": "LIVALO"}]},
            ensure_ascii=False,
        ),
    }

    scope = topic_matrix._topic_scope(
        brand_set=brand_set,
        topic_rows=[member_row, _group_topic_row()],
    )

    assert scope["scope_id"] == "group:livalo_family"


def test_general_single_atc_filter_resolves_containing_group_scope() -> None:
    brand_set = replace(
        _brand_set(),
        market_id="C10C0",
        applied_filter={"atc4": ["C10C0"]},
    )

    scope = topic_matrix._topic_scope(
        brand_set=brand_set,
        topic_rows=[_group_topic_row()],
    )

    assert scope["scope_id"] == "group:livalo_family"


def test_general_containing_group_scope_uses_tightest_then_lexical_priority() -> None:
    brand_set = replace(
        _brand_set(),
        market_id="C10C0",
        applied_filter={"atc4": ["C10C0"]},
    )
    broad = _topic_row_for_scope("group:a_broad", ["C10A1", "C10C0", "C10D1"])
    tight_later = _topic_row_for_scope("group:z_tight", ["C10A1", "C10C0"])
    tight_first = _topic_row_for_scope("group:a_tight", ["C10A1", "C10C0"])

    scope = topic_matrix._topic_scope(
        brand_set=brand_set,
        topic_rows=[broad, tight_later, tight_first],
    )

    assert scope["scope_id"] == "group:a_tight"


def test_general_containing_group_scope_still_wins_over_member_scope() -> None:
    brand_set = replace(
        _brand_set(),
        market_id="C10C0",
        applied_filter={"atc4": ["C10C0"]},
    )
    member = _topic_row_for_scope("atc4:C10C0", ["C10C0"])

    scope = topic_matrix._topic_scope(
        brand_set=brand_set,
        topic_rows=[member, _group_topic_row()],
    )

    assert scope["scope_id"] == "group:livalo_family"


def test_general_and_strategic_views_return_identical_topic_sets_and_ranks(monkeypatch) -> None:
    livalozet_meta = {
        "리바로젯": BrandMeta("리바로젯", "리바로젯", ("LIVALOZET",), True),
    }
    livalozet_choices = (
        BrandChoice("리바로젯", "리바로젯", 1, True),
    )
    general = replace(
        _brand_set(),
        market_id="C10C0",
        selected_brand="리바로젯",
        brand_meta=livalozet_meta,
        choices=livalozet_choices,
        applied_filter={"atc4": ["C10C0"]},
    )
    strategic = replace(
        _strategic_brand_set(),
        selected_brand="리바로젯",
        brand_meta=livalozet_meta,
        choices=livalozet_choices,
    )
    monkeypatch.setattr(
        topic_matrix,
        "resolve_brand_set",
        lambda **kwargs: general if kwargs["view_name"] == "general" else strategic,
    )
    monkeypatch.setattr(topic_matrix, "_fetch_topic_rows", lambda: [_group_topic_row()])
    monkeypatch.setattr(
        topic_matrix,
        "_catalog_atc4_values",
        lambda brand_set: (
            ("C10C0",)
            if brand_set.view_name == "general"
            else ("C10A1", "C10C0")
        ),
    )
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})
    monkeypatch.setattr(topic_matrix, "iqvia_product_codes_by_brand", lambda _brands: {})
    monkeypatch.setattr(topic_matrix, "_keyword_filter_domain", lambda _column: frozenset({"내과"}))
    monkeypatch.setattr(
        topic_matrix,
        "_fetch_sliced_topic_rows",
        lambda **_kwargs: [
            {
                "topic_id": "T01",
                "affected_row_count": 8,
                "brand_total_rows": 10,
                "share_pct": "80.00",
            },
            {
                "topic_id": "T02",
                "affected_row_count": 3,
                "brand_total_rows": 10,
                "share_pct": "30.00",
            },
        ],
    )

    general_result = topic_matrix.get_topic_brand_payload(
        {
            "view": "general",
            "selected_brand": "리바로젯",
            "filter": {"atc4": ["C10C0"]},
            "specialty": "내과",
        }
    )
    strategic_result = topic_matrix.get_topic_brand_payload(
        {
            "view": "strategic_ml",
            "market_id": "ml_006",
            "selected_brand": "리바로젯",
            "specialty": "내과",
        }
    )

    assert general_result is not None
    assert strategic_result is not None
    assert "reason" not in general_result
    assert all(brand["topics"] for brand in general_result["brands"])
    assert [brand["topics"] for brand in general_result["brands"]] == [
        brand["topics"] for brand in strategic_result["brands"]
    ]
    assert [brand["topic_shares"] for brand in general_result["brands"]] == [
        brand["topic_shares"] for brand in strategic_result["brands"]
    ]


def test_post_topic_service_uses_iqvia_product_codes_when_strategic_source_has_none(monkeypatch) -> None:
    strategic = _strategic_brand_set(empty_product_codes=True)
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: strategic)
    monkeypatch.setattr(topic_matrix, "_catalog_atc4_values", lambda _brand_set: ("C10A1", "C10C0"))
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})
    monkeypatch.setattr(topic_matrix, "_keyword_filter_domain", lambda _column: frozenset({"내과"}))
    monkeypatch.setattr(
        topic_matrix,
        "iqvia_product_codes_by_brand",
        lambda brands: {key: ("LIVALO",) if key == "리바로" else ("LIPITOR",) for key in brands},
        raising=False,
    )
    group_row = _group_topic_row()

    def fake_fetch_all(sql: str, params: tuple[object, ...] | None = None) -> list[dict[str, Any]]:
        if "row_topic_assignment" not in sql:
            return [group_row]
        if params and "LIPITOR" in params:
            return []
        assert params == (
            "group:livalo_family",
            "LIVALO",
            "내과",
            "2025-04",
            "2026-03",
            "brand_activity_group_replay",
            "group:livalo_family",
            "brand_activity_group_replay",
            "group:livalo_family",
            "group:livalo_family",
            "brand_activity_group_replay",
        )
        return [
            {"topic_id": "T01", "affected_row_count": 29, "brand_total_rows": 31, "share_pct": "93.55"},
            {"topic_id": "B1", "affected_row_count": 7, "brand_total_rows": 31, "share_pct": "22.58"},
        ]

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)

    result = topic_matrix.get_topic_brand_payload(
        {
            "view": "strategic_ml",
            "market_id": "ml_006",
            "selected_brand": "리바로",
            "specialty": "내과",
            "period_start": "2025-04",
            "period_end": "2026-03",
        }
    )

    assert result is not None
    assert result["scope"]["topic_set_version"] == "brand_activity_group_replay"
    assert result["brands"][0]["event_count"] == 31
    assert result["brands"][0]["brand_specific_topics"] == [
        {
            "topic_id": "B1",
            "label": "리바로 고유",
            "share_pct": 22.58,
            "row_count": 7,
            "definition": "리바로 특화",
        }
    ]


def test_post_topic_service_uses_brand_labels_from_resolved_scope_only(monkeypatch) -> None:
    base = _strategic_brand_set()
    brand_meta = dict(base.brand_meta)
    brand_meta["리바로"] = BrandMeta("리바로", "리바로", ("UBISTDIRECT",), True)
    strategic = BrandSetResolution(
        view_name=base.view_name,
        market_id=base.market_id,
        selected_brand=base.selected_brand,
        view=base.view,
        market_row=base.market_row,
        brand_rows=base.brand_rows,
        brand_meta=brand_meta,
        choices=base.choices,
        candidates=base.candidates,
        ranking_quarter=base.ranking_quarter,
        applied_filter=base.applied_filter,
    )
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: strategic)
    monkeypatch.setattr(topic_matrix, "_catalog_atc4_values", lambda _brand_set: ("C10A1", "C10C0"))
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})
    monkeypatch.setattr(topic_matrix, "_keyword_filter_domain", lambda _column: frozenset({"내과"}))
    monkeypatch.setattr(
        topic_matrix,
        "iqvia_product_codes_by_brand",
        lambda brands: {key: ("LIVALO",) if key == "리바로" else () for key in brands},
        raising=False,
    )
    resolved_scope = _group_topic_row()
    unrelated_scope = _post_topic_row()
    unrelated_payload = json.loads(unrelated_scope["payload"])
    unrelated_payload["scope"] = {"scope_id": "atc4:A02B2", "atc4_values": ["A02B2"]}
    unrelated_payload["brands"][0]["brand"] = "UBISTDIRECT"
    unrelated_payload["brands"][0]["brand_specific_topics"][0]["label"] = "다른 시장 고유"
    unrelated_scope = {
        **unrelated_scope,
        "scope_id": "atc4:A02B2",
        "atc4_values": json.dumps(["A02B2"]),
        "payload": json.dumps(unrelated_payload, ensure_ascii=False),
    }

    def fake_fetch_all(sql: str, params: tuple[object, ...] | None = None) -> list[dict[str, Any]]:
        if "row_topic_assignment" not in sql:
            return [resolved_scope, unrelated_scope]
        if params and "LIVALO" in params:
            assert "UBISTDIRECT" in params
            return [
                {"topic_id": "B1", "affected_row_count": 7, "brand_total_rows": 31, "share_pct": "22.58"}
            ]
        return []

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)

    result = topic_matrix.get_topic_brand_payload(
        {
            "view": "strategic_ml",
            "market_id": "ml_006",
            "selected_brand": "리바로",
            "specialty": "내과",
            "period_start": "2025-04",
            "period_end": "2026-03",
        }
    )

    assert result is not None
    assert result["brands"][0]["brand_specific_topics"][0]["label"] == "리바로 고유"


def test_post_topic_service_reads_assignments_for_brand_omitted_from_stored_payload(monkeypatch) -> None:
    brand_set = _strategic_brand_set(include_crestor=True)
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: brand_set)
    monkeypatch.setattr(topic_matrix, "_catalog_atc4_values", lambda _brand_set: ("C10A1", "C10C0"))
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})
    monkeypatch.setattr(topic_matrix, "_keyword_filter_domain", lambda _column: frozenset({"내과"}))
    monkeypatch.setattr(
        topic_matrix,
        "iqvia_product_codes_by_brand",
        lambda brands: {brand_key: () for brand_key in brands},
        raising=False,
    )

    def fake_fetch_all(sql: str, params: tuple[object, ...] | None = None) -> list[dict[str, Any]]:
        if "row_topic_assignment" not in sql:
            return [_group_topic_row()]
        if params and "CRESTOR" in params:
            return [
                {
                    "topic_id": "T01",
                    "affected_row_count": 254,
                    "brand_total_rows": 591,
                    "share_pct": "42.98",
                }
            ]
        return []

    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", fake_fetch_all)

    result = topic_matrix.get_topic_brand_payload(
        {
            "view": "strategic_ml",
            "market_id": "ml_006",
            "selected_brand": "리바로",
            "specialty": "내과",
            "period_start": "2025-04",
            "period_end": "2026-03",
        }
    )

    assert result is not None
    crestor = next(brand for brand in result["brands"] if brand["brand_key"] == "크레스토")
    assert crestor["event_count"] == 591
    assert crestor["topic_shares"] == [
        {
            "rank": 1,
            "topic_id": "T01",
            "label": "당뇨 안전성",
            "share_pct": 42.98,
            "row_count": 254,
        }
    ]


def test_missing_catalog_topic_scope_returns_explicit_reason(monkeypatch, caplog) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(topic_matrix, "_fetch_topic_rows", lambda: [])
    monkeypatch.setattr(topic_matrix, "_catalog_atc4_values", lambda _brand_set: ("C10A1",))
    monkeypatch.setattr(topic_matrix, "_company_names_by_brand", lambda *_a, **_k: {})

    with caplog.at_level("WARNING"):
        result = topic_matrix.get_topic_brand_payload(
            {
                "view": "general",
                "selected_brand": "리바로",
                "filter": {"atc4": ["C10A1"]},
            }
        )

    assert result is not None
    assert result["reason"] == "no_topic_scope:stored_scopes_missing"
    assert len(result["brands"]) == 2
    assert "reason=no_topic_scope:stored_scopes_missing" in caplog.text


def test_topic_query_failure_is_http_200_and_distinct_from_empty_data(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _brand_set())
    monkeypatch.setattr(
        topic_matrix,
        "_fetch_topic_rows",
        lambda: (_ for _ in ()).throw(pymysql.OperationalError(2006, "injected")),
    )
    monkeypatch.setattr(topic_matrix, "_company_names_by_brand", lambda *_a, **_k: {})

    response = TestClient(app).post(
        "/api/brand-activity/topics",
        json={
            "view": "general",
            "selected_brand": "리바로",
            "source": "IQVIA",
            "filters": {"atc4": ["C10A1"]},
        },
    )

    assert response.status_code == 200
    statuses = [brand["data_status"] for brand in response.json()["data"]["brands"]]
    assert statuses
    assert set(status["code"] for status in statuses) == {"unknown"}
    assert set(status["label"] for status in statuses) == {"모름"}


def test_topic_scope_failure_reason_distinguishes_missing_selection_from_mismatch(monkeypatch) -> None:
    rows = [_group_topic_row()]
    monkeypatch.setattr(topic_matrix, "_catalog_atc4_values", lambda _brand_set: ())

    assert topic_matrix._topic_scope_failure_reason(
        brand_set=_brand_set(),
        topic_rows=rows,
    ) == "no_topic_scope:selected_atc4_missing"

    monkeypatch.setattr(topic_matrix, "_catalog_atc4_values", lambda _brand_set: ("Z99Z9",))

    assert topic_matrix._topic_scope_failure_reason(
        brand_set=_brand_set(),
        topic_rows=rows,
    ) == "no_topic_scope:no_reachable_scope"


def test_cd_topic_scope_reads_atc_membership_through_ml_catalog(monkeypatch) -> None:
    brand_set = _brand_set()
    strategic_cd = BrandSetResolution(
        view_name="strategic_cd",
        market_id="cd_003",
        selected_brand=brand_set.selected_brand,
        view=ViewConfig(
            "mart_strategic_cd_brand_metric",
            "mart_strategic_cd_market_metric",
            "cd_market_id",
            "cd_market_name",
            "brand_ranking_stacked",
            True,
        ),
        market_row={"cd_market_name": "GUARDLET Market"},
        brand_rows=(),
        brand_meta=brand_set.brand_meta,
        choices=brand_set.choices,
        candidates=(),
        ranking_quarter="2025-Q4",
        applied_filter={},
    )
    captured: dict[str, object] = {}

    def fake_fetch_one(sql: str, params: tuple[str, ...]) -> dict[str, str]:
        captured.update({"sql": sql, "params": params})
        return {"atc_codes_json": '["A10N1", "A10N3"]'}

    monkeypatch.setattr(topic_matrix.db, "fetch_one", fake_fetch_one)

    assert topic_matrix._catalog_atc4_values(strategic_cd) == ("A10N1", "A10N3")
    assert "JOIN" in str(captured["sql"])
    assert "catalog_cd_market" in str(captured["sql"])
    assert "catalog_ml_market" in str(captured["sql"])
    assert captured["params"] == ("cd_003",)


def test_topic_scope_normalizes_catalog_and_stored_atc_codes(monkeypatch) -> None:
    brand_set = _brand_set()
    strategic = BrandSetResolution(
        view_name="strategic_ml",
        market_id="ml_001",
        selected_brand=brand_set.selected_brand,
        view=brand_set.view,
        market_row=brand_set.market_row,
        brand_rows=brand_set.brand_rows,
        brand_meta=brand_set.brand_meta,
        choices=brand_set.choices,
        candidates=brand_set.candidates,
        ranking_quarter=brand_set.ranking_quarter,
        applied_filter=brand_set.applied_filter,
    )
    monkeypatch.setattr(
        topic_matrix,
        "_catalog_atc4_values",
        lambda _brand_set: topic_matrix._atc4_values(["A2B2"]),
    )

    scope = topic_matrix._topic_scope(
        brand_set=strategic,
        topic_rows=[
            {
                "scope_id": "atc4:A02B2",
                "atc4_values": json.dumps(["A02B2"]),
                "payload": json.dumps({"scope": {"scope_id": "atc4:A02B2"}}),
            }
        ],
    )

    assert scope["scope_id"] == "atc4:A02B2"


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


def _group_topic_row() -> dict[str, str]:
    row = _post_topic_row()
    payload = json.loads(row["payload"])
    payload["scope"] = {
        "scope_id": "group:livalo_family",
        "atc4_values": ["C10A1", "C10C0"],
    }
    return {
        **row,
        "scope_id": "group:livalo_family",
        "run_id": "brand_activity_group_replay",
        "atc4_values": json.dumps(["C10A1", "C10C0"]),
        "payload": json.dumps(payload, ensure_ascii=False),
    }


def _topic_row_for_scope(scope_id: str, atc4_values: list[str]) -> dict[str, str]:
    row = _group_topic_row()
    payload = json.loads(row["payload"])
    payload["scope"] = {"scope_id": scope_id, "atc4_values": atc4_values}
    return {
        **row,
        "scope_id": scope_id,
        "atc4_values": json.dumps(atc4_values),
        "payload": json.dumps(payload, ensure_ascii=False),
    }
