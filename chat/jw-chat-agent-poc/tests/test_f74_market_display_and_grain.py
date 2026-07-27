from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from jw_chat_agent_poc.orchestrator.general_view_contract import enforce_general_view_contract
from jw_chat_agent_poc.orchestrator.markdown_renderers import market_members_md
from jw_chat_agent_poc.orchestrator.provenance_model import sanitize_internal_provenance_labels
from jw_chat_agent_poc.service.general_view_routing import GeneralViewService
from jw_chat_agent_poc.tools.general_view_backend import AtcCandidate, GeneralMarket, TopBrand


class _Backend:
    def __init__(self, market: GeneralMarket) -> None:
        self.market_value = market

    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        return (AtcCandidate(self.market_value.atc4_code, self.market_value.atc4_description),)

    def market(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket:
        return self.market_value


class _StrategicMembership:
    def resolve(self, question: str, allow_default: bool = False) -> SimpleNamespace:
        raise LookupError(question)

    def explicit_market(self, question: str) -> None:
        return None

    def market_members(self, question: str) -> tuple[str, ...]:
        return ()


def _market(
    *,
    source: str = "UBIST",
    atc4_code: str = "B02D",
    description: str = "FACTORS,2,7,9 AND 10",
    series: tuple[tuple[str, float], ...] = (),
    members: tuple[TopBrand, ...] = (),
) -> GeneralMarket:
    return GeneralMarket(
        view_type="general_view",
        market_basis="ATC4",
        atc4_code=atc4_code,
        atc4_description=description,
        source=source,
        measure="sales",
        unit="KRW",
        period=series[-1][0] if series else "2026-Q1",
        market_size=series[-1][1] if series else 10_000_000_000.0,
        brand="알프로릭스",
        brand_value=1_000_000_000.0,
        brand_share_pct=10.0,
        brand_rank=1,
        top_brands=members[:5],
        market_size_series=series,
        member_brands=members,
    )


def test_alfprolix_sales_does_not_publish_an_english_only_market_name() -> None:
    service = GeneralViewService(_Backend(_market()), _StrategicMembership(), enabled=True)

    result = service.answer("알프로릭스 매출", compact=False, dual=False)

    assert "- 시장: ATC4 B02D" in result["answer"]
    assert "- 시장: FACTORS,2,7,9 AND 10" not in result["answer"]


def test_iqvia_recent_year_uses_four_observed_quarters_without_monthly_interpolation() -> None:
    quarterly = (
        ("2025-Q2", 10_000_000_000.0),
        ("2025-Q3", 20_000_000_000.0),
        ("2025-Q4", 30_000_000_000.0),
        ("2026-Q1", 40_000_000_000.0),
    )
    service = GeneralViewService(
        _Backend(_market(source="IQVIA", series=quarterly)),
        _StrategicMembership(),
        enabled=True,
    )

    result = service.answer("알프로릭스 최근 1년 매출", compact=False, dual=False)

    contract = result["general_view_contract"]
    assert contract["period"] == "최근 4분기 합계 2025-Q2~2026-Q1"
    assert "1,000.0억원" in result["answer"]
    assert "월별 데이터가 부족" not in result["answer"]


def test_livalo_market_member_answer_keeps_a_human_readable_market_name() -> None:
    readable_shape = "고지혈증 시장의 구성 브랜드를 전략 mart에서 조회했습니다."
    internal_id_shape = "ml_006 시장의 구성 브랜드를 전략 mart에서 조회했습니다."

    public_answer = sanitize_internal_provenance_labels(readable_shape)

    assert public_answer == "고지혈증 시장의 구성 브랜드를 전략 mart에서 조회했습니다."
    assert sanitize_internal_provenance_labels(internal_id_shape).startswith("— 시장의")


def test_fifty_member_request_explains_the_fixed_twenty_row_policy() -> None:
    markdown = market_members_md(
        {
            "market_name": "고지혈증 시장",
            "period": "2026-05",
            "member_brands": [f"브랜드{index}" for index in range(1, 51)],
            "displayed_brand_count": 50,
            "total_brands_in_market": 555,
            "requested_limit": 50,
            "display_limit": 50,
            "limit_capped": False,
        }
    )

    assert "표시 상한" not in markdown
    assert "전체 555개 · 요청 50개 · 표시 50개" in markdown


def test_fifty_member_request_does_not_call_a_nine_brand_population_a_cap() -> None:
    members = tuple(
        TopBrand(brand=f"브랜드{index}", rank=index, value=None, share_pct=None)
        for index in range(1, 10)
    )
    service = GeneralViewService(
        _Backend(
            replace(
                _market(atc4_code="S01P0", description="안과용제", members=members),
                brand="아일리아",
            )
        ),
        _StrategicMembership(),
        enabled=True,
    )

    result = service.answer("아일리아 시장 구성 브랜드 50개", compact=False, dual=False)

    assert "전체 9개 · 요청 50개 · 표시 9개" in result["answer"]
    assert "요청 50개" in result["answer"]
    assert "전체 제공" in result["answer"]
    assert "표시 상한 9개" not in result["answer"]
    assert "상한" not in result["answer"]


def test_sanitized_general_view_contract_does_not_append_the_same_cap_notice_twice() -> None:
    section = (
        "## 일반뷰 (ATC4)\n\n"
        "ml_006 시장의 구성 브랜드를 전략 mart에서 조회했습니다.\n\n"
        "전체 50개 · 요청 50개 · 표시 50개"
    )
    contract = {
        "mode": "general_only",
        "section_markdown": section,
        "atc4_code": "C10A1",
        "source": "UBIST",
        "measure": "sales",
        "period": "2026-05",
    }
    already_sanitized = sanitize_internal_provenance_labels(section)

    answer = enforce_general_view_contract(already_sanitized, contract)

    assert answer.count("전체 50개 · 요청 50개 · 표시 50개") == 1
