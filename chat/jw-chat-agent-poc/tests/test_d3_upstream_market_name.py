from __future__ import annotations

from jw_chat_agent_poc.orchestrator.market_answer_contract import (
    _complete_row,
    _provenance_rows,
    _replace_provenance,
)
from jw_chat_agent_poc.agent_loop.loop import _attach_market_display_names
from jw_chat_agent_poc.resolver.brand_resolver import BrandResolution


def _metric_call(
    *,
    brand: str,
    market_id: str,
    market_display_name: str | None,
) -> dict:
    render_data = {
        "brand": brand,
        "metric": "sales",
        "market_id": market_id,
        "market_name": market_id,
        "period": "2026-05",
        "source_label": "UBIST",
        "unit_label": "KRW",
        "query_spec": {
            "source": "ubist",
            "view": "market_landscape",
            "market": market_id,
            "filters": {"brand": brand, "period": "2026-05"},
            "metrics": ["sales"],
        },
    }
    if market_display_name is not None:
        render_data["market_display_name"] = market_display_name
    return {
        "tool": "get_brand_metric",
        "source": "UBIST",
        "render_data": render_data,
    }


def test_live_shaped_metric_row_uses_catalog_market_name() -> None:
    calls = (
        _metric_call(
            brand="리바로",
            market_id="ml_006",
            market_display_name="리바로 리바로젯",
        ),
    )

    raw_row = _provenance_rows(calls)[0]
    completed = _complete_row(
        raw_row,
        question="리바로 매출 알려줘",
        answer="리바로 매출을 확인했습니다.",
        unit="억원",
    )

    assert completed.market == "리바로 리바로젯"
    assert "ml_006" not in " ".join(completed.as_fields().values())


def test_different_market_brands_keep_their_catalog_market_names() -> None:
    calls = [
        _metric_call(brand="리바로", market_id="ml_006", market_display_name=None),
        _metric_call(brand="가드렛", market_id="ml_003", market_display_name=None),
    ]
    resolutions = (
        BrandResolution("리바로", "", (), (), None, None, False, "ml_006", "리바로 리바로젯"),
        BrandResolution("가드렛", "", (), (), None, None, False, "ml_003", "가드렛 가드메트"),
    )

    _attach_market_display_names(calls, resolutions)
    rows = _provenance_rows(calls)

    assert {(row.brand, row.market) for row in rows} == {
        ("리바로", "리바로 리바로젯"),
        ("가드렛", "가드렛 가드메트"),
    }
    assert not any("ml_" in " ".join(row.as_fields().values()) for row in rows)

    answer = _replace_provenance(
        "리바로와 가드렛의 점유율 변화 비교",
        "두 브랜드의 점유율 변화를 비교했습니다.",
        calls,
    )
    assert "리바로 리바로젯" in answer
    assert "가드렛 가드메트" in answer
    assert "ml_006" not in answer
    assert "ml_003" not in answer


def test_same_market_brands_share_the_same_public_market_without_collapsing_brand_rows() -> None:
    calls = [
        _metric_call(brand="리바로", market_id="ml_006", market_display_name=None),
        _metric_call(brand="리바로젯", market_id="ml_006", market_display_name=None),
    ]
    resolutions = (
        BrandResolution("리바로", "", (), (), None, None, False, "ml_006", "리바로 리바로젯"),
        BrandResolution("리바로젯", "", (), (), None, None, False, "ml_006", "리바로 리바로젯"),
    )

    _attach_market_display_names(calls, resolutions)
    rows = _provenance_rows(calls)

    assert len(rows) == 2
    assert {row.brand for row in rows} == {"리바로", "리바로젯"}
    assert {row.market for row in rows} == {"리바로 리바로젯"}


def test_unknown_market_keeps_the_existing_safe_literal() -> None:
    calls = (
        _metric_call(brand="미확인브랜드", market_id="ml_999", market_display_name=None),
    )

    raw_row = _provenance_rows(calls)[0]
    completed = _complete_row(
        raw_row,
        question="미확인브랜드 매출 알려줘",
        answer="",
        unit="억원",
    )

    assert completed.market == "요청 브랜드의 전략 시장"
    assert "ml_999" not in " ".join(completed.as_fields().values())


def test_internal_catalog_name_is_not_attached_as_a_public_label() -> None:
    calls = [
        _metric_call(brand="리바로", market_id="ml_006", market_display_name=None),
    ]
    resolutions = (
        BrandResolution("리바로", "", (), (), None, None, False, "ml_006", "ml_006"),
    )

    _attach_market_display_names(calls, resolutions)
    completed = _complete_row(
        _provenance_rows(calls)[0],
        question="리바로 매출 알려줘",
        answer="",
        unit="억원",
    )

    assert completed.market == "요청 브랜드의 전략 시장"
    assert "market_display_name" not in calls[0]["render_data"]


def test_catalog_name_is_not_attached_when_the_call_market_does_not_match() -> None:
    calls = [
        _metric_call(brand="리바로", market_id="ml_003", market_display_name=None),
    ]
    resolutions = (
        BrandResolution("리바로", "", (), (), None, None, False, "ml_006", "리바로 리바로젯"),
    )

    _attach_market_display_names(calls, resolutions)

    assert "market_display_name" not in calls[0]["render_data"]
