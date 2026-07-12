from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
import math
import re
from typing import Any

from .display_format import display_number, display_pct
from .source_loader import Agent3Source
from .strength_candidate_extractor import MarketMetricRow


FORBIDDEN_MARKET_WORDS = ("선도", "안정적", "성장", "우수", "강력")


@dataclass(frozen=True, slots=True)
class MarketPositionResult:
    candidate: dict[str, Any]
    summary: dict[str, Any]
    narrative: str


def build_market_position_fallback(
    *,
    brand_key: str,
    brand_name: str,
    source: Agent3Source,
    profile: dict[str, Any],
    base_summary: dict[str, Any],
    market_rows: list[MarketMetricRow],
) -> MarketPositionResult:
    """Build the recovered deterministic fallback for one brand/source unit."""

    codes = _scope_codes(profile, source)
    source_db = "ubist" if source == "ubist" else "iqvia_nsa"
    histories: dict[str, dict[str, float]] = defaultdict(dict)
    desc_by_code: dict[str, str] = {}
    for row in sorted(market_rows, key=lambda item: (item.brand_key, item.atc4_code)):
        if row.source.lower() != source_db:
            continue
        code = canonical_atc4(row.atc4_code, source)
        if code not in codes:
            continue
        desc_by_code.setdefault(code, str(row.atc4_desc or code).strip())
        aggregate = histories[row.brand_key]
        for period, value in _history(row.raw_value_history).items():
            aggregate[period] = aggregate.get(period, 0.0) + value

    if not histories:
        raise RuntimeError(f"empty market scope: {source_db}/{codes}")
    if brand_key not in histories:
        raise RuntimeError(f"target absent from market scope: {brand_key}/{source}/{codes}")

    latest_period = max(
        (period for values in histories.values() for period in values),
        key=_period_key,
    )
    latest_values = {
        key: values.get(latest_period, 0.0)
        for key, values in histories.items()
    }
    cumulative = {
        key: _stable_sum(values.values())
        for key, values in histories.items()
    }
    ranked = sorted(
        histories,
        key=lambda key: (
            0 if latest_values[key] > 0 else 1,
            -latest_values[key] if latest_values[key] > 0 else 0.0,
            -cumulative[key],
            key,
        ),
    )
    rank_by_brand = {key: index for index, key in enumerate(ranked, start=1)}
    market_size = _stable_sum(latest_values.values())
    latest_value = latest_values[brand_key]
    share_pct = latest_value / market_size * 100 if market_size > 0 else 0.0
    observation_count = _consecutive_positive_count(
        histories[brand_key],
        latest_period,
        source,
    )
    cumulative_value = cumulative[brand_key]

    source_label = "UBIST" if source == "ubist" else "IQVIA"
    sales_label = "처방액" if source == "ubist" else "매출액"
    period_unit = "개월" if source == "ubist" else "분기"
    joined_codes = "+".join(codes)
    market_name = " / ".join(desc_by_code.get(code, code) for code in codes)
    if any(word in market_name for word in FORBIDDEN_MARKET_WORDS):
        market_name = " / ".join(f"ATC4 {code}" for code in codes)

    candidate = {
        "brand": brand_name,
        "source": source,
        "measure": "sales",
        "metric": "market_position",
        "slice": f"전체 {source_label} / ATC4 {joined_codes}",
        "period": latest_period,
        "rank": rank_by_brand[brand_key],
        "share_pct": share_pct,
        "market_brand_count": len(histories),
        "latest_value": latest_value,
        "observation_count": observation_count,
        "market_key": f"{source}:{joined_codes}",
        "cumulative_value": cumulative_value,
        "evidence": f"market_scope.{source}.{joined_codes}.{latest_period}",
        "low_base": latest_value <= 0,
        "caveats": [],
    }
    if latest_value > 0:
        narrative = (
            f"{source_label} {market_name} 시장 {len(histories)}개 브랜드 중 "
            f"{rank_by_brand[brand_key]}위이며, 최신 {latest_period} {sales_label}은 "
            f"{display_number(latest_value)}(점유율 {display_pct(share_pct)})입니다."
        )
        if observation_count > 1:
            narrative += f" 최근 {observation_count}{period_unit} 연속 실적이 확인됩니다."
    else:
        narrative = (
            f"{source_label} {market_name} 시장에서 최신 {latest_period} 실적은 "
            "확인되지 않습니다."
        )
        if cumulative_value > 0:
            narrative += (
                f" 전체 관측기간 누적 {sales_label}은 "
                f"{display_number(cumulative_value)}입니다."
            )

    forbidden_hits = [word for word in FORBIDDEN_MARKET_WORDS if word in narrative]
    if forbidden_hits:
        raise RuntimeError(
            f"market_position narrative contains forbidden words: {forbidden_hits}"
        )

    summary = dict(base_summary)
    summary.update(
        {
            "brand": brand_name,
            "candidate_count": 1,
            "source": source,
            "strength_items": [
                {
                    "candidate_index": 0,
                    "confidence": "high",
                    "metric": "market_position",
                    "narrative": narrative,
                    "numbers": {
                        "rank": rank_by_brand[brand_key],
                        "share_pct": share_pct,
                        "market_brand_count": len(histories),
                        "latest_value": latest_value,
                        "observation_count": observation_count,
                        "cumulative_value": cumulative_value,
                    },
                    "period": latest_period,
                    "slice": candidate["slice"],
                }
            ],
        }
    )
    return MarketPositionResult(
        candidate=candidate,
        summary=summary,
        narrative=narrative,
    )


def canonical_atc4(value: str, source: Agent3Source) -> str:
    normalized = value.strip().upper()
    if source != "ubist":
        return normalized
    aliases = {
        "A2B2": "A02B2",
        "A6B2": "A06B2",
        "C1D": "C01D0",
        "G4C0": "G04C0",
    }
    if normalized in aliases:
        return aliases[normalized]
    if re.fullmatch(r"[A-Z]\d[A-Z]\d", normalized):
        return f"{normalized[0]}0{normalized[1:]}"
    return normalized


def _stable_sum(values: Iterable[float]) -> float:
    return math.fsum(values)


def _scope_codes(profile: dict[str, Any], source: Agent3Source) -> tuple[str, ...]:
    codes = tuple(
        sorted(
            {
                canonical_atc4(str(value), source)
                for value in profile.get("atc4_codes", [])
                if str(value).strip()
            }
        )
    )
    if not codes:
        raise RuntimeError(f"missing ATC4 scope: {source}")
    return codes


def _history(value: dict[str, float]) -> dict[str, float]:
    result: dict[str, float] = {}
    for period, raw in value.items():
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result[str(period)] = number
    return result


def _period_key(period: str) -> tuple[int, int, str]:
    month = re.fullmatch(r"(20\d{2})-(\d{2})", period)
    if month:
        return int(month.group(1)), int(month.group(2)), period
    quarter = re.fullmatch(r"(20\d{2})-Q([1-4])", period)
    if quarter:
        return int(quarter.group(1)), int(quarter.group(2)) * 3, period
    return 0, 0, period


def _period_ordinal(period: str, source: Agent3Source) -> int | None:
    if source == "ubist":
        match = re.fullmatch(r"(20\d{2})-(\d{2})", period)
        periods_per_year = 12
    else:
        match = re.fullmatch(r"(20\d{2})-Q([1-4])", period)
        periods_per_year = 4
    if not match:
        return None
    return int(match.group(1)) * periods_per_year + int(match.group(2)) - 1


def _consecutive_positive_count(
    target_history: dict[str, float],
    latest_period: str,
    source: Agent3Source,
) -> int:
    latest = _period_ordinal(latest_period, source)
    if latest is None:
        return 0
    values = {
        _period_ordinal(period, source): value
        for period, value in target_history.items()
    }
    count = 0
    ordinal = latest
    while ordinal in values and float(values[ordinal] or 0.0) > 0:
        count += 1
        ordinal -= 1
    return count
