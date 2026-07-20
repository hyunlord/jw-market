from __future__ import annotations

from dataclasses import dataclass
import re

from jw_chat_agent_poc.service.conversation import ConversationTurn


_PREVIOUS_YEAR_RE = re.compile(r"^\s*그\s*전\s*(?:해|년)(?:는|은)?\s*[?!.]?\s*$")
_COLLOQUIAL_TREND_RE = re.compile(
    r"^\s*(?:걔|그\s*애)(?:의)?\s*최근\s*(?:추세|경향|흐름)(?:은|는)?\s*[?!.]?\s*$"
)
_MARKET_METRIC_RE = re.compile(
    r"^\s*시장\s*(?P<metric>규모|HHI|집중도|CR\s*5)(?:는|은)?\s*[?!.]?\s*$",
    re.IGNORECASE,
)
_VIEW_ONLY_RE = re.compile(
    r"^\s*(?P<view>일반\s*뷰|전략\s*뷰|경쟁\s*뷰)(?:로|으로)?(?:는|은)?\s*[?!.]?\s*$"
)
_MARKET_REPLACEMENT_RE = re.compile(
    r"^\s*(?P<market>[가-힣A-Za-z0-9_-]{2,40}\s*시장)(?:에서는|에서|으로는|은|는)?\s*[?!.]?\s*$"
)
_BARE_SUBJECT_RE = re.compile(
    r"^\s*(?P<subject>[가-힣A-Za-z0-9_-]{2,30}?)(?:은|는|이|가)\s*[?!.]?\s*$"
)
_FILE_CHANNEL_REFERENCE_RE = re.compile(r"그\s*중\s*\d{1,3}\s*번\s*채널")
_EXPLICIT_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})\s*년")
_MARKET_LABEL_RE = re.compile(r"([가-힣A-Za-z0-9_-]{2,40}\s*시장)")
_INTERNAL_MARKET_ID_RE = re.compile(r"(?:ml|strategy)_\d+", re.IGNORECASE)
_VIEW_LABELS = {
    "market_landscape": "전략뷰",
    "competitive_dynamics": "경쟁뷰",
    "general_view": "일반뷰",
}
_NON_BRAND_SUBJECTS = frozenset(
    {
        "날씨",
        "시장",
        "매출",
        "점유율",
        "순위",
        "성분",
        "임상",
        "허가",
        "부작용",
        "환자수",
        "일반뷰",
        "전략뷰",
        "경쟁뷰",
    }
)


@dataclass(frozen=True, slots=True)
class DeterministicFollowup:
    resolved_question: str
    brand: str | None = None
    unresolved_reference: bool = False
    interpretation_notice: str | None = None


def requires_deterministic_followup(question: str) -> bool:
    if any(
        pattern.match(question)
        for pattern in (
            _PREVIOUS_YEAR_RE,
            _COLLOQUIAL_TREND_RE,
            _MARKET_METRIC_RE,
            _VIEW_ONLY_RE,
            _MARKET_REPLACEMENT_RE,
        )
    ):
        return True
    subject = _bare_subject(question)
    return bool(subject and subject not in _NON_BRAND_SUBJECTS)


def is_file_axis_followup(question: str) -> bool:
    return bool(_FILE_CHANNEL_REFERENCE_RE.search(question))


def resolve_deterministic_followup(
    question: str,
    previous_turn: ConversationTurn | None,
    inherited_intent: str,
) -> DeterministicFollowup | None:
    if _PREVIOUS_YEAR_RE.match(question):
        if previous_turn is None or not previous_turn.slots.anchor_brand or not inherited_intent:
            return _unresolved(question)
        year = _previous_year(previous_turn)
        if year is None:
            return _unresolved(question)
        brand = previous_turn.slots.anchor_brand
        return _resolved(
            f"{brand} {year}년 {inherited_intent}은?",
            brand,
            f"{brand}의 {year}년 {inherited_intent}로 이해했어요.",
        )

    if _COLLOQUIAL_TREND_RE.match(question):
        if previous_turn is None or not previous_turn.slots.anchor_brand or not inherited_intent:
            return _unresolved(question)
        brand = previous_turn.slots.anchor_brand
        metric = _base_metric(inherited_intent)
        return _resolved(
            f"{brand} 최근 {metric} 추세는?",
            brand,
            f"{brand}의 최근 {metric} 추세로 이해했어요.",
        )

    market_metric = _MARKET_METRIC_RE.match(question)
    if market_metric is not None:
        if previous_turn is None or not (market := _market_label(previous_turn)):
            return _unresolved(question)
        metric = " ".join(market_metric.group("metric").upper().split())
        return _resolved(
            f"{market} {metric}는?",
            None,
            f"{market}의 {metric}로 이해했어요.",
        )

    view_match = _VIEW_ONLY_RE.match(question)
    if view_match is not None:
        if previous_turn is None or not inherited_intent:
            return _unresolved(question)
        subject = previous_turn.slots.anchor_brand or _market_label(previous_turn)
        if not subject:
            return _unresolved(question)
        view = view_match.group("view").replace(" ", "")
        brand = previous_turn.slots.anchor_brand
        return _resolved(
            f"{subject} {view} {inherited_intent}는?",
            brand,
            f"{subject}의 {inherited_intent}을(를) {view} 기준으로 이해했어요.",
        )

    market_match = _MARKET_REPLACEMENT_RE.match(question)
    if market_match is not None:
        if previous_turn is None or not previous_turn.slots.anchor_brand or not inherited_intent:
            return _unresolved(question)
        brand = previous_turn.slots.anchor_brand
        market = " ".join(market_match.group("market").split())
        view = _VIEW_LABELS.get(previous_turn.slots.view or "", "")
        scoped_intent = f"{view} {inherited_intent}" if view else inherited_intent
        return _resolved(
            f"{market}에서 {brand} {scoped_intent}은?",
            brand,
            f"{market}에서 {brand}의 {scoped_intent}로 이해했어요.",
        )

    subject = _bare_subject(question)
    if not subject or subject in _NON_BRAND_SUBJECTS:
        return None
    if previous_turn is None:
        return _unresolved(question)
    if any(
        (
            previous_turn.slots.file_name,
            previous_turn.slots.file_measure,
            previous_turn.slots.file_manufacturer,
            previous_turn.slots.file_sheet,
        )
    ):
        return None
    if not inherited_intent:
        return None
    return _resolved(
        f"{subject} {inherited_intent}은?",
        subject,
        f"{subject}의 {inherited_intent}로 이해했어요.",
    )


def _previous_year(turn: ConversationTurn) -> int | None:
    match = _EXPLICIT_YEAR_RE.search(turn.question)
    if match is None and turn.slots.period:
        match = re.match(r"(20\d{2})", turn.slots.period)
    if match is None:
        return None
    return int(match.group(1)) - 1


def _market_label(turn: ConversationTurn) -> str:
    definition = " ".join((turn.slots.market_definition or "").split())
    if definition and _INTERNAL_MARKET_ID_RE.fullmatch(definition) is None:
        return definition
    match = _MARKET_LABEL_RE.search(turn.question)
    if match is not None:
        return " ".join(match.group(1).split())
    return definition


def _base_metric(intent: str) -> str:
    return re.sub(r"\s*(?:경향성|추이|흐름|변화)\s*$", "", intent).strip()


def _bare_subject(question: str) -> str:
    match = _BARE_SUBJECT_RE.match(question)
    return match.group("subject") if match else ""


def _resolved(question: str, brand: str | None, notice: str) -> DeterministicFollowup:
    return DeterministicFollowup(
        resolved_question=question,
        brand=brand,
        interpretation_notice=notice,
    )


def _unresolved(question: str) -> DeterministicFollowup:
    return DeterministicFollowup(resolved_question=question, unresolved_reference=True)
