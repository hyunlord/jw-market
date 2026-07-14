from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.bq_fusion import BQEvidenceSlice, BQFusionError, BQFusionMode, BQFusionRequest, SourceKind, validate_fusion_request


def test_side_by_side_preserves_market_news_csd_hira_metadata_and_none_value() -> None:
    market = BQEvidenceSlice(
        kind=SourceKind.MARKET,
        source="UBIST",
        period="2026-04",
        unit="억원",
        view="전략뷰 (market_landscape)",
        market_definition="리바로/리바로젯 시장",
        scope="brand",
        evidence_refs=("qr_0001",),
        value=None,
    )
    news = BQEvidenceSlice.news("events/event_brand_scores", ("event_17",))
    csd = BQEvidenceSlice.csd("CSD ChannelDynamics", "2026-04", ("csd_1",))
    hira = BQEvidenceSlice.hira("hira_disease", "2024", "명", ("hira_1",))

    plan = validate_fusion_request(
        BQFusionRequest(BQFusionMode.SIDE_BY_SIDE, (market, news, csd, hira))
    )

    assert plan.mode is BQFusionMode.SIDE_BY_SIDE
    assert plan.slices == (market, news, csd, hira)
    assert plan.has_source_divergence is True
    assert plan.slices[0].value is None
    assert plan.slices[0].evidence_refs == ("qr_0001",)


def test_side_by_side_allows_ubist_iqvia_source_divergence_without_merging() -> None:
    ubist = BQEvidenceSlice.market("UBIST", "2026-04", "억원", "전략뷰", "ml_006", ("u1",))
    iqvia = BQEvidenceSlice.market("IQVIA", "2026-Q1", "억원", "전략뷰", "ml_006", ("i1",))

    plan = validate_fusion_request(
        BQFusionRequest(BQFusionMode.SIDE_BY_SIDE, (ubist, iqvia))
    )

    assert plan.has_source_divergence is True
    assert [item.source for item in plan.slices] == ["UBIST", "IQVIA"]


def test_aggregation_rejects_ubist_iqvia_source_divergence() -> None:
    ubist = BQEvidenceSlice.market("UBIST", "2026-04", "억원", "전략뷰", "ml_006", ("u1",))
    iqvia = BQEvidenceSlice.market("IQVIA", "2026-04", "억원", "전략뷰", "ml_006", ("i1",))

    with pytest.raises(BQFusionError, match="source"):
        validate_fusion_request(BQFusionRequest(BQFusionMode.AGGREGATE, (ubist, iqvia)))


def test_aggregation_rejects_file_market_but_side_by_side_preserves_both() -> None:
    file_slice = BQEvidenceSlice.file("업로드 파일(report.pdf)", ("file:p1",))
    market = BQEvidenceSlice.market("UBIST", "2026-04", "억원", "전략뷰", "ml_006", ("u1",))

    side_by_side = validate_fusion_request(
        BQFusionRequest(BQFusionMode.SIDE_BY_SIDE, (file_slice, market))
    )

    assert side_by_side.slices == (file_slice, market)
    with pytest.raises(BQFusionError, match="FILE\\+MARKET"):
        validate_fusion_request(
            BQFusionRequest(BQFusionMode.AGGREGATE, (file_slice, market))
        )


@pytest.mark.parametrize(
    "replacement",
    (
        {"period": "2026-03"},
        {"unit": "%"},
        {"market_definition": "다른 시장"},
        {"view": "일반뷰"},
        {"scope": "market"},
    ),
    ids=("period", "unit", "definition", "view", "scope"),
)
def test_aggregation_rejects_incompatible_context_fields(
    replacement: dict[str, str],
) -> None:
    base = BQEvidenceSlice.market("UBIST", "2026-04", "억원", "전략뷰", "ml_006", ("u1",))
    changed = BQEvidenceSlice.market(
        "UBIST",
        replacement.get("period", "2026-04"),
        replacement.get("unit", "억원"),
        replacement.get("view", "전략뷰"),
        replacement.get("market_definition", "ml_006"),
        ("u2",),
        scope=replacement.get("scope", "brand"),
    )

    with pytest.raises(BQFusionError, match="incompatible"):
        validate_fusion_request(BQFusionRequest(BQFusionMode.AGGREGATE, (base, changed)))
