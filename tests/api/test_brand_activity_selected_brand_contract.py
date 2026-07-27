from __future__ import annotations

import pymysql
import pytest

from pipeline.scripts.api import brand_activity_brand_resolver as resolver
from pipeline.scripts.api.brand_activity_brand_resolver import (
    competitor_status_payload,
    resolve_brand_set,
)
from pipeline.scripts.api.deep_analysis_context import DeepAnalysisContext


def _context(
    *,
    source: str = "iqvia",
    db_source: str = "iqvia_nsa",
    allowed: tuple[str, ...] = ("iqvia",),
) -> DeepAnalysisContext:
    return DeepAnalysisContext(
        brand_key="리바로",
        brand_name="리바로",
        view_kind="strategic_ml",
        market_id="ml_006",
        market_name="리바로 리바로젯",
        source=source,
        db_source=db_source,
        in_catalog=True,
        has_market_data=True,
        market_allowed_sources=allowed,
        brand_available_sources=(db_source,),
    )


def _market_row() -> dict[str, object]:
    return {
        "ml_id": "ml_006",
        "ml_name": "리바로 리바로젯",
        "brand_ranking_stacked": {"2026-Q1": []},
    }


def _brand_row(brand: str, sales: float) -> dict[str, object]:
    return {
        "brand_key": brand,
        "brand_name": brand,
        "is_jw": brand == "리바로",
        "by_dimension": {"products": [{"product_code": brand.upper()}]},
        "metric_history": {"2026-Q1": {"raw_value": sales}},
    }


def test_selected_brand_is_first_with_five_competitors(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_brand_row("리바로", 1), *[_brand_row(f"경쟁{i}", 100 - i) for i in range(1, 7)]]
    monkeypatch.setattr(resolver, "_resolve_strategic_brand_context", lambda *_args, **_kwargs: _context())
    monkeypatch.setattr(resolver, "_fetch_brand_rows", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(resolver, "_fetch_market_row", lambda *_args, **_kwargs: _market_row())

    result = resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_006",
        selected_brand="리바로",
        source="iqvia_nsa",
    )

    assert result is not None
    assert [choice.brand_key for choice in result.choices] == [
        "리바로",
        "경쟁1",
        "경쟁2",
        "경쟁3",
        "경쟁4",
        "경쟁5",
    ]
    assert competitor_status_payload(result) == {
        "competitors_available": True,
        "competitors_reason": "ok",
    }


def test_no_competitor_keeps_selected_brand_and_reports_none_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver, "_resolve_strategic_brand_context", lambda *_args, **_kwargs: _context())
    monkeypatch.setattr(resolver, "_fetch_brand_rows", lambda *_args, **_kwargs: [_brand_row("리바로", 1)])
    monkeypatch.setattr(resolver, "_fetch_market_row", lambda *_args, **_kwargs: _market_row())

    result = resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_006",
        selected_brand="리바로",
        source="iqvia_nsa",
    )

    assert result is not None
    assert [choice.brand_key for choice in result.choices] == ["리바로"]
    assert competitor_status_payload(result) == {
        "competitors_available": False,
        "competitors_reason": "none_matched",
    }


def test_unsupported_source_keeps_selected_brand_and_reports_catalog_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported = resolver.BrandSetInputError(
        "source is not available for the requested market",
        status_code=422,
        error="source_not_available",
    )

    def resolve_context(*_args: object, source: str | None, **_kwargs: object) -> DeepAnalysisContext:
        if source is not None:
            raise unsupported
        return _context(source="ubist", db_source="ubist", allowed=("ubist",))

    monkeypatch.setattr(resolver, "_resolve_strategic_brand_context", resolve_context)
    monkeypatch.setattr(resolver, "_fetch_brand_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(resolver, "_fetch_market_row", lambda *_args, **_kwargs: None)

    result = resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_006",
        selected_brand="리바로",
        source="iqvia_nsa",
    )

    assert result is not None
    assert [choice.brand_key for choice in result.choices] == ["리바로"]
    assert competitor_status_payload(result) == {
        "competitors_available": False,
        "competitors_reason": "source_unsupported",
    }


def test_lookup_failure_keeps_selected_brand_and_reports_lookup_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver, "_resolve_strategic_brand_context", lambda *_args, **_kwargs: _context())

    def fail_lookup(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise pymysql.OperationalError(2006, "injected lookup failure")

    monkeypatch.setattr(resolver, "_fetch_brand_rows", fail_lookup)

    result = resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_006",
        selected_brand="리바로",
        source="iqvia_nsa",
    )

    assert result is not None
    assert [choice.brand_key for choice in result.choices] == ["리바로"]
    assert competitor_status_payload(result) == {
        "competitors_available": False,
        "competitors_reason": "lookup_failed",
    }


def test_missing_general_market_preserves_not_found_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver, "_fetch_brand_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(resolver, "_fetch_market_row", lambda *_args, **_kwargs: None)

    result = resolve_brand_set(
        view_name="general",
        market_id="Z99Z9",
        selected_brand="없는브랜드",
        source="ubist",
    )

    assert result is None


def test_catalog_fallback_failure_is_not_reported_as_source_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported = resolver.BrandSetInputError(
        "source is not available for the requested market",
        status_code=422,
        error="source_not_available",
    )

    def resolve_context(*_args: object, source: str | None, **_kwargs: object) -> DeepAnalysisContext:
        if source is not None:
            raise unsupported
        raise pymysql.OperationalError(2006, "injected catalog fallback failure")

    monkeypatch.setattr(resolver, "_resolve_strategic_brand_context", resolve_context)

    result = resolve_brand_set(
        view_name="strategic_ml",
        market_id="ml_006",
        selected_brand="리바로",
        source="iqvia_nsa",
    )

    assert result is not None
    assert [choice.brand_key for choice in result.choices] == ["리바로"]
    assert competitor_status_payload(result) == {
        "competitors_available": False,
        "competitors_reason": "lookup_failed",
    }
