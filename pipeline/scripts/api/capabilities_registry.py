"""Single source of truth for the ``GET /api/capabilities`` contract (E-2).

The capabilities document is *generated from the API's real response models*
(introspection) joined with ONE curated label/unit registry in this module.
Nothing is hand-duplicated across the codebase: the view enum, the metric
field ids, and the period anchors all resolve back to the symbols the API
actually uses, so the published contract cannot silently drift from the
served payloads.

Guarantees enforced by ``tests/api/test_capabilities.py``:
  * every numeric field on a metric group's response model has a label here
    (adding a KPI field to a model forces a registry entry — no silent gap);
  * every published metric id is a real field on that group's model;
  * the view enum equals the canonical triple used by the request models.
"""

from __future__ import annotations

import typing
from typing import Any

from pydantic import BaseModel

from pipeline.scripts.api.config import config
from pipeline.scripts.api.models.cause import (
    CauseSummary,
    ExtendedMetricBlock,
    MarketContext,
)
from pipeline.scripts.api.models.deep_analysis import DeepAnalysisChannelSpec
from pipeline.scripts.api.models.market_status import (
    BackExtendedMetrics,
    BackMetrics,
    FrontMetrics,
)

CONTRACT_VERSION = "1.0"

# --- Views (계약 문서 첫머리 고정; 신규 호출은 top-level ``view`` 사용) ----------------
# 요청 모델 ``DynamicMarketRequest.view`` 및 여러 서비스가 쓰는 정본 3종.
VIEWS: tuple[str, ...] = ("general", "strategic_ml", "strategic_cd")
# 레거시 ``view_kind`` → 신규 ``view`` 추론 매핑 (openapi_docs / validators.query_params).
LEGACY_VIEW_ALIASES: dict[str, str] = {
    "market_landscape": "strategic_ml",
    "competitive_dynamics": "strategic_cd",
}

# --- Period anchors: dynamic_market/period_window.py 정규식이 실제 수용하는 형태 -------
#   ^(YYYY)(-(MM|Q[1-4]))?$
PERIOD_ANCHORS: list[dict[str, str]] = [
    {"id": "annual", "pattern": "YYYY", "label": "연간"},
    {"id": "quarter", "pattern": "YYYY-Q[1-4]", "label": "분기"},
    {"id": "month", "pattern": "YYYY-MM", "label": "월"},
]
# 기간 앵커와 별개인 시리즈 형태(원인분석 10-포인트 트렌드 등).
SERIES_SHAPES: list[dict[str, str]] = [
    {"id": "ten_point", "label": "10-포인트 트렌드 시리즈"},
]

# --- Curated metric registry: field id -> (label, unit) -------------------------------
# unit vocabulary: pct | value | krw | index | score | rank
#   value = measure 의존 크기(sales→원, qty→수량)
_METRIC_LABELS: dict[str, tuple[str, str]] = {
    # 공통/원인분석
    "market_share": ("시장 점유율", "pct"),
    "rank_in_market": ("시장 내 순위", "rank"),
    "rank": ("시장 내 순위", "rank"),
    "mom": ("전월 대비 성장률(MoM)", "pct"),
    "qoq": ("전분기 대비 성장률(QoQ)", "pct"),
    "yoy": ("전년 대비 성장률(YoY)", "pct"),
    "mat": ("MAT(이동연간누계)", "value"),
    "growth_abs": ("절대 성장액", "value"),
    "cagr_1y": ("1년 CAGR", "pct"),
    "cagr_3y": ("3년 CAGR", "pct"),
    "cagr_5y": ("5년 CAGR", "pct"),
    "ei_5y": ("5년 초과성장지수(EI)", "index"),
    "momentum_score": ("모멘텀 스코어", "score"),
    "growth_contribution": ("성장 기여도", "pct"),
    "hhi": ("HHI 시장집중도", "index"),
    "market_cagr_5y": ("시장 5년 CAGR", "pct"),
    # dynamic-market (market_status FrontMetrics/BackMetrics/BackExtendedMetrics)
    "value_recent": ("최근 실적 값", "value"),
    "ms_recent_pct": ("최근 점유율", "pct"),
    "gr_mom_pct": ("MoM 성장률", "pct"),
    "gr_qoq_pct": ("QoQ 성장률", "pct"),
    "gr_yoy_pct": ("YoY 성장률", "pct"),
    "gr_yoy_mat_pct": ("YoY MAT 성장률", "pct"),
    "gr_yoy_ym_pct": ("YoY 동월 성장률", "pct"),
    "ms_change_yoy_pct": ("점유율 YoY 변화", "pct"),
    "cagr_5y_pct": ("5년 CAGR", "pct"),
    "sales_first_period_krw": ("최초 기간 매출", "krw"),
    "ms_first_period_pct": ("최초 기간 점유율", "pct"),
    "market_size_recent": ("최근 시장 규모", "value"),
    "market_cagr_5y_pct": ("시장 5년 CAGR", "pct"),
    "market_cagr_3y_pct": ("시장 3년 CAGR", "pct"),
    "target_brand_sales": ("선택 브랜드 최근 매출", "value"),
    "brand_cagr_5y_pct": ("브랜드 5년 CAGR", "pct"),
    "brand_cagr_3y_pct": ("브랜드 3년 CAGR", "pct"),
    "excess_growth_pct": ("초과 성장률", "pct"),
}

# 명시적 순위 필드(정수형이므로 numeric 규칙과 별개로 metric 취급).
_RANK_FIELDS: frozenset[str] = frozenset({"rank", "rank_in_market"})

# 그룹 -> introspect 대상 응답 모델(정본).
_GROUP_MODELS: dict[str, tuple[type[BaseModel], ...]] = {
    "cause": (CauseSummary, ExtendedMetricBlock, MarketContext),
    "dynamic-market": (FrontMetrics, BackMetrics, BackExtendedMetrics),
    "deep-analysis": (DeepAnalysisChannelSpec,),
}


def _is_numeric(annotation: Any) -> bool:
    """True for ``float`` or ``float | None`` field annotations."""

    if annotation is float:
        return True
    return float in typing.get_args(annotation)


def metric_field_ids(models: tuple[type[BaseModel], ...]) -> list[str]:
    """Introspect the given response models for metric field ids (drift source)."""

    ids: list[str] = []
    seen: set[str] = set()
    for model in models:
        for name, field in model.model_fields.items():
            if name in seen:
                continue
            if name in _RANK_FIELDS or _is_numeric(field.annotation):
                seen.add(name)
                ids.append(name)
    return ids


def _metrics_for(models: tuple[type[BaseModel], ...]) -> list[dict[str, str]]:
    metrics: list[dict[str, str]] = []
    for field_id in metric_field_ids(models):
        label, unit = _METRIC_LABELS[field_id]  # KeyError => registry drift (test-guarded)
        metrics.append({"id": field_id, "label": label, "unit": unit})
    return metrics


def build_capabilities() -> dict[str, Any]:
    """Return the machine-readable capabilities contract for E-2 consumers."""

    return {
        "contract_version": CONTRACT_VERSION,
        "api_version": config.app_version,
        "generated_from": "response-model introspection + curated label registry",
        "views": {
            "enum": list(VIEWS),
            "legacy_aliases": LEGACY_VIEW_ALIASES,
        },
        "metric_groups": [
            {"group": group, "metrics": _metrics_for(models)}
            for group, models in _GROUP_MODELS.items()
        ],
        "period": {
            "anchors": PERIOD_ANCHORS,
            "series_shapes": SERIES_SHAPES,
        },
        "identifiers": {
            "market_id": {
                "status": "retained",
                "deprecated": False,
                "note": "기존 공개 계약 유지(PL 결정). 요청/응답에 존치.",
            },
        },
        "deprecated": [
            {
                "field": "view_kind",
                "location": "dynamic-market.filters.view_kind",
                "replacement": "view",
                "note": "레거시 전략뷰 힌트. 신규 호출은 top-level view(strategic_ml/strategic_cd)를 사용.",
            },
        ],
        "provenance": {
            "source_epoch": {
                "status": "supported",
                "surface": "dynamic-market 캐시 키/리플레이(response_cache.source_epoch)",
            },
            "built_at": {
                "status": "planned",
                "surface": "미구현 — 계약 ⑤ 예고 필드.",
            },
        },
    }
