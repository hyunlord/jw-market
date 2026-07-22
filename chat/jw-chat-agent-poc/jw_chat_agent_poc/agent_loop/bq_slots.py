from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class BqSlots:
    brand: str
    period: str
    question: str
    metrics: tuple[str, ...]
    modifiers: tuple[str, ...]
    axes: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BqSlotSignature:
    contract_id: str
    metrics: frozenset[str] = frozenset()
    modifiers: frozenset[str] = frozenset()
    axes: frozenset[str] = frozenset()
    sources: frozenset[str] = frozenset()
    any_axis: frozenset[str] = frozenset()


_PRESCRIPTION_METRIC_PATTERNS: Final = (
    ("prescription_dispensing_amount", re.compile(r"처방\s*조제액")),
    ("prescription_count", re.compile(r"처방\s*건수")),
    ("prescription_volume", re.compile(r"처방\s*량")),
    ("prescription", re.compile(r"처방|조제|prescription", re.IGNORECASE)),
)
_EXPLICIT_SALES_RE: Final = re.compile(r"매출|(?<![A-Za-z])sales(?![A-Za-z])", re.IGNORECASE)
_PRESCRIPTION_RE: Final = _PRESCRIPTION_METRIC_PATTERNS[-1][1]

_METRIC_PATTERNS: Final = (
    ("market", re.compile(r"시장")),
    ("market_size", re.compile(r"시장\s*규모")),
    ("patient_count", re.compile(r"환자\s*수")),
    ("sales", _EXPLICIT_SALES_RE),
    ("prescription", _PRESCRIPTION_RE),
    ("activity", re.compile(r"영업\s*활동")),
    ("competition", re.compile(r"경쟁\s*(?:구도|상대|사)")),
    ("threat", re.compile(r"신규\s*진입|위협\s*브랜드")),
    ("news", re.compile(r"이슈|뉴스")),
)
_MODIFIER_PATTERNS: Final = (
    ("trend", re.compile(r"추이|최근|변(?:해|화|하고|했|동)?|어때")),
    ("forecast", re.compile(r"앞으로|전망|예측|어떻게\s*될")),
    ("impact", re.compile(r"영향")),
    ("position", re.compile(r"우리\s*위치")),
    ("cause", re.compile(r"왜")),
)
_AXIS_PATTERNS: Final = (
    ("channel", re.compile(r"채널")),
    ("specialty", re.compile(r"진료과")),
)
_SOURCE_PATTERNS: Final = (
    ("ubist", re.compile(r"UBIST", re.IGNORECASE)),
    ("iqvia_nsa", re.compile(r"IQVIA|NSA", re.IGNORECASE)),
)

_SIGNATURES: Final = (
    BqSlotSignature("C3", sources=frozenset({"ubist", "iqvia_nsa"})),
    BqSlotSignature(
        "D2",
        metrics=frozenset({"activity", "sales"}),
        modifiers=frozenset({"impact"}),
    ),
    BqSlotSignature("D3", metrics=frozenset({"activity", "competition"})),
    BqSlotSignature("A3", metrics=frozenset({"patient_count", "sales"})),
    BqSlotSignature(
        "A2",
        metrics=frozenset({"market"}),
        modifiers=frozenset({"forecast"}),
    ),
    BqSlotSignature(
        "A1",
        metrics=frozenset({"market_size"}),
        modifiers=frozenset({"trend"}),
    ),
    BqSlotSignature(
        "B1",
        metrics=frozenset({"competition"}),
        modifiers=frozenset({"trend"}),
    ),
    BqSlotSignature(
        "B2",
        metrics=frozenset({"competition"}),
        modifiers=frozenset({"position"}),
    ),
    BqSlotSignature("B3", metrics=frozenset({"threat"})),
    BqSlotSignature("C2", any_axis=frozenset({"channel", "specialty"})),
    BqSlotSignature(
        "C1",
        metrics=frozenset({"sales"}),
        modifiers=frozenset({"trend"}),
    ),
    BqSlotSignature(
        "D1",
        metrics=frozenset({"activity"}),
        modifiers=frozenset({"trend"}),
    ),
    BqSlotSignature("E1", metrics=frozenset({"news"})),
    BqSlotSignature("E2", modifiers=frozenset({"cause"})),
)


def extract_bq_slots(question: str, *, brand: str, period: str) -> BqSlots:
    return BqSlots(
        brand=brand,
        period=period,
        question=question,
        metrics=_matches(question, _METRIC_PATTERNS),
        modifiers=_matches(question, _MODIFIER_PATTERNS),
        axes=_matches(question, _AXIS_PATTERNS),
        sources=_matches(question, _SOURCE_PATTERNS),
    )


def requested_prescription_metric(question: str) -> str | None:
    return next(
        (name for name, pattern in _PRESCRIPTION_METRIC_PATTERNS if pattern.search(question)),
        None,
    )


def prescription_metric_requires_typed_stop(question: str) -> bool:
    return requested_prescription_metric(question) is not None


def contract_id_for_slots(slots: BqSlots) -> str | None:
    return next(
        (
            signature.contract_id
            for signature in _SIGNATURES
            if _matches_signature(signature, slots)
        ),
        None,
    )


def _matches(
    question: str,
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
) -> tuple[str, ...]:
    return tuple(name for name, pattern in patterns if pattern.search(question))


def _matches_signature(signature: BqSlotSignature, slots: BqSlots) -> bool:
    metrics = frozenset(slots.metrics)
    modifiers = frozenset(slots.modifiers)
    axes = frozenset(slots.axes)
    sources = frozenset(slots.sources)
    any_axis_matches = not signature.any_axis or bool(signature.any_axis & axes)
    return (
        signature.metrics <= metrics
        and signature.modifiers <= modifiers
        and signature.axes <= axes
        and signature.sources <= sources
        and any_axis_matches
    )
