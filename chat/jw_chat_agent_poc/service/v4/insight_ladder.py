from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from jw_chat_agent_poc.service.v4.derived_metrics import DerivedMetricCard
from jw_chat_agent_poc.service.v4.fact_digest import DerivedCoreCard, FactDigest
from jw_chat_agent_poc.service.v4.insight_claims import evidence_catalog_payload

_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")
_L1_METRIC_PRIORITY = (
    "growth_spread_vs_market",
    "brand_growth_rate",
    "market_growth_rate",
    "gap_change",
    "share_delta",
    "rank_delta",
    "approval_age",
    "time_since_last_change",
    "reexam_remaining",
    "expiry_remaining",
    "active_patent_count",
    "patient_yoy_growth",
    "gender_ratio",
    "age_top_segment_share",
    "months_since_latest_registration",
    "count_by_status",
    "approvals_by_strength",
    "earliest_active_expiry",
    "latest_active_expiry",
    "absolute_gap",
)
_PROMPT_RECORD_LIMITS = {
    "clinicaltrials": 8,
    "hira": 24,
    "patent": 16,
    "document": 16,
}
_DEFAULT_PROMPT_RECORD_LIMIT = 24
_PROMPT_STRING_LIMITS = {
    "clinicaltrials": 240,
    "hira": 400,
    "patent": 400,
    "document": 600,
}
_DEFAULT_PROMPT_STRING_LIMIT = 400
_PROMPT_SEQUENCE_LIMITS = {
    "clinicaltrials": 6,
    "hira": 8,
    "patent": 8,
    "document": 8,
}
_DEFAULT_PROMPT_SEQUENCE_LIMIT = 8
_PROMPT_METRIC_INPUT_LIMIT = 24


@dataclass(frozen=True)
class InsightMaterial:
    digest: FactDigest
    trace: dict[str, Any]


@dataclass(frozen=True)
class DeterministicInsight:
    text: str
    manifest: dict[str, Any]


def prepare_insight_material(
    digest: FactDigest,
    *,
    force_compression: bool,
    char_threshold: int,
    card_limit: int,
    metric_limit: int | None,
) -> InsightMaterial:
    """Bound a prompt-only copy without mutating the complete source digest."""

    raw_chars = _prompt_chars(digest)
    bounded, record_caps = _record_bounded_digest(digest)
    compression_applied = force_compression or raw_chars > char_threshold
    if compression_applied:
        prepared, metric_input_caps = _compressed_digest(
            bounded,
            card_limit=card_limit,
            metric_limit=metric_limit,
        )
    else:
        prepared = bounded
        metric_input_caps = {
            "selected": sum(len(metric.inputs) for metric in prepared.derived_metrics),
            "omitted": 0,
        }
    compressed_chars = _prompt_chars(prepared)
    return InsightMaterial(
        digest=prepared,
        trace={
            "digest_char_count": raw_chars,
            "dm_count": len(digest.derived_metrics),
            "file_table_row_count": _file_table_row_count(digest.cards),
            "compression_applied": compression_applied,
            "raw_token_estimate": _token_estimate(raw_chars),
            "compressed_token_estimate": _token_estimate(compressed_chars),
            "selected_card_count": len(prepared.cards),
            "selected_dm_count": len(prepared.derived_metrics),
            "metric_input_caps": metric_input_caps,
            "record_caps": record_caps,
        },
    )


def build_l1_insight(digest: FactDigest) -> DeterministicInsight:
    """Render distinct, audit-friendly relation sentences from dm cards only."""

    metrics = _prioritized_unique_metrics(digest.derived_metrics)
    sentences: list[str] = []
    claims: list[dict[str, Any]] = []
    predicates: set[str] = set()
    for metric in metrics:
        sentence = _metric_sentence(metric)
        if not sentence:
            continue
        predicate = sentence.rstrip(".").rsplit(" ", 1)[-1]
        if predicate in predicates:
            continue
        predicates.add(predicate)
        sentences.append(sentence)
        claims.append(
            {
                "claim_index": len(claims) + 1,
                "claim_type": "CALC",
                "evidence_ids": [metric.id, *metric.inputs],
                "hedge": "none",
                "metric_type": metric.type,
            }
        )
        if len(sentences) == 4:
            break

    if not sentences:
        sentences.append(
            "계산 가능한 파생 관계 지표가 없어 원천 사실의 추가 비교는 생략합니다."
        )
        claims.append(
            {
                "claim_index": 1,
                "claim_type": "OBS",
                "evidence_ids": [],
                "hedge": "none",
                "metric_type": "no_derived_metric",
            }
        )
    type_counts: dict[str, int] = {}
    for claim in claims:
        claim_type = str(claim["claim_type"])
        type_counts[claim_type] = type_counts.get(claim_type, 0) + 1
    return DeterministicInsight(
        text="## 종합 인사이트\n" + " ".join(sentences),
        manifest={
            "parse_status": "deterministic_l1",
            "claim_count": len(claims),
            "type_counts": type_counts,
            "claims": claims,
            "verification": {
                "claims": claims,
                "hard_block_count": 0,
                "route": "dm_deterministic",
            },
        },
    )


def build_grounded_facts_extension(digest: FactDigest) -> str:
    """Render concrete source values when generated facts remain short."""

    sentences: list[str] = []
    for card in digest.cards:
        if card.card_type == "file_aggregate" or card.source in {
            "document",
            "document_sql",
        }:
            file_extension = _file_grounded_facts(card)
            if file_extension:
                sentences.append(file_extension)
        if card.source != "hira":
            continue
        representatives = card.full_stats.get("code_representatives")
        if not isinstance(representatives, Mapping):
            continue
        details: list[tuple[str, str, Decimal]] = []
        for code in card.full_stats.get("codes") or representatives:
            representative = representatives.get(str(code))
            if not isinstance(representative, Mapping):
                continue
            value = representative.get("value")
            if value is None:
                continue
            try:
                numeric_value = Decimal(str(value))
            except InvalidOperation:
                continue
            name = str(representative.get("disease_name") or "").strip()
            period = str(representative.get("period") or "").strip()
            label = f"{code}({name})" if name else str(code)
            period_text = f"{period}년 " if re.fullmatch(r"\d{4}", period) else f"{period} "
            details.append((label, period_text, numeric_value))
        if details:
            entity = card.entity or "요청 질환"
            total = sum((value for _, _, value in details), Decimal(0))
            ranked_labels = {
                label: rank
                for rank, (label, _, _) in enumerate(
                    sorted(details, key=lambda item: item[2], reverse=True),
                    start=1,
                )
            }
            sentences.append(
                f"보유 HIRA 원천에서 {entity} 관련 부모 상병코드 {len(details)}개의 "
                f"환자수를 전건 확인했으며, 코드별 환자수의 단순 합계는 "
                f"{_display_integer(total)}명입니다."
            )
            for label, period_text, value in details:
                share = (value / total * Decimal(100)) if total else Decimal(0)
                sentences.append(
                    f"{period_text}{label} 환자수는 {_display_integer(value)}명입니다. "
                    f"이는 {len(details)}개 부모 코드 단순 합계 {_display_integer(total)}명 중 "
                    f"{share.quantize(Decimal('0.01'))}%이며, 코드별 환자수 규모 "
                    f"{ranked_labels[label]}위에 해당합니다."
                )
            largest = max(details, key=lambda item: item[2])
            smallest = min(details, key=lambda item: item[2])
            gap = largest[2] - smallest[2]
            sentences.append(
                f"가장 큰 {largest[0]}와 가장 작은 {smallest[0]}의 환자수 차이는 "
                f"{_display_integer(gap)}명이며, 이 비교는 동일한 HIRA 기준연도와 "
                "부모 상병코드 층에서 계산했습니다."
            )
            if len(details) >= 2:
                top_two = sorted(details, key=lambda item: item[2], reverse=True)[:2]
                top_two_total = sum((item[2] for item in top_two), Decimal(0))
                remaining_total = total - top_two_total
                top_two_share = (
                    top_two_total / total * Decimal(100) if total else Decimal(0)
                )
                sentences.append(
                    f"환자수 상위 두 코드 {top_two[0][0]}와 {top_two[1][0]}의 합계는 "
                    f"{_display_integer(top_two_total)}명으로 전체 단순 합계의 "
                    f"{top_two_share.quantize(Decimal('0.01'))}%입니다. 나머지 "
                    f"{len(details) - 2}개 코드의 합계는 {_display_integer(remaining_total)}명이며, "
                    "각 비중은 위의 동일 모수로 계산했습니다."
                )
    if sentences:
        return " ".join(sentences)
    return _section_body(build_l1_insight(digest).text)


def _file_grounded_facts(card: DerivedCoreCard) -> str:
    facts = card.file_facts
    rows = tuple(
        row
        for row in facts.get("analytics_rows") or ()
        if isinstance(row, Sequence) and not isinstance(row, (str, bytes))
    )
    if not rows:
        return ""
    columns = tuple(str(value).strip() for value in facts.get("analytics_columns") or ())
    document = str(facts.get("document_name") or card.entity or "업로드 파일").strip()
    sheet = str(facts.get("sheet_name") or "시트 미제공").strip()
    operation = str(facts.get("analytics_operation") or "파일 집계").strip()
    sentences = [
        f"{document}의 {sheet} 시트에서 {operation} SQL을 실행해 {len(rows)}개 결과 행을 확인했습니다."
    ]
    target_chars = 1050
    for rank, row in enumerate(rows, start=1):
        values: list[str] = []
        for index, value in enumerate(row):
            if value is None or value == "":
                continue
            column = columns[index] if index < len(columns) and columns[index] else f"열 {index + 1}"
            values.append(f"{column} {_display_file_value(value)}")
        if values:
            sentences.append(f"결과 {rank}행은 " + " · ".join(values) + "입니다.")
        if rank >= 8 and len(re.sub(r"\s+", "", " ".join(sentences))) >= target_chars:
            break
        if rank >= 20:
            break
    for index, column in enumerate(columns):
        if not _is_additive_file_column(column):
            continue
        numeric_values = tuple(
            numeric
            for row in rows
            if index < len(row)
            for numeric in (_numeric_decimal(row[index]),)
            if numeric is not None
        )
        if len(numeric_values) < 2:
            continue
        total = sum(numeric_values, Decimal(0))
        maximum = max(numeric_values)
        minimum = min(numeric_values)
        sentences.append(
            f"{column} 열의 {len(numeric_values)}개 수치 합계는 {_display_file_value(total)}, "
            f"최대값은 {_display_file_value(maximum)}, 최소값은 {_display_file_value(minimum)}, "
            f"최대와 최소의 차이는 {_display_file_value(maximum - minimum)}입니다."
        )
    return " ".join(sentences)


def _is_additive_file_column(column: str) -> bool:
    if re.search(
        r"(?:price|가격|판매가|매입가|m/s|share|rate|ratio|growth|cagr|증감|성장|비율|점유)",
        column,
        re.IGNORECASE,
    ):
        return False
    return bool(
        re.search(
            r"(?:매출|sales|revenue|value|수량|units?|volume|count|건수)",
            column,
            re.IGNORECASE,
        )
    )


def _display_file_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return f"{int(value):,}"
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    numeric = _numeric_decimal(value)
    if numeric is not None:
        return _display_file_value(numeric)
    return str(value).strip()


def _numeric_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _display_integer(value: Any) -> str:
    try:
        return f"{int(Decimal(str(value))):,}"
    except (InvalidOperation, ValueError):
        return str(value)


def _section_body(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(
        lines[1:] if lines and lines[0].startswith("## ") else lines
    ).strip()


def _prompt_chars(digest: FactDigest) -> int:
    payload = {
        "fact_digest": digest.repair_prompt_payload(),
        "evidence_catalog": evidence_catalog_payload(digest),
    }
    return len(json.dumps(payload, ensure_ascii=False, default=str))


def _token_estimate(chars: int) -> int:
    return (chars + 3) // 4


def _compressed_digest(
    digest: FactDigest,
    *,
    card_limit: int,
    metric_limit: int | None,
) -> tuple[FactDigest, dict[str, int]]:
    ranked_cards = sorted(
        enumerate(digest.cards),
        key=lambda item: (-_card_relevance(digest.question, item[1]), item[0]),
    )
    selected_cards = tuple(
        _compress_card(card) for _, card in ranked_cards[:card_limit]
    )
    metrics = _prioritized_metrics(digest.derived_metrics)
    selected_metrics = tuple(metrics if metric_limit is None else metrics[:metric_limit])
    metric_input_caps = {
        "selected": sum(
            min(len(metric.inputs), _PROMPT_METRIC_INPUT_LIMIT)
            for metric in selected_metrics
        ),
        "omitted": sum(
            max(len(metric.inputs) - _PROMPT_METRIC_INPUT_LIMIT, 0)
            for metric in selected_metrics
        ),
    }
    prompt_metrics = tuple(
        metric.model_copy(
            update={"inputs": metric.inputs[:_PROMPT_METRIC_INPUT_LIMIT]}
        )
        for metric in selected_metrics
    )
    return digest.model_copy(
        update={
            "cards": selected_cards,
            "visible_record_ids": (),
            "visible_tables": (),
            "derived_metrics": prompt_metrics,
        }
    ), metric_input_caps


def _record_bounded_digest(
    digest: FactDigest,
) -> tuple[FactDigest, dict[str, dict[str, int]]]:
    """Cap prompt-only record arrays while retaining source totals and summaries."""

    capped_cards: list[DerivedCoreCard] = []
    manifest: dict[str, dict[str, int]] = {}
    for card in digest.cards:
        limit = _PROMPT_RECORD_LIMITS.get(card.source, _DEFAULT_PROMPT_RECORD_LIMIT)
        representative, _ = _cap_mapping_sequences(card.representative, limit)
        temporal_stats, _ = _cap_mapping_sequences(card.temporal_stats, limit)
        file_facts, _ = _cap_mapping_sequences(card.file_facts, limit)
        visible_rows = card.visible_rows[:limit]
        capped_cards.append(
            card.model_copy(
                update={
                    "representative": representative,
                    "temporal_stats": temporal_stats,
                    "file_facts": file_facts,
                    "visible_rows": visible_rows,
                    "evidence_ids": card.evidence_ids[:limit],
                }
            )
        )
        source = manifest.setdefault(card.source, {"selected": 0, "omitted": 0})
        source["selected"] += min(card.received_count, limit)
        source["omitted"] += max(card.received_count - limit, 0)
    return digest.model_copy(update={"cards": tuple(capped_cards)}), manifest


def _cap_mapping_sequences(
    value: Mapping[str, Any], limit: int
) -> tuple[dict[str, Any], int]:
    removed = 0
    result: dict[str, Any] = {}
    items = list(value.items())
    if len(items) > limit and all(isinstance(item, Mapping) for _, item in items):
        removed += len(items) - limit
        items = items[:limit]
    for key, item in items:
        if isinstance(item, Mapping):
            result[str(key)], nested_removed = _cap_mapping_sequences(item, limit)
            removed += nested_removed
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            result[str(key)] = list(item[:limit])
            removed += max(0, len(item) - limit)
        else:
            result[str(key)] = item
    return result, removed


def _compress_card(card: DerivedCoreCard) -> DerivedCoreCard:
    return card.model_copy(
        update={
            "representative": _compact_mapping(card.representative, source=card.source),
            "distributions": _compact_mapping(card.distributions, source=card.source),
            "full_stats": _compact_mapping(card.full_stats, source=card.source),
            "temporal_stats": _compact_mapping(card.temporal_stats, source=card.source),
            "file_facts": _compact_mapping(card.file_facts, source=card.source),
            "visible_rows": (),
        }
    )


def _compact_mapping(value: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    return {
        str(key): _compact_value(item, source=source) for key, item in value.items()
    }


def _compact_value(value: Any, *, source: str) -> Any:
    if isinstance(value, str):
        limit = _PROMPT_STRING_LIMITS.get(source, _DEFAULT_PROMPT_STRING_LIMIT)
        return value if len(value) <= limit else value[:limit]
    if isinstance(value, Mapping):
        return _compact_mapping(value, source=source)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        limit = _PROMPT_SEQUENCE_LIMITS.get(source, _DEFAULT_PROMPT_SEQUENCE_LIMIT)
        return [_compact_value(item, source=source) for item in value[:limit]]
    return value


def _card_relevance(question: str, card: DerivedCoreCard) -> int:
    tokens = set(_TOKEN_RE.findall(question.casefold()))
    if not tokens:
        return 0
    surface = json.dumps(card.model_dump(mode="json"), ensure_ascii=False).casefold()
    return sum(1 for token in tokens if token in surface)


def _file_table_row_count(cards: Sequence[DerivedCoreCard]) -> int:
    return sum(_row_count(card.file_facts) for card in cards)


def _row_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return sum(
            len(item)
            if key in {"rows", "result_rows", "analytics_rows"}
            and isinstance(item, list)
            else _row_count(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return sum(_row_count(item) for item in value)
    return 0


def _prioritized_unique_metrics(
    metrics: Sequence[DerivedMetricCard],
) -> list[DerivedMetricCard]:
    ordered = _prioritized_metrics(metrics)
    selected: list[DerivedMetricCard] = []
    seen_types: set[str] = set()
    for metric in ordered:
        if metric.type in seen_types:
            continue
        seen_types.add(metric.type)
        selected.append(metric)
    return selected


def _prioritized_metrics(
    metrics: Sequence[DerivedMetricCard],
) -> list[DerivedMetricCard]:
    priority = {
        metric_type: index for index, metric_type in enumerate(_L1_METRIC_PRIORITY)
    }
    ordered = sorted(
        enumerate(metrics),
        key=lambda item: (priority.get(item[1].type, len(priority)), item[0]),
    )
    return [metric for _, metric in ordered]


def _metric_sentence(metric: DerivedMetricCard) -> str:
    value = _display_value(metric.value)
    magnitude = _display_value(abs(_decimal(metric.value)))
    period = f"{metric.period} " if metric.period else ""
    match metric.type:
        case "growth_spread_vs_market":
            direction = (
                "상회했습니다" if _decimal(metric.value) >= 0 else "하회했습니다"
            )
            return f"{metric.entity}의 성장률은 {period}시장 성장률을 {magnitude}{metric.unit} {direction}."
        case "brand_growth_rate" | "market_growth_rate" | "patient_yoy_growth":
            return f"{metric.entity}의 {period}성장률은 {value}{metric.unit}입니다."
        case "gap_change":
            direction = (
                "확대됐습니다" if _decimal(metric.value) >= 0 else "축소됐습니다"
            )
            return f"{metric.entity}의 {period}격차는 {magnitude}{metric.unit} {direction}."
        case "share_delta":
            direction = (
                "높아졌습니다" if _decimal(metric.value) >= 0 else "낮아졌습니다"
            )
            return f"{metric.entity}의 {period}점유율은 {magnitude}{metric.unit} {direction}."
        case "rank_delta":
            direction = "낮아졌습니다" if _decimal(metric.value) > 0 else "높아졌습니다"
            return f"{metric.entity}의 {period}순위는 {magnitude}계단 {direction}."
        case (
            "approval_age"
            | "time_since_last_change"
            | "reexam_remaining"
            | "expiry_remaining"
            | "months_since_latest_registration"
        ):
            return f"{metric.entity}의 {period}경과·잔여 기간은 {value}{metric.unit}입니다."
        case "active_patent_count" | "approvals_by_strength" | "count_by_status":
            return f"{metric.entity}의 {period}확인 건수는 {value}{metric.unit}으로 집계됐습니다."
        case "gender_ratio" | "age_top_segment_share":
            return f"{metric.entity}의 {period}구성 지표는 {value}{metric.unit}으로 나타났습니다."
        case "earliest_active_expiry" | "latest_active_expiry":
            return f"{metric.entity}의 {period}유효 특허 만료 기준일은 {value}{metric.unit}입니다."
        case "absolute_gap":
            return f"{metric.entity}의 {period}절대 격차는 {value}{metric.unit}으로 계산됐습니다."
        case _:
            return ""


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)


def _display_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


__all__ = [
    "DeterministicInsight",
    "InsightMaterial",
    "build_grounded_facts_extension",
    "build_l1_insight",
    "prepare_insight_material",
]
