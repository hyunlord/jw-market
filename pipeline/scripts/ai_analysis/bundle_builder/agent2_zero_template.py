from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, assert_never
class ZeroNarrativeType(StrEnum):
    GROWTH = "growth"
    DECLINE = "decline"
    STABLE = "stable"
    LEADER = "leader"
    NICHE = "niche"
    NEWCOMER = "newcomer"
    INSUFFICIENT = "insufficient"

@dataclass(frozen=True, slots=True)
class KpiSnapshot:
    brand: str
    market_name: str | None = None
    rank: int | None = None
    share_pct: float | None = None
    cagr_pct: float | None = None
    ei: float | None = None
    momentum: float | None = None
    hhi: float | None = None
    market_size_recent: float | None = None
    first_positive_period: str | None = None
    is_new: bool = False
def classify_zero_type(snapshot: KpiSnapshot) -> ZeroNarrativeType:
    """Classify a no-evidence brand from KPI shape only."""
    if not _has_any_story_value(snapshot):
        return ZeroNarrativeType.INSUFFICIENT
    if snapshot.is_new or snapshot.first_positive_period:
        return ZeroNarrativeType.NEWCOMER
    if snapshot.rank is not None and snapshot.rank <= 3:
        return ZeroNarrativeType.LEADER
    if _has_growth_signal(snapshot):
        return ZeroNarrativeType.GROWTH
    if _has_decline_signal(snapshot):
        return ZeroNarrativeType.DECLINE
    if _is_stable(snapshot):
        return ZeroNarrativeType.STABLE
    if snapshot.share_pct is not None and snapshot.share_pct < 1.0:
        return ZeroNarrativeType.NICHE
    if snapshot.rank is not None and snapshot.rank >= 20:
        return ZeroNarrativeType.NICHE
    return ZeroNarrativeType.STABLE
def render_zero_template(snapshot: KpiSnapshot) -> dict[str, dict[str, Any]]:
    """Render a cache-compatible, LLM-free card for evidence-zero brands."""
    kind = classify_zero_type(snapshot)
    return {stage: _template_stage(snapshot, kind) for stage in ("phenomenon", "cause", "prediction", "recommendation")}
def _has_any_story_value(snapshot: KpiSnapshot) -> bool:
    return any(
        value is not None
        for value in (
            snapshot.rank,
            snapshot.share_pct,
            snapshot.cagr_pct,
            snapshot.ei,
            snapshot.momentum,
            snapshot.hhi,
            snapshot.market_size_recent,
            snapshot.first_positive_period,
        )
    )
def _has_growth_signal(snapshot: KpiSnapshot) -> bool:
    return (
        snapshot.cagr_pct is not None
        and snapshot.ei is not None
        and snapshot.momentum is not None
        and snapshot.cagr_pct > 0
        and snapshot.ei > 100
        and snapshot.momentum > 0
    )
def _has_decline_signal(snapshot: KpiSnapshot) -> bool:
    return (
        snapshot.cagr_pct is not None
        and snapshot.momentum is not None
        and snapshot.cagr_pct < 0
        and snapshot.momentum < 0
    )
def _is_stable(snapshot: KpiSnapshot) -> bool:
    return (
        snapshot.cagr_pct is not None
        and snapshot.momentum is not None
        and abs(snapshot.cagr_pct) <= 1.0
        and abs(snapshot.momentum) <= 1.0
    )
def _template_stage(snapshot: KpiSnapshot, kind: ZeroNarrativeType) -> dict[str, Any]:
    return {
        "title": f"{snapshot.brand} 통계 기반 요약 (관련 뉴스 없음)",
        "body": _body(snapshot, kind),
        "bullets": _bullets(snapshot, kind),
        "is_template": True,
        "evidence_none": True,
        "template_type": kind.value,
    }
def _body(snapshot: KpiSnapshot, kind: ZeroNarrativeType) -> str:
    match kind:
        case ZeroNarrativeType.GROWTH:
            return _growth_body(snapshot)
        case ZeroNarrativeType.DECLINE:
            return _decline_body(snapshot)
        case ZeroNarrativeType.STABLE:
            return _stable_body(snapshot)
        case ZeroNarrativeType.LEADER:
            return _leader_body(snapshot)
        case ZeroNarrativeType.NICHE:
            return _niche_body(snapshot)
        case ZeroNarrativeType.NEWCOMER:
            return _newcomer_body(snapshot)
        case ZeroNarrativeType.INSUFFICIENT:
            return (
                f"{snapshot.brand}은 현재 score≥50 근거 뉴스가 없고, mart KPI도 충분하지 않아 "
                "정량 신호를 단정하지 않습니다. 관련 뉴스 없음 상태로 표시합니다."
            )
        case unreachable:
            assert_never(unreachable)
def _growth_body(snapshot: KpiSnapshot) -> str:
    position = _position(snapshot) or "점유율 확인 구간"
    signals = _signals(
        ("CAGR", _pct(snapshot.cagr_pct)),
        ("EI", _number(snapshot.ei)),
        ("모멘텀", _number(snapshot.momentum)),
    )
    return _join(
        [
            f"{_topic(snapshot.brand)} {_market_loc(snapshot)}",
            f"{position}{_as_particle(position)} 관측됩니다.",
            f"{signals} 신호가 동시에 우상향해 시장 평균보다 빠른 확장 흐름이 강합니다.",
            "관련 뉴스는 없지만, 통계만 보면 단순 유지보다 성장 서사가 더 뚜렷한 브랜드입니다.",
        ]
    )
def _decline_body(snapshot: KpiSnapshot) -> str:
    signals = _signals(("CAGR", _pct(snapshot.cagr_pct)), ("모멘텀", _number(snapshot.momentum)))
    return _join(
        [
            f"{_topic(snapshot.brand)} {_market_loc(snapshot)}",
            f"{signals} 신호가 함께 약해져 하락 압력이 관측됩니다.",
            "관련 뉴스 없음 구간이므로 원인은 단정하지 않고, 경쟁 심화 가능성은 정량 신호 수준으로만 해석합니다.",
        ]
    )
def _stable_body(snapshot: KpiSnapshot) -> str:
    return _join(
        [
            f"{_topic(snapshot.brand)} {_market_loc(snapshot)}",
            _position(snapshot),
            "수준을 큰 변동 없이 유지하는 정체형 통계 카드입니다.",
        ]
    )
def _leader_body(snapshot: KpiSnapshot) -> str:
    position = _position(snapshot) or "상위권"
    hhi = _number(snapshot.hhi)
    return _join(
        [
            f"{_topic(snapshot.brand)} {_market_loc(snapshot)}",
            f"{position}로 선두권 포지션을 차지합니다.",
            f"HHI {hhi}" if hhi else "",
            "구조상 관련 뉴스가 없어도 시장 내 존재감은 통계로 확인됩니다.",
        ]
    )
def _niche_body(snapshot: KpiSnapshot) -> str:
    position = _position(snapshot) or "하위권, 낮은 점유율"
    return _join(
        [
            f"{_topic(snapshot.brand)} {_market_loc(snapshot)}",
            f"{position}로 관측되는 니치형 브랜드입니다.",
            "관련 뉴스 없음 상태에서는 확대 해석보다 포지션 확인에 초점을 둡니다.",
        ]
    )
def _newcomer_body(snapshot: KpiSnapshot) -> str:
    first = f"{snapshot.first_positive_period}부터 매출이 확인되는" if snapshot.first_positive_period else "최근 매출이 확인되는"
    return _join(
        [
            f"{_topic(snapshot.brand)} {_market_loc(snapshot)}",
            first,
            "신규형 브랜드입니다.",
            "뉴스 근거는 아직 없지만, 초기 매출 발생 여부를 기준으로 추적 대상에 올립니다.",
        ]
    )
def _bullets(snapshot: KpiSnapshot, kind: ZeroNarrativeType) -> list[str]:
    bullets = ["관련 뉴스 없음: score≥50 evidence 0건", f"분류: {kind.value}"]
    for label, value in (
        ("시장 순위", _rank(snapshot.rank)),
        ("최근 점유율", _pct(snapshot.share_pct)),
        ("CAGR", _pct(snapshot.cagr_pct)),
    ):
        if value:
            bullets.append(f"{label} {value}")
    return bullets
def _position(snapshot: KpiSnapshot) -> str:
    share = _pct(snapshot.share_pct)
    return ", ".join(part for part in (_rank(snapshot.rank), f"점유율 {share}" if share else None) if part)
def _signals(*items: tuple[str, str | None]) -> str:
    return ", ".join(f"{label} {value}" for label, value in items if value)
def _market(snapshot: KpiSnapshot) -> str:
    return snapshot.market_name or "해당 시장"
def _market_loc(snapshot: KpiSnapshot) -> str:
    return f"{_market(snapshot)}에서"
def _has_final_consonant(text: str) -> bool:
    for char in reversed(text.strip()):
        code = ord(char)
        if 0xAC00 <= code <= 0xD7A3:
            return (code - 0xAC00) % 28 != 0
        if char.isalnum():
            return False
    return False
def _topic(text: str) -> str:
    return f"{text}{'은' if _has_final_consonant(text) else '는'}"
def _as_particle(text: str) -> str:
    return "으로" if _has_final_consonant(text) else "로"
def _pct(value: float | None) -> str | None:
    number = _small_number(value)
    return f"{number}%" if number is not None else None
def _number(value: float | None) -> str | None:
    return _small_number(value)
def _small_number(value: float | None) -> str | None:
    if value is None:
        return None
    if abs(value) < 0.05:
        if value == 0:
            value = 0.0
        elif abs(value) < 0.001:
            return f"{'-' if value < 0 else ''}<0.001"
        else:
            return f"{value:.3f}"
    return f"{value:,.1f}"
def _rank(value: int | None) -> str | None:
    return f"{value}위" if value is not None else None
def _join(parts: list[str | None]) -> str:
    return " ".join(part for part in parts if part)
