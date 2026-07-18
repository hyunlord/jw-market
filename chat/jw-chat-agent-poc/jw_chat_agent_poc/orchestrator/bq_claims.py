from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final, Sequence

_NUMBER_RE: Final = re.compile(r"(?<![A-Za-z가-힣])[+-]?\d+(?:\.\d+)?%?(?![A-Za-z가-힣])")
_DATE_RE: Final = re.compile(r"\d{4}-\d{2}(?:~\d{4}-\d{2})?")

_KIND_TO_REF_KIND: Final[dict[str, frozenset[str]]] = {
    "number": frozenset({"number"}),
    "temporal_overlap": frozenset({"time_window"}),
    "forecast": frozenset({"forecast"}),
    "numeric_so_what": frozenset({"number"}),
    "news": frozenset({"news"}),
    "event": frozenset({"event"}),
}


class BQClaimError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    source: str
    kind: str
    identity: str
    period: str | None = None


@dataclass(frozen=True, slots=True)
class GroundedClaim:
    kind: str
    text: str
    evidence_refs: tuple[EvidenceRef, ...]
    identity: str = ""
    condition: str = ""
    uncertainty: str = ""
    numbers: tuple[str, ...] = ()


def number_claim(value: float, refs: Sequence[EvidenceRef]) -> GroundedClaim:
    refs_tuple = _validated_refs("number", refs)
    text = f"{value:.2f}%"
    return GroundedClaim("number", text, refs_tuple, identity=refs_tuple[0].identity, numbers=(text,))


def temporal_overlap_claim(claim_period: tuple[str, str], refs: Sequence[EvidenceRef]) -> GroundedClaim:
    refs_tuple = _validated_refs("temporal_overlap", refs)
    start, end = claim_period
    if start > end:
        raise BQClaimError("temporal overlap window is inverted")
    if len({ref.period for ref in refs_tuple}) < 2:
        raise BQClaimError("temporal overlap needs distinct evidence periods")
    for ref in refs_tuple:
        if ref.period is None or not (start <= ref.period <= end):
            raise BQClaimError("temporal overlap falls outside evidence window")
    return GroundedClaim("temporal_overlap", f"{start}~{end}", refs_tuple, numbers=(start, end))


def conditional_forecast_claim(
    value: float,
    condition: str,
    uncertainty: str,
    refs: Sequence[EvidenceRef],
) -> GroundedClaim:
    if not condition:
        raise BQClaimError("conditional forecast needs a condition")
    if not uncertainty:
        raise BQClaimError("conditional forecast needs uncertainty")
    refs_tuple = _validated_refs("forecast", refs)
    value_text = f"{value:g}"
    text = f"{value_text} {condition} {uncertainty}"
    return GroundedClaim("forecast", text, refs_tuple, condition=condition, uncertainty=uncertainty, numbers=(value_text,))


def numeric_so_what_claim(current: float | None, baseline: float | None, refs: Sequence[EvidenceRef]) -> GroundedClaim:
    if current is None or baseline is None:
        raise BQClaimError("missing numeric input cannot be coerced")
    refs_tuple = _validated_refs("numeric_so_what", refs)
    delta = current - baseline
    current_text = f"{current:.2f}"
    baseline_text = f"{baseline:.2f}"
    delta_text = f"{delta:+.2f}"
    text = f"{current_text} - {baseline_text} = {delta_text}"
    return GroundedClaim(
        "numeric_so_what",
        text,
        refs_tuple,
        numbers=(current_text, baseline_text, delta_text),
    )


def news_identity_claim(identity: str, refs: Sequence[EvidenceRef]) -> GroundedClaim:
    refs_tuple = _validated_refs("news", refs)
    _ensure_identity(identity, refs_tuple)
    return GroundedClaim("news", identity, refs_tuple, identity=identity)


def event_identity_claim(identity: str, refs: Sequence[EvidenceRef]) -> GroundedClaim:
    refs_tuple = _validated_refs("event", refs)
    _ensure_identity(identity, refs_tuple)
    return GroundedClaim("event", identity, refs_tuple, identity=identity)


def verify_claim_surface(claim: GroundedClaim, surface: str) -> None:
    if claim.identity and claim.identity not in surface:
        raise BQClaimError("unsupported substitution or identity drop")
    if claim.condition and claim.condition not in surface:
        raise BQClaimError("condition dropped")
    if claim.uncertainty and claim.uncertainty not in surface:
        raise BQClaimError("uncertainty dropped")
    allowed = set(claim.numbers) | _surface_numbers(claim.uncertainty)
    stripped = _DATE_RE.sub("DATE", surface)
    for token in _NUMBER_RE.findall(stripped):
        if token not in allowed:
            raise BQClaimError(f"unknown number: {token}")


def _validated_refs(kind: str, refs: Sequence[EvidenceRef]) -> tuple[EvidenceRef, ...]:
    refs_tuple = tuple(refs)
    if not refs_tuple:
        raise BQClaimError("evidence refs required")
    sources = {ref.source for ref in refs_tuple}
    if len(sources) != 1:
        raise BQClaimError("source aggregation is unsupported")
    expected = _KIND_TO_REF_KIND[kind]
    for ref in refs_tuple:
        if ref.kind not in expected:
            raise BQClaimError("unsupported substitution")
    return refs_tuple


def _ensure_identity(identity: str, refs: tuple[EvidenceRef, ...]) -> None:
    if not identity:
        raise BQClaimError("identity required")
    for ref in refs:
        if ref.identity != identity:
            raise BQClaimError("unknown news/event identity")


def _surface_numbers(text: str) -> set[str]:
    if not text:
        return set()
    return set(_NUMBER_RE.findall(_DATE_RE.sub("DATE", text)))
