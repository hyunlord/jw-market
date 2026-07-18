from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .model import EvalQuestion, GoldObservation, JsonValue, RawResult, ScoredRow

TRANSPARENT_TERMS = (
    "없",
    "0건",
    "지원",
    "불가",
    "한계",
    "확인되지",
    "데이터",
    "기준",
    "전략뷰",
    "경쟁군",
    "출처",
    "근거",
    "fact_id",
    "숫자 검증",
)
UNSUPPORTED_TERMS = ("없", "0건", "지원", "불가", "한계", "확인되지", "데이터 없음")
CLARIFY_TERMS = ("어떤", "무슨", "기준", "구체", "선택", "확인")
KEY_ALIASES = {
    "market_size_recent": ("market_size_recent", "market_size_recent_krw", "market_size_억원"),
    "ms": ("ms", "ms_recent_pct", "market_share"),
    "market_share": ("market_share", "ms_recent_pct", "ms"),
}
QUESTION_STOPWORDS = {
    "리바로",
    "리바로젯",
    "알려줘",
    "보여줘",
    "같은",
    "시장",
    "기준",
    "최근",
    "매출",
}


def _walk_values(value: JsonValue, target_key: str) -> list[int | float | str]:
    found: list[int | float | str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == target_key and isinstance(child, int | float | str):
                found.append(child)
            found.extend(_walk_values(child, target_key))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_values(child, target_key))
    return found


def _as_float(value: int | float | str) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _number_tokens(value: float, kind: str) -> set[str]:
    tokens: set[str] = set()
    if kind == "krw":
        eok = value / 100_000_000
        tokens.update(
            {
                f"{eok:,.0f}",
                f"{eok:,.1f}",
                f"{eok:,.2f}",
                f"{eok:.0f}",
                f"{eok:.1f}",
                f"{eok:.2f}",
                f"{int(round(value)):,}",
            }
        )
    elif kind == "percent":
        pct = value * 100 if abs(value) <= 1 else value
        tokens.update({f"{pct:.0f}", f"{pct:.1f}", f"{pct:.2f}", f"{pct:.3f}"})
    elif kind == "rank":
        tokens.add(str(int(round(value))))
    else:
        tokens.update({f"{value:.0f}", f"{value:.2f}", f"{value:.3f}", f"{value:.4f}"})
    return tokens


def _contains_observation(answer: str, observation: GoldObservation) -> bool:
    if isinstance(observation.value, str):
        return observation.value in answer
    numeric = _as_float(observation.value)
    if numeric is None or not math.isfinite(numeric):
        return False
    normalized = answer.replace(",", "")
    for token in _number_tokens(numeric, observation.kind):
        normalized_token = token.replace(",", "")
        if re.search(rf"(?<![\d.]){re.escape(normalized_token)}(?![\d.])", normalized):
            return True
    return False


def _answer_text(raw: RawResult | None) -> str:
    if raw is None:
        return ""
    value = raw.result.get("answer", "")
    if isinstance(value, str):
        return value
    return str(value)


def extract_gold(question: EvalQuestion, raw: RawResult | None) -> tuple[GoldObservation, ...]:
    if raw is None:
        return ()
    observations: list[GoldObservation] = []
    for key in question.gold_keys:
        search_keys = KEY_ALIASES.get(key.key, (key.key,))
        values: list[int | float | str] = []
        for search_key in search_keys:
            values.extend(_walk_values(raw.result, search_key))
        for value in values[:2]:
            observations.append(
                GoldObservation(
                    label=key.label,
                    key=key.key,
                    kind=key.kind,
                    value=value,
                )
            )
    return tuple(observations)


@dataclass(frozen=True, slots=True)
class _NumericScore:
    accuracy: str
    checked: int
    population: int
    matched: int

    @property
    def status(self) -> str:
        return "OK" if self.accuracy == "O" else "FAIL"

    def acceptance_note(self) -> str:
        return (
            f"numeric_accuracy={self.status}; checked={self.checked}; "
            f"population={self.population}; matched={self.matched}"
        )


def _numeric_score(answer: str, observations: tuple[GoldObservation, ...]) -> _NumericScore:
    population = len(observations)
    hits = tuple(_contains_observation(answer, observation) for observation in observations)
    checked = len(hits)
    matched = sum(hits)
    complete = population > 0 and checked == population and matched == population
    return _NumericScore(
        accuracy="O" if complete else "X",
        checked=checked,
        population=population,
        matched=matched,
    )


def _score_numeric(answer: str, observations: tuple[GoldObservation, ...]) -> str:
    """Return the legacy O/X value while requiring every gold observation."""

    return _numeric_score(answer, observations).accuracy


def _score_qualitative(question: EvalQuestion, answer: str, ok: bool) -> tuple[int, str]:
    if not ok or not answer.strip():
        return 1, "응답 오류 또는 빈 응답"
    transparent = any(term in answer for term in TRANSPARENT_TERMS)
    unsupported_clear = any(term in answer for term in UNSUPPORTED_TERMS)
    if question.expected_behavior in {"unsupported", "no_data"}:
        score = 5 if unsupported_clear else 2
        note = "기권/0건 투명" if unsupported_clear else "unsupported/no-data 투명성 부족"
        return score, note
    if question.expected_behavior == "ambiguous":
        score = 4 if any(term in answer for term in CLARIFY_TERMS) or "?" in answer else 2
        return score, "모호성 처리 평가"
    if question.expected_behavior == "complex_target":
        terms = [
            token
            for token in re.split(r"\s+", question.question.replace("?", ""))
            if len(token) >= 2 and token not in QUESTION_STOPWORDS
        ]
        overlap = sum(1 for token in terms if token in answer)
        if unsupported_clear:
            return 4, "복합 질문 한계 명시"
        if overlap >= 2 and len(answer) > 100:
            return 4, "복합 질문 부분 처리"
        return 2, "복합 질문을 일반 단발 답변으로 처리"
    score = 3
    if len(re.sub(r"\s+", "", answer)) >= 40:
        score += 1
    if transparent:
        score += 1
    return min(score, 5), "규칙 기반 정성점수"


def score_question(question: EvalQuestion, raw: RawResult | None) -> ScoredRow:
    answer = _answer_text(raw)
    ok = bool(raw.ok) if raw is not None else False
    observations = extract_gold(question, raw)
    numeric_score = _numeric_score(answer, observations)
    qualitative, note = _score_qualitative(question, answer, ok)
    note = f"{note}; {numeric_score.acceptance_note()}"
    if raw is not None and raw.error:
        note = f"{note}; error={raw.error}"
    return ScoredRow(
        question=question,
        answer=answer,
        numeric_accuracy=numeric_score.accuracy,
        qualitative_score=qualitative,
        note=note,
        gold_observations=observations,
        ok=ok,
    )
