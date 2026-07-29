from __future__ import annotations

import re
import unicodedata


_INPUT_MODE = "shadow"
_INSTRUCTION_OVERRIDE_PATTERNS = (
    re.compile(
        r"(?:이전|앞선|기존|지금까지(?:의)?|위(?:의)?|모든)\s*"
        r"(?:시스템\s*)?(?:지시(?:사항)?|명령|규칙|프롬프트)"
        r"(?:을|를)?\s*(?:전부\s*)?"
        r"(?:무시(?:하|해)|따르지\s*말|잊어|폐기)"
    ),
    re.compile(
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+)?"
        r"(?:previous|prior|above|system)\s+"
        r"(?:instructions?|prompts?|rules?)\b",
        re.IGNORECASE,
    ),
)
_SYSTEM_PROMPT_REQUEST_PATTERNS = (
    re.compile(
        r"(?:시스템\s*프롬프트(?:\s*원문)?|"
        r"(?:너|당신|모델|챗봇)(?:의)?\s*(?:내부\s*)?"
        r"(?:지침|규칙|명령|프롬프트))"
        r".{0,40}?(?:출력|보여|공개|알려|복사|반복|인용)"
    ),
    re.compile(
        r"(?:출력|보여|공개|알려|복사|반복|인용)"
        r".{0,40}?(?:시스템\s*프롬프트(?:\s*원문)?|"
        r"(?:너|당신|모델|챗봇)(?:의)?\s*(?:내부\s*)?"
        r"(?:지침|규칙|명령|프롬프트))"
    ),
    re.compile(
        r"\b(?:reveal|show|print|display|repeat|quote|expose)\b"
        r".{0,50}?\b(?:system\s+prompt|hidden\s+instructions?|internal\s+instructions?)\b",
        re.IGNORECASE,
    ),
)
_DOMAIN_GUIDANCE_TERMS = re.compile(
    r"(?:급여|진료|허가|고시|임상|처방|치료|용법|의약|심사평가원|HIRA)",
    re.IGNORECASE,
)
_GUIDANCE_TERMS = re.compile(r"(?:지침|기준|가이드라인|주의사항)")

_SYSTEM_PROMPT_FINGERPRINTS = (
    "너는 JW 시장분석 채팅 에이전트다",
    "제공된 확정 fact만 근거로 답변 전체를",
    "확정 fact set:",
)
_SYSTEM_ROLE_MARKUP_PATTERNS = (
    re.compile(r"<\s*/?\s*system(?:\s[^>]*)?>", re.IGNORECASE),
    re.compile(r"""["']role["']\s*:\s*["']system["']""", re.IGNORECASE),
    re.compile(r"\bSYSTEM\s+PROMPT\s*:", re.IGNORECASE),
)


def evaluate_input_policy(question: str) -> dict[str, object]:
    """Classify high-confidence instruction manipulation without blocking."""

    normalized = _normalize(question)
    if _matches_any(normalized, _INSTRUCTION_OVERRIDE_PATTERNS):
        return _input_decision("flagged", "instruction_override")
    if _matches_any(normalized, _SYSTEM_PROMPT_REQUEST_PATTERNS):
        return _input_decision("flagged", "system_prompt_request")
    if _DOMAIN_GUIDANCE_TERMS.search(normalized) and _GUIDANCE_TERMS.search(normalized):
        return _input_decision("allow", "normal_domain_guidance")
    return _input_decision("allow")


def evaluate_output_leakage(answer: str) -> dict[str, object]:
    """Observe system-prompt fingerprints while preserving the answer bytes."""

    normalized = _normalize(answer)
    if any(fingerprint.casefold() in normalized.casefold() for fingerprint in _SYSTEM_PROMPT_FINGERPRINTS):
        return _output_decision("flagged", "system_prompt_fingerprint")
    if _matches_any(normalized, _SYSTEM_ROLE_MARKUP_PATTERNS):
        return _output_decision("flagged", "system_role_markup")
    return _output_decision("allow")


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or ""))


def _matches_any(value: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(value) is not None for pattern in patterns)


def _input_decision(verdict: str, *reason_codes: str) -> dict[str, object]:
    return {
        "mode": _INPUT_MODE,
        "verdict": verdict,
        "reason_codes": tuple(reason_codes),
    }


def _output_decision(verdict: str, *reason_codes: str) -> dict[str, object]:
    return {
        "mode": _INPUT_MODE,
        "verdict": verdict,
        "reason_codes": tuple(reason_codes),
        "user_surface_action": "observe_only" if verdict == "flagged" else "none",
    }
