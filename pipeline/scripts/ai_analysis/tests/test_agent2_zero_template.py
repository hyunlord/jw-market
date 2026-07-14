from __future__ import annotations

from bundle_builder.agent2_zero_template import (
    KpiSnapshot,
    ZeroNarrativeType,
    classify_zero_type,
    render_zero_template,
)


def test_growth_template_is_longer_and_uses_statistics_without_news_claims() -> None:
    snapshot = KpiSnapshot(
        brand="성장브랜드",
        market_name="고지혈증 시장",
        rank=8,
        share_pct=2.4,
        cagr_pct=12.3,
        ei=142.0,
        momentum=6.5,
        hhi=950.0,
    )

    rendered = render_zero_template(snapshot)

    assert classify_zero_type(snapshot) is ZeroNarrativeType.GROWTH
    assert rendered["phenomenon"]["is_template"] is True
    assert rendered["phenomenon"]["evidence_none"] is True
    assert rendered["phenomenon"]["template_type"] == "growth"
    assert rendered["phenomenon"]["body"].count(".") >= 2
    assert "관련 뉴스 없음" in rendered["phenomenon"]["title"]
    assert "12.3%" in rendered["phenomenon"]["body"]
    assert "142.0" in rendered["phenomenon"]["body"]
    assert rendered["phenomenon"] is not rendered["cause"]


def test_stable_template_stays_short() -> None:
    snapshot = KpiSnapshot(
        brand="정체브랜드",
        market_name="위식도역류질환 시장",
        rank=12,
        share_pct=1.1,
        cagr_pct=0.4,
        ei=98.0,
        momentum=0.2,
    )

    rendered = render_zero_template(snapshot)

    assert classify_zero_type(snapshot) is ZeroNarrativeType.STABLE
    assert rendered["phenomenon"]["template_type"] == "stable"
    assert rendered["phenomenon"]["body"].count("입니다.") == 1


def test_template_omits_missing_kpi_values_gracefully() -> None:
    snapshot = KpiSnapshot(brand="데이터빈약", market_name="시장")

    rendered = render_zero_template(snapshot)

    assert classify_zero_type(snapshot) is ZeroNarrativeType.INSUFFICIENT
    body = rendered["phenomenon"]["body"]
    assert "None" not in body
    assert "0.0" not in body
    assert rendered["phenomenon"]["template_type"] == "insufficient"
