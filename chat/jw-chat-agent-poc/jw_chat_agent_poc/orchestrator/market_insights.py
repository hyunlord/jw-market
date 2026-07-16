from __future__ import annotations

import math
import re
from typing import Any

from jw_chat_agent_poc.orchestrator.markdown_formatting import eok_value, pct_value


_FORBIDDEN_CLAIMS = (
    "때문에",
    "로 인해",
    "영향으로",
    "경쟁 심화",
    "진입으로",
    "예상됩니다",
    "전망입니다",
    "될 것",
    "보입니다",
    "우려",
    "양호",
    "위협",
    "긍정적",
    "부정적",
    "견조",
    "필요합니다",
    "권합니다",
    "검토",
    "대응",
)
_FORBIDDEN_RE = re.compile("|".join(re.escape(term) for term in _FORBIDDEN_CLAIMS))


def render_market_insights(calls: list[dict[str, Any]]) -> tuple[str, ...]:
    data = _first_insight_data(calls)
    if data is None:
        return ()
    brand = str(data.get("brand") or "해당 브랜드")
    lines = [
        *_movement_insights(brand, data),
        _competitor_line(data),
        _concentration_line(data),
    ]
    return tuple(line for line in lines if line)


def render_market_narrative(calls: list[dict[str, Any]]) -> str:
    """Explain verified market movement without adding causes or forecasts."""

    data = _first_insight_data(calls)
    if data is None:
        return ""
    brand = str(data.get("brand") or "해당 브랜드")
    summary = _position_summary(brand, data)
    details = _movement_insights(brand, data)
    if not summary or not details:
        return ""
    return f"{summary}\n\n수치로 보면, {' '.join(details)}"


def _movement_insights(brand: str, data: dict[str, Any]) -> tuple[str, ...]:
    lines = (
        _direction_line(brand, data),
        _growth_line(data),
        _rank_line(data),
        _extreme_line(data),
        _turning_line(data),
        _trend_line(data),
        _missing_line(data),
    )
    return tuple(line for line in lines if line)


def forbidden_claims(markdown: str) -> tuple[str, ...]:
    found = set(_FORBIDDEN_RE.findall(markdown))
    return tuple(term for term in _FORBIDDEN_CLAIMS if term in found)


def _position_summary(brand: str, data: dict[str, Any]) -> str:
    sales_delta = _number(data.get("sales_delta_krw"))
    share_delta = _number(data.get("share_delta_pctp"))
    if sales_delta is None or share_delta is None or sales_delta == 0 or share_delta == 0:
        return ""
    if sales_delta > 0 and share_delta < 0:
        direction = f"{brand}는 매출이 늘었지만 점유율은 낮아져, 외형 성장과 시장 내 상대적 위치가 엇갈렸습니다."
    elif sales_delta > 0 and share_delta > 0:
        direction = f"{brand}는 매출과 점유율이 함께 올라, 외형과 시장 내 상대적 위치가 같이 높아졌습니다."
    elif sales_delta < 0 and share_delta < 0:
        direction = f"{brand}는 매출과 점유율이 함께 낮아져, 외형과 시장 내 상대적 위치가 같이 약해졌습니다."
    else:
        direction = f"{brand}는 매출은 줄었지만 점유율은 올라, 외형과 시장 내 상대적 위치가 서로 다르게 움직였습니다."

    excess = _number(data.get("excess_growth_pctp"))
    if excess is None:
        return direction
    if excess < 0:
        growth = "브랜드 성장률이 시장 성장률보다 낮아 시장 성장 속도에는 못 미쳤습니다."
    elif excess > 0:
        growth = "브랜드 성장률이 시장 성장률보다 높아 시장보다 빠르게 성장했습니다."
    else:
        growth = "브랜드와 시장의 성장률은 같은 수준이었습니다."
    return f"{direction} {growth}"


def _first_insight_data(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    for call in calls:
        render_data = call.get("render_data")
        if not isinstance(render_data, dict):
            continue
        insight = render_data.get("series_insight")
        if isinstance(insight, dict):
            return {"brand": render_data.get("brand"), **insight}
    return None


def _direction_line(brand: str, data: dict[str, Any]) -> str:
    share_start = _number(data.get("share_start_pct"))
    share_end = _number(data.get("share_end_pct"))
    share_delta = _number(data.get("share_delta_pctp"))
    sales_start = _number(data.get("sales_start_krw"))
    sales_end = _number(data.get("sales_end_krw"))
    sales_delta = _number(data.get("sales_delta_krw"))
    if None in (share_start, share_end, share_delta, sales_start, sales_end, sales_delta):
        return ""
    share_word = _direction_word(share_delta)
    sales_word = _direction_word(sales_delta)
    if not share_word or not sales_word:
        return ""
    connector = "했으나," if share_delta * sales_delta < 0 else "했고,"
    return (
        f"{brand} 점유율은 {pct_value(share_start)}에서 {pct_value(share_end)}로 "
        f"{abs(share_delta):.2f}%p {share_word}{connector} 처방조제액은 "
        f"{eok_value(None, sales_start)}에서 {eok_value(None, sales_end)}으로 "
        f"{eok_value(None, abs(sales_delta))} {sales_word}했습니다."
    )


def _growth_line(data: dict[str, Any]) -> str:
    brand_growth = _number(data.get("brand_growth_pct"))
    market_growth = _number(data.get("market_growth_pct"))
    excess = _number(data.get("excess_growth_pctp"))
    if None in (brand_growth, market_growth, excess):
        return ""
    return (
        f"브랜드 성장률 {brand_growth:.2f}% · 시장 성장률 {market_growth:.2f}% · "
        f"초과성장 {excess:.2f}%p입니다."
    )


def _rank_line(data: dict[str, Any]) -> str:
    start = data.get("rank_start")
    end = data.get("rank_end")
    if not isinstance(start, int) or not isinstance(end, int) or start == end:
        return ""
    return f"순위는 {start}위에서 {end}위로 변했습니다."


def _extreme_line(data: dict[str, Any]) -> str:
    maximum = _number(data.get("share_max_pct"))
    minimum = _number(data.get("share_min_pct"))
    max_period = str(data.get("share_max_period") or "")
    min_period = str(data.get("share_min_period") or "")
    if None in (maximum, minimum) or not max_period or not min_period:
        return ""
    return f"최고 {maximum:.2f}%({max_period}) · 최저 {minimum:.2f}%({min_period})입니다."


def _turning_line(data: dict[str, Any]) -> str:
    period = str(data.get("turning_point") or "")
    kind = data.get("turning_kind")
    if not period or kind not in {"low", "high"}:
        return ""
    label = "저점 후 반등" if kind == "low" else "정점 후 하락"
    return f"{period} {label}이 확인됩니다."


def _trend_line(data: dict[str, Any]) -> str:
    direction = data.get("trend_direction")
    months = data.get("trend_months")
    if direction not in {"up", "down"} or not isinstance(months, int) or months < 2:
        return ""
    return f"최근 {months}개월 연속 {'상승' if direction == 'up' else '하락'}했습니다."


def _competitor_line(data: dict[str, Any]) -> str:
    competitors = data.get("competitors")
    if not isinstance(competitors, list | tuple) or not competitors:
        return ""
    competitor = competitors[0]
    if not isinstance(competitor, dict):
        return ""
    start = _number(competitor.get("share_start_pct"))
    end = _number(competitor.get("share_end_pct"))
    sales = _number(competitor.get("sales_end_krw"))
    rank = competitor.get("rank_end")
    if None in (start, end, sales) or not isinstance(rank, int):
        return ""
    return (
        f"같은 기간 {competitor.get('brand')}의 점유율은 {start:.2f}%에서 {end:.2f}%, "
        f"처방조제액 {eok_value(None, sales)}, {rank}위입니다."
    )


def _concentration_line(data: dict[str, Any]) -> str:
    hhi = _number(data.get("hhi_end"))
    cr5 = _number(data.get("cr5_end_pct"))
    denominator = data.get("denominator_end")
    if None in (hhi, cr5) or not isinstance(denominator, int) or denominator <= 0:
        return ""
    top_count = min(5, denominator)
    return f"상위 {top_count}개 합계 {cr5:.2f}% · HHI {hhi:.2f} · 분모 {denominator}개입니다."


def _missing_line(data: dict[str, Any]) -> str:
    missing = data.get("missing_periods")
    if not isinstance(missing, list | tuple) or not missing:
        return ""
    return " · ".join(str(period) for period in missing) + "은 데이터 미보유입니다."


def _direction_word(value: float) -> str:
    return "증가" if value > 0 else "감소" if value < 0 else ""


def _number(value: Any) -> float | None:
    if not isinstance(value, int | float):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None
