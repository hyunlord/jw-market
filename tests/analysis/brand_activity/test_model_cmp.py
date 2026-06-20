from __future__ import annotations

from pipeline.scripts.analysis.brand_activity.model_cmp.market_groups import (
    build_market_group_model,
    filter_options_for_brand,
)
from pipeline.scripts.analysis.brand_activity.model_cmp.models import CsdPresence
from pipeline.scripts.analysis.brand_activity.model_cmp.quality import max_share_delta_pp, topic_overlap_score
from pipeline.scripts.analysis.brand_activity.model_cmp.response import normalize_share_payload, topics_from_payload


def test_market_group_model_preserves_source_markets_and_absent_members() -> None:
    model = build_market_group_model()

    assert len(model.groups) == 5
    livalo_group = model.groups["livalo_family"]
    source_markets = {market.source_market for market in livalo_group.source_markets}
    assert source_markets == {"LIVALO Market", "LIVALOZET Market"}
    assert livalo_group.atc4_set == ("C10A1", "C10C0")

    absent = [
        member.kr_brand
        for group in model.groups.values()
        for member in group.members
        if member.status is CsdPresence.ABSENT_IN_CSD
    ]
    assert absent == ["리바로하이", "피나스타", "제이다트"]


def test_filter_options_include_individual_source_market_and_group_union() -> None:
    model = build_market_group_model()

    options = filter_options_for_brand(model, "LIVALOZET")

    assert [option.option_id for option in options] == ["source:LIVALOZET Market", "group:livalo_family"]
    assert options[0].label == "리바로젯"
    assert options[0].source_markets == ("LIVALOZET Market",)
    assert options[1].label == "리바로+리바로젯"
    assert options[1].source_markets == ("LIVALO Market", "LIVALOZET Market")


def test_quality_metrics_compare_topic_axis_and_repeat_shares() -> None:
    flash_topics = ["강력한 지질 강하 효과", "복합제 병용 이점", "당뇨 안전성 및 대사 이점"]
    lite_topics = ["LDL-C 강하 효능", "복합제 처방 전략", "당뇨 안전성"]
    first_shares = {"T1": 45.7, "T2": 20.0, "기타": 0.0}
    second_shares = {"T1": 42.7, "T2": 23.0, "기타": 0.0}

    assert topic_overlap_score(flash_topics, lite_topics) > 0.2
    assert max_share_delta_pp(first_shares, second_shares) == 3.0


def test_response_normalization_accepts_common_model_schema_aliases() -> None:
    axis_payload = {
        "topic_axis": [
            {"id": "T1", "topic_label": "효능", "description": "효능 메시지", "representative_keywords": ["LDL-C"]}
        ]
    }
    share_payload = {
        "topic_distribution": [{"id": "T1", "topic_label": "효능", "percentage": 0.65, "count": 13}],
        "other_pct": 0.35,
    }

    topics = topics_from_payload(axis_payload, "fallback")
    normalized = normalize_share_payload(share_payload, brand="LIVALOZET", scope_id="group:livalo_family", row_count=20)

    assert topics[0].label == "효능"
    assert normalized["topic_shares"][0]["share_pct"] == 65.0
    assert normalized["etc_pct"] == 35.0
