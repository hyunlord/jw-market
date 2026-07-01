from __future__ import annotations

from collections.abc import Iterable, Mapping
import json
from typing import Any


VALID_BQ_IDS = frozenset({"Q1", "Q2", "Q2.5", "Q3", "Q4", "Q5", "Q1/Q5"})
VALID_SOURCES = frozenset({"metrics", "external_api", "document", "none", "resolver", "deep_analysis_events"})
VALID_SCOPES = frozenset({"single_brand", "portfolio"})


def parse_json_object(raw: str) -> Mapping[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise json.JSONDecodeError("JSON object not found", raw, 0)
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise TypeError("LLM router output must be a JSON object")
    return parsed


def valid_bq_ids(value: Any) -> tuple[str, ...]:
    return tuple(item for item in string_items(value) if item in VALID_BQ_IDS)


def valid_sources(value: Any, question: str, has_documents: bool) -> tuple[str, ...]:
    allow_document = has_documents or any(token in question for token in ("업로드", "문서", "가이드라인"))
    sources: list[str] = []
    for item in string_items(value):
        if item not in VALID_SOURCES:
            continue
        if item == "document" and not allow_document:
            continue
        sources.append(item)
    return tuple(sources)


def first_valid_bq(value: Any) -> str | None:
    values = valid_bq_ids(value)
    return values[0] if values else None


def string_items(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Iterable):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def valid_scope(value: Any) -> str:
    return value if isinstance(value, str) and value in VALID_SCOPES else "single_brand"


def confidence(data: Mapping[str, Any]) -> float | None:
    value = data.get("confidence")
    if isinstance(value, int | float):
        return float(value)
    return None


def bool_value(value: Any) -> bool:
    return bool(value) if isinstance(value, bool) else False


def question_label(bq: str) -> str:
    labels = {
        "Q1": "시장정의·규모·성장예측",
        "Q2": "경쟁 차별점·점유율·처방요인",
        "Q2.5": "개발중 경쟁·임상단계",
        "Q3": "Segment 처방추이",
        "Q4": "영업 Impact",
        "Q5": "타겟/허가/급여/임상/포트폴리오/사업성",
        "Q1/Q5": "업로드 문서 근거 검색",
    }
    return labels.get(bq, bq)
