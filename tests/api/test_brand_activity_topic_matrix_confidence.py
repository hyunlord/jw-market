from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api import brand_activity_topic_matrix as topic_matrix
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetResolution
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice, BrandMeta, ViewConfig


def test_post_topic_service_emits_confidence_from_brand_row_count(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _confidence_brand_set())
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})
    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", lambda _sql, _params=None: [_confidence_topic_row()])

    payload = topic_matrix.get_topic_brand_payload({"view": "general", "market_id": "K01A3", "selected_brand": "플라주오피"})

    assert payload is not None
    brands = {brand["brand_key"]: brand for brand in payload["brands"]}
    assert brands["플라주오피"]["event_count"] == 1
    assert brands["플라주오피"]["confidence"] == "insufficient"
    assert brands["엔커버"]["event_count"] == 4
    assert brands["엔커버"]["confidence"] == "insufficient"
    assert brands["가스모틴"]["event_count"] == 31
    assert brands["가스모틴"]["confidence"] == "low"
    assert brands["가나칸"]["event_count"] == 34
    assert brands["가나칸"]["confidence"] == "low"
    assert brands["리바로브이"]["event_count"] == 50
    assert brands["리바로브이"]["confidence"] == "reliable"
    assert brands["리바로"]["event_count"] == 473
    assert brands["리바로"]["confidence"] == "reliable"
    assert brands["리피토"]["event_count"] == 990
    assert brands["리피토"]["confidence"] == "reliable"
    assert brands["미매칭"]["event_count"] == 0
    assert brands["미매칭"]["confidence"] == "insufficient"
    assert brands["미매칭"]["topics"] == []
    assert brands["플라주오피"]["topics"] == [{"rank": 1, "topic_id": "T01", "label": "수액", "share": 100.0}]


def test_post_topic_service_only_adds_confidence_keys_to_brand_contract(monkeypatch) -> None:
    monkeypatch.setattr(topic_matrix, "resolve_brand_set", lambda **_kwargs: _confidence_brand_set())
    monkeypatch.setattr(topic_matrix, "_alias_lookup", lambda: {})
    monkeypatch.setattr("pipeline.scripts.api.db.fetch_all", lambda _sql, _params=None: [_confidence_topic_row()])

    payload = topic_matrix.get_topic_brand_payload({"view": "general", "market_id": "K01A3", "selected_brand": "플라주오피"})

    assert payload is not None
    assert set(payload["brands"][0]) == {
        "brand_key",
        "brand_name",
        "is_jw",
        "is_selected",
        "sales_rank",
        "topics",
        "event_count",
        "confidence",
    }


def _confidence_brand_set() -> BrandSetResolution:
    view = ViewConfig("mart_general_brand_metric", "mart_general_market_metric", "atc4_code", "atc4_desc", "brand_ranking", False)
    brand_meta = {
        "플라주오피": BrandMeta("플라주오피", "플라주오피", ("PLAJU OP",), True),
        "엔커버": BrandMeta("엔커버", "엔커버", ("ENCOVER",), False),
        "가스모틴": BrandMeta("가스모틴", "가스모틴", ("GASMOTIN",), False),
        "가나칸": BrandMeta("가나칸", "가나칸", ("GANAKHAN",), False),
        "리바로브이": BrandMeta("리바로브이", "리바로브이", ("LIVALO V",), False),
        "리바로": BrandMeta("리바로", "리바로", ("LIVALO",), False),
        "리피토": BrandMeta("리피토", "리피토", ("LIPITOR",), False),
        "미매칭": BrandMeta("미매칭", "미매칭", ("NO STORED TOPIC",), False),
    }
    return BrandSetResolution(
        view_name="general",
        market_id="K01A3",
        selected_brand="플라주오피",
        view=view,
        market_row={"atc4_desc": "CONFIDENCE"},
        brand_rows=(),
        brand_meta=brand_meta,
        choices=(
            BrandChoice("플라주오피", "플라주오피", 1, True),
            BrandChoice("엔커버", "엔커버", 2, False),
            BrandChoice("가스모틴", "가스모틴", 3, False),
            BrandChoice("가나칸", "가나칸", 4, False),
            BrandChoice("리바로브이", "리바로브이", 5, False),
            BrandChoice("리바로", "리바로", 6, False),
            BrandChoice("리피토", "리피토", 7, False),
            BrandChoice("미매칭", "미매칭", 8, False),
        ),
        candidates=(),
        ranking_quarter="2025-Q4",
        applied_filter={"atc4": ["K01A3"]},
    )


def _confidence_topic_row() -> dict[str, str]:
    payload = {
        "brands": [
            {"brand": "PLAJU OP", "row_count": 1, "topic_shares": [_topic("T01", "수액", 100.0)]},
            {"brand": "ENCOVER", "row_count": 4, "topic_shares": [_topic("T01", "영양공급", 75.0)]},
            {"brand": "GASMOTIN", "row_count": 31, "topic_shares": [_topic("T01", "위장운동", 64.5)]},
            {"brand": "GANAKHAN", "row_count": 34, "topic_shares": [_topic("T01", "위장운동", 67.7)]},
            {"brand": "LIVALO V", "row_count": 50, "topic_shares": [_topic("T01", "당뇨 안전성", 50.0)]},
            {"brand": "LIVALO", "row_count": 473, "topic_shares": [_topic("T01", "당뇨 안전성", 62.5)]},
            {"brand": "LIPITOR", "row_count": 990, "topic_shares": [_topic("T01", "LDL 조절", 36.3)]},
        ]
    }
    return {
        "scope_id": "atc4:K01A3",
        "display_name": "PLAJU OP Market",
        "quality_grade": "A",
        "source_row_count": "1",
        "payload": json.dumps(payload, ensure_ascii=False),
    }


def _topic(topic_id: str, label: str, share_pct: float) -> dict[str, str | float | int]:
    return {"topic_id": topic_id, "label": label, "share_pct": share_pct, "row_count": 1}
