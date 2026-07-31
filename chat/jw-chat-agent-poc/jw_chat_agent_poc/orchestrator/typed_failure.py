from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import (
    ShadowGate,
    emit_shadow_gate_observation,
)


class TypedFailureCode(StrEnum):
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    INDEX_MISS = "INDEX_MISS"
    EVIDENCE_BINDING_FAILED = "EVIDENCE_BINDING_FAILED"
    MARKET_UNRESOLVED = "market_unresolved"
    INCOMPATIBLE_COMPARISON = "incompatible_comparison"
    DISEASE_CODE_ABSENT = "DISEASE_CODE_ABSENT"

    # Reserved until an exact producer contract exists.
    NO_OFFICIAL_RECORD = "NO_OFFICIAL_RECORD"
    NO_EVIDENCE = "NO_EVIDENCE"
    PARTIAL_EVIDENCE = "PARTIAL_EVIDENCE"
    UNSUPPORTED_MULTI_ENTITY = "UNSUPPORTED_MULTI_ENTITY"
    NO_FILE_ATTACHED = "NO_FILE_ATTACHED"


@dataclass(frozen=True, slots=True)
class TypedFailureResult:
    code: TypedFailureCode
    user_message: str
    recovery_action: str | None
    source: str | None
    evidence_summary: tuple[str, ...]
    terminal: bool
    partial: bool


# Priority is semantic rather than surface-based:
# identity/scope safety -> proven absence -> capability/index ->
# evidence integrity -> partial -> infrastructure -> unknown fallback.
_CODE_PRIORITY: Final[tuple[TypedFailureCode, ...]] = (
    TypedFailureCode.IDENTITY_MISMATCH,
    TypedFailureCode.MARKET_UNRESOLVED,
    TypedFailureCode.INCOMPATIBLE_COMPARISON,
    TypedFailureCode.UNSUPPORTED_MULTI_ENTITY,
    TypedFailureCode.NO_FILE_ATTACHED,
    TypedFailureCode.DISEASE_CODE_ABSENT,
    TypedFailureCode.NO_OFFICIAL_RECORD,
    TypedFailureCode.INDEX_MISS,
    TypedFailureCode.EVIDENCE_BINDING_FAILED,
    TypedFailureCode.NO_EVIDENCE,
    TypedFailureCode.PARTIAL_EVIDENCE,
    TypedFailureCode.UPSTREAM_UNAVAILABLE,
)
_ACTIVE_CODES: Final[frozenset[TypedFailureCode]] = frozenset(
    {
        TypedFailureCode.UPSTREAM_UNAVAILABLE,
        TypedFailureCode.IDENTITY_MISMATCH,
        TypedFailureCode.INDEX_MISS,
        TypedFailureCode.EVIDENCE_BINDING_FAILED,
        TypedFailureCode.MARKET_UNRESOLVED,
        TypedFailureCode.INCOMPATIBLE_COMPARISON,
        TypedFailureCode.DISEASE_CODE_ABSENT,
    }
)
_FAILURE_TRAITS: Final[Mapping[TypedFailureCode, tuple[bool, bool]]] = {
    TypedFailureCode.UPSTREAM_UNAVAILABLE: (True, False),
    TypedFailureCode.IDENTITY_MISMATCH: (True, False),
    TypedFailureCode.INDEX_MISS: (True, False),
    TypedFailureCode.EVIDENCE_BINDING_FAILED: (True, False),
    TypedFailureCode.MARKET_UNRESOLVED: (True, False),
    TypedFailureCode.INCOMPATIBLE_COMPARISON: (True, True),
    TypedFailureCode.DISEASE_CODE_ABSENT: (True, False),
}
_CODE_KEYS: Final[tuple[str, ...]] = (
    "error_code",
    "reason_code",
    "status",
    "fallback_code",
)
_MAPPING_SURFACES: Final[tuple[str, ...]] = (
    "render_data",
    "router_diagnostics",
    "routing_v4",
    "official_web_fallback",
    "executed_call_signature",
)
_SEQUENCE_SURFACES: Final[tuple[str, ...]] = ("tool_calls", "sources")
_MESSAGE_KEYS: Final[tuple[str, ...]] = (
    "user_message",
    "answer",
    "preview",
    "message",
)


def normalize_typed_failure(
    result: Mapping[str, Any],
) -> TypedFailureResult | None:
    """Normalize known legacy failure surfaces without mutating or rendering."""

    candidates: dict[TypedFailureCode, Mapping[str, Any]] = {}
    for surface in _known_surfaces(result):
        for key in _CODE_KEYS:
            code = _active_code(surface.get(key))
            if code is not None:
                candidates.setdefault(code, surface)

    selected = next((code for code in _CODE_PRIORITY if code in candidates), None)
    if selected is None:
        return None

    selected_surface = candidates[selected]
    terminal, partial = _FAILURE_TRAITS[selected]
    return TypedFailureResult(
        code=selected,
        user_message=(
            _first_text(selected_surface, _MESSAGE_KEYS)
            or _first_text(result, _MESSAGE_KEYS)
        ),
        recovery_action=(
            _optional_text(selected_surface, "recovery_action")
            or _optional_text(result, "recovery_action")
        ),
        source=(
            _optional_text(selected_surface, "source")
            or _optional_text(result, "source")
            or _single_source(result)
        ),
        evidence_summary=(
            _string_tuple(selected_surface.get("evidence_summary"))
            or _string_tuple(result.get("evidence_summary"))
        ),
        terminal=terminal,
        partial=partial,
    )


def observe_typed_failure(
    result: Mapping[str, Any],
    *,
    legacy_answer: str = "",
    question_fingerprint: str = "",
) -> TypedFailureResult | None:
    normalized = normalize_typed_failure(result)
    shadow_answer = (
        render_typed_failure_shadow(normalized)
        if normalized is not None
        else None
    )
    matches = shadow_answer is not None and shadow_answer == legacy_answer
    emit_shadow_gate_observation(
        gate=ShadowGate.TYPED_FAILURE_MODEL,
        phase="surface",
        status=(
            "NOT_APPLICABLE"
            if normalized is None
            else "MATCH" if matches else "DIFF"
        ),
        reason=normalized.code.value if normalized is not None else "no_active_code",
        required_count=1 if normalized is not None else 0,
        observed_count=1 if matches else 0,
        missing_count=1 if normalized is not None and not matches else 0,
        terminal=normalized.terminal if normalized is not None else None,
        partial=normalized.partial if normalized is not None else None,
        question_fingerprint=question_fingerprint,
    )
    return normalized


def render_typed_failure_shadow(result: TypedFailureResult) -> str:
    if result.recovery_action is None or result.recovery_action in result.user_message:
        return result.user_message
    if not result.user_message:
        return result.recovery_action
    return f"{result.user_message}\n\n{result.recovery_action}"


def _active_code(value: Any) -> TypedFailureCode | None:
    if not isinstance(value, str):
        return None
    try:
        code = TypedFailureCode(value.strip())
    except ValueError:
        return None
    return code if code in _ACTIVE_CODES else None


def _known_surfaces(
    result: Mapping[str, Any],
) -> Iterator[Mapping[str, Any]]:
    seen: set[int] = set()
    yield from _walk_surfaces(result, seen=seen, depth=0)


def _walk_surfaces(
    surface: Mapping[str, Any],
    *,
    seen: set[int],
    depth: int,
) -> Iterator[Mapping[str, Any]]:
    if depth > 8 or id(surface) in seen:
        return
    seen.add(id(surface))
    yield surface

    for key in _MAPPING_SURFACES:
        value = surface.get(key)
        if isinstance(value, Mapping):
            yield from _walk_surfaces(value, seen=seen, depth=depth + 1)

    for key in _SEQUENCE_SURFACES:
        value = surface.get(key)
        if not isinstance(value, Sequence) or isinstance(value, str | bytes):
            continue
        for item in value:
            if isinstance(item, Mapping):
                yield from _walk_surfaces(item, seen=seen, depth=depth + 1)


def _first_text(
    surface: Mapping[str, Any],
    keys: tuple[str, ...],
) -> str:
    return next(
        (
            text
            for key in keys
            if (text := _optional_text(surface, key)) is not None
        ),
        "",
    )


def _optional_text(surface: Mapping[str, Any], key: str) -> str | None:
    value = surface.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _single_source(result: Mapping[str, Any]) -> str | None:
    sources = _string_tuple(result.get("sources"))
    return sources[0] if len(sources) == 1 else None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(
        stripped
        for item in value
        if isinstance(item, str) and (stripped := item.strip())
    )
