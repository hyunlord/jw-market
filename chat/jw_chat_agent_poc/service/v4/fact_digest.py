from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from jw_chat_agent_poc.hira_catalog import (
    catalog_parent_codes_for_name,
    requested_population_layer,
    select_catalog_population,
)
from jw_chat_agent_poc.hira_surface import (
    hira_disease_mapping,
    hira_is_aggregate_row,
    hira_record_matches_question,
    hira_row_reconciliation,
    hira_summary_payload,
    requested_hira_axes,
)
from jw_chat_agent_poc.service.v4.contracts import AnswerContract, AnswerShape
from jw_chat_agent_poc.service.v4.derived_metrics import (
    DerivedMetricCard,
    DerivedMetricManifest,
    build_derived_metrics,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    DeterministicRender,
    EvidenceRecord,
    EvidenceSet,
)
from jw_chat_agent_poc.service.v4.semantic_contract import (
    ContractSlotCoverage,
    contract_slot_coverage,
)
from jw_chat_agent_poc.service.v4.temporal_analysis import (
    clinical_time_axis,
    nedrug_time_axis,
    normalize_surface_dates,
    patent_time_axis,
)
from jw_chat_agent_poc.service.web_relevance import filter_web_results

AnswerType = Literal[
    "patent",
    "clinical",
    "disease",
    "market",
    "mixed",
    "file_aggregate",
    "document_summary",
    "general",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DerivedCoreCard(_FrozenModel):
    card_type: AnswerType
    source: str
    evidence_ids: tuple[str, ...] = ()
    received_count: int = 0
    matched_count: int = 0
    visible_count: int = 0
    entity: str | None = None
    metric: str | None = None
    period: str | None = None
    unit: str | None = None
    representative: dict[str, Any] = Field(default_factory=dict)
    distributions: dict[str, dict[str, int]] = Field(default_factory=dict)
    full_stats: dict[str, Any] = Field(default_factory=dict)
    temporal_stats: dict[str, Any] = Field(default_factory=dict)
    file_facts: dict[str, Any] = Field(default_factory=dict)
    visible_rows: tuple[dict[str, Any], ...] = ()
    derived_fields: tuple[str, ...] = ()


class FactDigest(_FrozenModel):
    question: str
    answer_type: AnswerType
    answer_contract: AnswerContract | None = None
    reference_date: str | None = None
    cards: tuple[DerivedCoreCard, ...] = ()
    visible_record_ids: tuple[str, ...] = ()
    source_received_counts: dict[str, int] = Field(default_factory=dict)
    source_visible_counts: dict[str, int] = Field(default_factory=dict)
    visible_tables: tuple[str, ...] = ()
    derived_metrics: tuple[DerivedMetricCard, ...] = ()
    derived_metrics_manifest: DerivedMetricManifest = Field(
        default_factory=DerivedMetricManifest,
    )

    def prompt_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def compact_prompt_payload(self) -> dict[str, Any]:
        """Return code-owned facts without duplicating rendered markdown tables."""
        payload = self.prompt_payload()
        payload.pop("visible_tables", None)
        for card in payload["cards"]:
            card["visible_rows"] = []
            card["evidence_ids"] = []
        return payload

    def repair_prompt_payload(self) -> dict[str, Any]:
        """Bound document prose for the single final insight-repair request."""
        payload = self.compact_prompt_payload()
        payload["visible_record_ids"] = []
        for card in payload["cards"]:
            if card.get("card_type") != "document_summary":
                continue
            representative = card.get("representative")
            if isinstance(representative, dict):
                representative.pop("content", None)
            file_facts = card.get("file_facts")
            if not isinstance(file_facts, dict):
                continue
            chunks = file_facts.get("chunks")
            if isinstance(chunks, list):
                file_facts["chunks"] = [
                    _bounded_prompt_text(str(chunk), limit=600)
                    for chunk in chunks[:8]
                ]
            targeted_facts = file_facts.get("targeted_facts")
            if isinstance(targeted_facts, list):
                file_facts["targeted_facts"] = [
                    _bounded_prompt_text(str(fact), limit=600)
                    for fact in targeted_facts[:3]
                ]
        return payload


def _bounded_prompt_text(value: str, *, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    separator = " ... "
    retained = limit - len(separator)
    head = retained * 3 // 4
    tail = retained - head
    return f"{text[:head].rstrip()}{separator}{text[-tail:].lstrip()}"


_DOCUMENT_SUMMARY_TERSE_RE = re.compile(
    r"(?:(?:이\s*)?(?:내용\s*)?(?:요약|정리)(?:해\s*줘|해주세요|해줘요)?|"
    r"내용\s*(?:을\s*)?알려\s*줘)[.!?]?",
)


def is_document_summary_request(question: str) -> bool:
    """Distinguish a whole-document summary from a targeted document lookup."""

    normalized = " ".join(question.casefold().split())
    return bool(_DOCUMENT_SUMMARY_TERSE_RE.fullmatch(normalized)) or (
        any(term in normalized for term in ("요약", "정리"))
        and any(term in normalized for term in ("문서", "pdf", "파일", "업로드"))
    )


def classify_answer_type(
    question: str,
    evidence_sets: Sequence[EvidenceSet],
    *,
    prefer_document: bool = False,
    answer_contract: AnswerContract | None = None,
) -> AnswerType:
    normalized = " ".join(question.casefold().split())
    axes = tuple(
        name
        for name, terms in (
            ("disease", ("환자", "유병률", "상병", "성별", "연령")),
            ("market", ("매출", "총액", "점유율", "sellout", "sell out")),
            ("patent", ("특허", "만료")),
            ("clinical", ("임상", "clinical", "trial", "nct")),
        )
        if any(term in normalized for term in terms)
    )
    document_records = tuple(
        record
        for evidence in evidence_sets
        if evidence.source == "document"
        for record in evidence.records
    )
    if document_records:
        has_vdb_records = any(_is_vdb_record(record) for record in document_records)
        if (
            has_vdb_records
            and answer_contract is not None
            and answer_contract.answer_shape
            in {AnswerShape.DOCUMENT_LIST_EXTRACT, AnswerShape.DOCUMENT_SUMMARY}
        ):
            return "document_summary"
        if has_vdb_records and is_document_summary_request(question):
            return "document_summary"
        non_document_records = any(
            evidence.source != "document" and evidence.records
            for evidence in evidence_sets
        )
        if any(_is_sql_record(record) for record in document_records) and (
            not non_document_records
            or any(
                term in normalized
                for term in (
                    "총액",
                    "합계",
                    "집계",
                    "sellout",
                    "sell out",
                    "매출",
                )
            )
        ):
            return "file_aggregate"
        if has_vdb_records and prefer_document and len(axes) <= 1:
            return "document_summary"
    if (
        answer_contract is not None
        and set(answer_contract.required_sources) == {"nedrug"}
        and any(
            metric in {"허가일", "재심사기간", "변경일"}
            for metric in answer_contract.required_metrics
        )
    ):
        return "general"
    if len(axes) > 1:
        return "mixed"
    if axes:
        return cast(AnswerType, axes[0])
    sources = {evidence.source for evidence in evidence_sets if evidence.records}
    for source, answer_type in (
        ("patent", "patent"),
        ("clinicaltrials", "clinical"),
        ("hira", "disease"),
        ("mart", "market"),
    ):
        if source in sources:
            return cast(AnswerType, answer_type)
    return "general"


def build_fact_digest(
    question: str,
    evidence_sets: Sequence[EvidenceSet],
    rendered: DeterministicRender,
    *,
    prefer_document: bool = False,
    observed_on: date | None = None,
    answer_contract: AnswerContract | None = None,
) -> FactDigest:
    reference_date = observed_on or datetime.now(UTC).date()
    visible_ids = tuple(
        dict.fromkeys(
            record_id for node in rendered.nodes for record_id in node.record_ids
        )
    )
    visible_set = frozenset(visible_ids)
    answer_type = classify_answer_type(
        question,
        evidence_sets,
        prefer_document=prefer_document,
        answer_contract=answer_contract,
    )
    cards = tuple(
        card
        for evidence in evidence_sets
        if (
            card := _card_for_evidence(
                question,
                answer_type,
                evidence,
                visible_set,
                reference_date,
                answer_contract,
            )
        )
        is not None
    )
    derived = build_derived_metrics(
        tuple(card.model_dump(mode="python") for card in cards),
        observed_on=reference_date,
    )
    file_derived = _file_analytics_derived_metrics(cards)
    all_derived = (*derived.metrics, *file_derived)
    return FactDigest(
        question=question,
        answer_type=answer_type,
        answer_contract=answer_contract,
        reference_date=reference_date.isoformat(),
        cards=cards,
        visible_record_ids=visible_ids,
        source_received_counts={
            evidence.source: evidence.coverage.records_received
            for evidence in evidence_sets
        },
        source_visible_counts={
            evidence.source: sum(
                record.evidence_id in visible_set for record in evidence.records
            )
            for evidence in evidence_sets
        },
        visible_tables=tuple(
            node.text for node in rendered.nodes if _contains_markdown_table(node.text)
        ),
        derived_metrics=all_derived,
        derived_metrics_manifest=DerivedMetricManifest(
            generated=tuple(metric.id for metric in all_derived),
            skipped=derived.manifest.skipped,
        ),
    )


def fact_digest_contract_coverage(
    digest: FactDigest,
    *,
    source_states: Mapping[str, str],
) -> ContractSlotCoverage | None:
    """Measure the answer contract against code-owned, surfaced facts only."""
    contract = digest.answer_contract
    if contract is None:
        return None

    effective_source_states = _visible_contract_source_states(digest, source_states)
    visible_cards = tuple(card for card in digest.cards if _card_is_visible(digest, card))

    card_entities = tuple(card.entity for card in visible_cards if card.entity)
    fulfilled_entities = tuple(
        required
        for required in contract.required_entities
        if any(_entity_matches(required, observed) for observed in card_entities)
    )
    fulfilled_metrics = tuple(
        metric
        for metric in contract.required_metrics
        if any(_card_fulfills_metric(card, metric) for card in visible_cards)
    )
    observed_periods = tuple(
        dict.fromkeys(
            period
            for card in visible_cards
            for period in _card_periods(card)
            if period
        )
    )
    fulfilled_periods = tuple(
        required
        for required in contract.required_periods
        if any(_period_matches(required, observed) for observed in observed_periods)
    )
    fulfilled_dimensions = tuple(
        dimension
        for dimension in contract.required_dimensions
        if _digest_fulfills_dimension(digest, dimension, cards=visible_cards)
    )
    return contract_slot_coverage(
        contract,
        source_states=effective_source_states,
        fulfilled_entities=fulfilled_entities,
        fulfilled_metrics=fulfilled_metrics,
        fulfilled_periods=fulfilled_periods,
        fulfilled_dimensions=fulfilled_dimensions,
    )


def period_scope_trace(digest: FactDigest) -> dict[str, Any]:
    """Report when the surfaced mart history cannot cover the requested span."""
    contract = digest.answer_contract
    requested_count = contract.required_period_count if contract is not None else None
    periods = tuple(
        sorted(
            {
                period
                for card in digest.cards
                if card.card_type == "market"
                for period in _card_periods(card)
                if period
            }
        )
    )
    available_count = len(periods)
    fallback = bool(requested_count and available_count < requested_count)
    return {
        "requested_period_count": requested_count,
        "available_period_count": available_count,
        "available_period_from": periods[0] if periods else None,
        "available_period_to": periods[-1] if periods else None,
        "period_fallback": int(fallback),
        "period_fallback_reason": (
            "requested_span_exceeds_available_history" if fallback else "not_applicable"
        ),
    }


_DOCUMENT_SOURCE_ALIASES = frozenset(
    {"document", "document_rag", "document_sql", "file_vdb", "file_sql"}
)


def _canonical_contract_source(source: str) -> str:
    return "document" if source in _DOCUMENT_SOURCE_ALIASES else source


def _visible_contract_source_states(
    digest: FactDigest,
    source_states: Mapping[str, str],
) -> dict[str, str]:
    """Canonicalize execution lanes and require surfaced evidence for success."""
    effective = {
        _canonical_contract_source(source): state
        for source, state in source_states.items()
    }
    visible_by_source: Counter[str] = Counter()
    for source, count in digest.source_visible_counts.items():
        visible_by_source[_canonical_contract_source(source)] += max(0, count)
    for source in set(effective) | set(visible_by_source):
        effective[source] = (
            "EXECUTED_SUCCESS"
            if visible_by_source[source] > 0
            else "EXECUTED_SUCCESS_EMPTY"
        )
    return effective


def _card_is_visible(digest: FactDigest, card: DerivedCoreCard) -> bool:
    if card.visible_count > 0 or bool(card.visible_rows):
        return True
    visible_ids = frozenset(digest.visible_record_ids)
    return bool(visible_ids.intersection(card.evidence_ids))


def _card_fulfills_metric(card: DerivedCoreCard, required_metric: str) -> bool:
    if card.received_count <= 0:
        return False
    metric = (card.metric or "").casefold()
    required = required_metric.casefold()
    if required in metric or metric in required:
        return True
    if required in {"매출", "매출비중"}:
        return card.card_type in {"market", "file_aggregate"}
    if required in {"허가일", "재심사기간", "변경일"}:
        aliases = {
            "허가일": ("ITEM_PERMIT_DATE", "허가일", "permit_date"),
            "재심사기간": ("REEXAM_DATE", "재심사기간", "reexamination"),
            "변경일": ("CHANGE_DATE", "변경일", "change_date"),
        }[required_metric]
        return any(
            any(row.get(key) not in {None, ""} for key in aliases)
            for row in card.visible_rows
        )
    return False


def _entity_matches(required: str, observed: str) -> bool:
    required_key = re.sub(r"\s+", "", required).casefold()
    observed_key = re.sub(r"\s+", "", observed).casefold()
    return bool(required_key and (required_key in observed_key or observed_key in required_key))


def _period_matches(required: str, observed: str) -> bool:
    required_parts = tuple(int(value) for value in re.findall(r"\d+", required))
    observed_parts = tuple(int(value) for value in re.findall(r"\d+", observed))
    return bool(required_parts and required_parts == observed_parts[: len(required_parts)])


def _card_periods(card: DerivedCoreCard) -> tuple[str, ...]:
    periods: list[str] = []
    if card.period:
        periods.append(card.period)
    series = card.full_stats.get("series")
    if isinstance(series, Sequence) and not isinstance(series, (str, bytes)):
        periods.extend(
            str(row.get("period"))
            for row in series
            if isinstance(row, Mapping) and row.get("period")
        )
    query_result = card.file_facts.get("query_result")
    if isinstance(query_result, Mapping):
        query_periods = query_result.get("periods")
        if isinstance(query_periods, Sequence) and not isinstance(
            query_periods, (str, bytes)
        ):
            periods.extend(
                str(row.get("period"))
                for row in query_periods
                if isinstance(row, Mapping) and row.get("period")
            )
    return tuple(dict.fromkeys(periods))


def _digest_fulfills_dimension(
    digest: FactDigest,
    dimension: str,
    *,
    cards: Sequence[DerivedCoreCard] | None = None,
) -> bool:
    contract = digest.answer_contract
    if contract is None:
        return False
    visible_cards = tuple(cards) if cards is not None else digest.cards
    if dimension == "품목":
        return any(card.visible_rows for card in visible_cards)
    if dimension in {"month", "period"}:
        period_count = max((_card_period_count(card) for card in visible_cards), default=0)
        return period_count >= (contract.required_period_count or 1)
    if dimension == "comparison_period":
        return max((_card_period_count(card) for card in visible_cards), default=0) >= 2
    if dimension == "rank":
        required_count = contract.top_k or 1
        return any(
            _file_result_row_count(card, "top_n") >= required_count
            for card in visible_cards
        )
    result_kind_by_dimension = {
        "channel": "group_by",
        "account": "group_by",
        "region": "group_by",
    }
    result_kind = result_kind_by_dimension.get(dimension)
    return bool(
        result_kind
        and any(_file_result_row_count(card, result_kind) > 0 for card in visible_cards)
    )


def _card_period_count(card: DerivedCoreCard) -> int:
    return len(_card_periods(card))


def _file_result_row_count(card: DerivedCoreCard, result_kind: str) -> int:
    query_result = card.file_facts.get("query_result")
    if not isinstance(query_result, Mapping) or query_result.get("kind") != result_kind:
        return 0
    rows = query_result.get("rows")
    return (
        len(rows)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes))
        else 0
    )


def render_core_answer(digest: FactDigest) -> str:
    contract = digest.answer_contract
    missing_required_sources = tuple(
        source
        for source in (contract.required_sources if contract is not None else ())
        if digest.source_received_counts.get(source, 0) <= 0
    )
    renderers = {
        "patent": _render_patent_core,
        "clinical": _render_clinical_core,
        "disease": _render_disease_core,
        "market": lambda card: _render_market_core(card, contract),
        "general": lambda card: _render_nedrug_core(card, contract),
        "file_aggregate": lambda card: _render_file_aggregate_core(card, contract),
        "document_summary": _render_document_summary_core,
    }
    requested_card_types = _requested_card_types(digest.question)
    if digest.answer_type == "mixed":
        sentences: list[str] = []
        ordered_cards = sorted(
            enumerate(digest.cards),
            key=lambda indexed: (
                _axis_position(digest.question, indexed[1].card_type),
                indexed[0],
            ),
        )
        for _, card in ordered_cards:
            if card.card_type not in requested_card_types:
                continue
            renderer = renderers.get(card.card_type)
            if renderer is None:
                continue
            sentences.extend(_sentences(renderer(card))[:2])
        primary = " ".join(sentences)
        rendered = _append_received_source_summaries(
            primary,
            digest,
            renderers,
            primary_cards=tuple(card for _, card in ordered_cards),
        )
        return _finalize_core_prose(
            _prepend_required_source_gap(rendered, missing_required_sources),
            digest.question,
        )
    renderer = renderers.get(digest.answer_type)
    if renderer is None:
        return ""
    card = next(
        (card for card in digest.cards if card.card_type == digest.answer_type),
        None,
    )
    primary = renderer(card) if card is not None else ""
    rendered = _append_received_source_summaries(
        primary,
        digest,
        renderers,
        primary_cards=(card,) if card is not None else (),
    )
    return _finalize_core_prose(
        _prepend_required_source_gap(rendered, missing_required_sources),
        digest.question,
    )


def render_file_analytics_tables(digest: FactDigest) -> tuple[str, ...]:
    """Expose file analytics tables separately from core prose."""

    return tuple(
        "\n".join(
            part
            for part in (
                "## 업로드 파일 분석 표",
                (
                    "판매가(SO)=소비자 판매 단가 · 매입가(SI)=약국 매입 단가 · "
                    "연도 값은 해당 연도 평균"
                    if "판매가(SO)" in table
                    else ""
                ),
                table,
            )
            if part
        )
        for card in digest.cards
        if card.card_type == "file_aggregate"
        and (table := str(card.file_facts.get("analytics_table_markdown") or "").strip())
    )


def render_hira_statistics_tables(digest: FactDigest) -> tuple[str, ...]:
    """Expose HIRA detail tables after prose synthesis for SSE table events."""

    headers = (
        "| 상병코드 | 상병명 | 기간 | 구분 | 환자수 |",
        "| 상병코드 | 상병명 | 기간 | 구분 | 성별 | 연령 | 환자수 |",
    )
    return tuple(
        re.sub(
            r"\s*원천 미제공\s+\d+행은 표에서 제외했습니다\.?",
            "",
            table,
        ).strip()
        for table in digest.visible_tables
        if any(header in table for header in headers)
    )


def _finalize_core_prose(rendered: str, question: str) -> str:
    """Remove question echoes and normalize code-owned numeric prose."""
    normalized = rendered.strip()
    question_text = " ".join(question.split()).strip()
    question_stem = re.sub(r"[.!?]+$", "", question_text).strip()
    for echo in (question_text, question_stem):
        if not echo:
            continue
        normalized = re.sub(
            rf"^{re.escape(echo)}(?:은|는)\s*",
            "",
            normalized,
        )
    normalized = re.sub(r"\b(20\d{2})\s+\1년\b", r"\1년", normalized)
    normalized = re.sub(
        r"(?<![\d,.])(\d{4,})(?=명\b)",
        lambda match: f"{int(match.group(1)):,}",
        normalized,
    )
    return normalized


def _prepend_required_source_gap(
    rendered: str,
    missing_sources: Sequence[str],
) -> str:
    if not missing_sources:
        return rendered
    labels = {
        "mart": "내부 데이터마트",
        "nedrug": "식품의약품안전처 허가정보",
        "hira": "건강보험심사평가원",
        "openfda": "미국 FDA 공개정보",
        "clinicaltrials": "ClinicalTrials.gov",
        "web": "공개 웹",
        "patent": "식약처 의약품 특허목록",
        "document": "업로드 문서",
        "prior_turn": "이전 답변",
    }
    sources = "·".join(labels.get(source, source) for source in missing_sources)
    gap = (
        f"필수 원천인 {sources}의 결과를 수신하지 못해 질문의 직접 답을 "
        "확정하지 못했습니다."
    )
    return " ".join(part for part in (gap, rendered) if part)


def _append_received_source_summaries(
    primary: str,
    digest: FactDigest,
    renderers: Mapping[str, Callable[[DerivedCoreCard], str]],
    *,
    primary_cards: Sequence[DerivedCoreCard],
) -> str:
    """Attach grounded summaries only for value axes requested by the question."""

    sentences = list(_sentences(primary))
    covered_sources = {
        card.source for card in primary_cards if card is not None and primary
    }
    for card in digest.cards:
        if _body_card_relevance_reason(digest, card) == "axis_not_requested":
            continue
        if (
            digest.source_received_counts.get(card.source, 0) <= 0
            and card.received_count <= 0
        ):
            continue
        if card.source in covered_sources:
            continue
        renderer = renderers.get(card.card_type)
        rendered = renderer(card) if renderer is not None else ""
        summary = next(iter(_sentences(rendered)), "")
        if not summary:
            summary = _render_generic_source_core(card)
        if summary:
            sentences.append(summary)
            if card.card_type == "file_aggregate":
                analytics_growth = next(
                    (
                        sentence
                        for sentence in _sentences(rendered)
                        if "연평균" in sentence and "성장" in sentence
                    ),
                    "",
                )
                if analytics_growth and analytics_growth != summary:
                    sentences.append(analytics_growth)
            covered_sources.add(card.source)
    source_labels = {
        "mart": "내부 데이터마트",
        "nedrug": "식품의약품안전처 허가정보",
        "hira": "건강보험심사평가원",
        "openfda": "미국 FDA 공개정보",
        "clinicaltrials": "ClinicalTrials.gov",
        "web": "공개 웹",
        "patent": "식약처 의약품 특허목록",
        "document": "업로드 문서",
        "prior_turn": "이전 답변",
    }
    relevant_sources = {
        card.source
        for card in digest.cards
        if _body_card_relevance_reason(digest, card) != "axis_not_requested"
    }
    for source, received_count in digest.source_received_counts.items():
        if (
            received_count <= 0
            or source in covered_sources
            or source not in relevant_sources
        ):
            continue
        label = source_labels.get(source, source)
        sentences.append(f"{label}에서 질문 관련 데이터를 수신했습니다.")
        covered_sources.add(source)
    return " ".join(sentences)


def body_relevance_trace(digest: FactDigest) -> dict[str, Any]:
    """Explain card-level body inclusion without changing collected material."""

    decisions = [
        {
            "card_index": index,
            "source": card.source,
            "card_type": card.card_type,
            "entity": card.entity,
            "decision": (
                "included"
                if (reason := _body_card_relevance_reason(digest, card))
                != "axis_not_requested"
                else "excluded"
            ),
            "reason": reason,
        }
        for index, card in enumerate(digest.cards)
    ]
    return {
        "policy": "question_requested_value_axis",
        "granularity": "card",
        "requested_card_types": sorted(_requested_card_types(digest.question)),
        "included_card_count": sum(
            decision["decision"] == "included" for decision in decisions
        ),
        "excluded_card_count": sum(
            decision["decision"] == "excluded" for decision in decisions
        ),
        "decisions": decisions,
        "collection_preserved": True,
    }


def _body_card_relevance_reason(
    digest: FactDigest,
    card: DerivedCoreCard,
) -> str:
    if digest.answer_type == "mixed":
        return (
            "question_requested_axis"
            if card.card_type in _requested_card_types(digest.question)
            else "axis_not_requested"
        )
    if card.card_type == digest.answer_type:
        return "primary_answer_axis"
    if card.card_type in _requested_card_types(digest.question):
        return "question_requested_axis"
    return "axis_not_requested"


def _render_generic_source_core(card: DerivedCoreCard) -> str:
    labels = {
        "openfda": "미국 FDA 공개정보",
        "web": "공개 웹",
        "nedrug": "식품의약품안전처",
        "prior_turn": "이전 답변",
    }
    source = labels.get(card.source, card.source)
    item = card.representative
    entity = str(item.get("entity") or card.entity or "질문 관련 항목").strip()
    fact = str(item.get("fact") or item.get("value") or "").strip()
    if fact:
        return f"{source}에서 {entity} 관련 {fact}가 확인됐습니다."
    if entity == "질문 관련 항목":
        return f"{source}에서 관련 데이터를 수신했습니다."
    return f"{source}에서 {entity} 관련 데이터를 수신했습니다."


def _card_for_evidence(
    question: str,
    answer_type: AnswerType,
    evidence: EvidenceSet,
    visible_ids: frozenset[str],
    observed_on: date,
    answer_contract: AnswerContract | None = None,
) -> DerivedCoreCard | None:
    card_type: AnswerType
    if evidence.source == "patent":
        card_type = "patent"
    elif evidence.source == "clinicaltrials":
        card_type = "clinical"
    elif evidence.source == "hira":
        card_type = "disease"
    elif evidence.source == "mart":
        card_type = "market"
    elif evidence.source == "nedrug":
        card_type = "general"
    elif evidence.source == "document":
        if answer_type == "file_aggregate":
            card_type = "file_aggregate"
        elif answer_type == "document_summary":
            card_type = "document_summary"
        elif any(_is_sql_record(record) for record in evidence.records):
            card_type = "file_aggregate"
        elif any(_is_vdb_record(record) for record in evidence.records):
            card_type = "document_summary"
        else:
            return None
    elif evidence.source in {"openfda", "web"}:
        card_type = "general"
    else:
        return None

    received_records = tuple(evidence.records)
    records = received_records
    web_relevance_counts: Counter[str] = Counter()
    if evidence.source == "web":
        decision = filter_web_results(
            question,
            tuple(record.payload for record in received_records),
            identity_values=(
                answer_contract.resolved_entities if answer_contract is not None else ()
            ),
        )
        accepted_ranks = {rank for rank, _item in decision.accepted}
        records = tuple(
            record
            for rank, record in enumerate(received_records, start=1)
            if rank in accepted_ranks
        )
        web_relevance_counts.update(item.reason_code for item in decision.exclusions)
    visible = tuple(record for record in records if record.evidence_id in visible_ids)
    shared = {
        "card_type": card_type,
        "source": evidence.source,
        "evidence_ids": tuple(record.evidence_id for record in records),
        "received_count": evidence.coverage.records_received,
        "matched_count": len(records),
        "visible_count": len(visible),
        "visible_rows": tuple(dict(record.payload) for record in visible[:10]),
    }
    if card_type == "patent":
        return _patent_card(records, shared, observed_on)
    if card_type == "clinical":
        return _clinical_card(
            question,
            records,
            shared,
            observed_on,
            evidence.query_manifest,
        )
    if card_type == "disease":
        return _disease_card(question, records, shared, evidence.query_spec)
    if card_type == "market":
        return _market_card(question, records, shared, answer_contract)
    if card_type == "file_aggregate":
        return _file_aggregate_card(question, records, shared)
    if evidence.source == "nedrug":
        return _nedrug_card(question, records, shared, observed_on)
    if evidence.source in {"openfda", "web", "prior_turn"}:
        first = records[0].payload if records else {}
        entity = _text(
            first,
            "entity",
            "product",
            "brand",
            "title",
            "name",
            "term",
        ) or "질문 관련 항목"
        fact = _text(
            first,
            "fact",
            "summary",
            "status",
            "description",
            "headline",
        )
        return DerivedCoreCard(
            **shared,
            entity=entity,
            metric="공개 근거",
            representative={"entity": entity, "fact": fact},
            full_stats={
                "source_queries": evidence.query_spec,
                "relevance_excluded_count": sum(web_relevance_counts.values()),
                "relevance_excluded_reasons": dict(web_relevance_counts),
            },
        )
    return _document_summary_card(
        question,
        records,
        shared,
        answer_contract=answer_contract,
    )


def _patent_card(
    records: Sequence[EvidenceRecord],
    shared: dict[str, Any],
    observed_on: date,
) -> DerivedCoreCard:
    lane_scoped_records = tuple(
        record for record in records if record.payload.get("lane") == "kr_primary"
    )
    domestic_records = lane_scoped_records or tuple(
        record for record in records if not record.payload.get("lane")
    )
    brand_scope_applied = any(
        record.payload.get("brand_scope_match") in {True, False}
        for record in domestic_records
    )
    scoped_domestic_records = tuple(
        record
        for record in domestic_records
        if not brand_scope_applied or record.payload.get("brand_scope_match") is True
    )
    domestic_product_records = tuple(
        record
        for record in scoped_domestic_records
        if _text(record.payload, "page_group", "PAGE_GB_NM") in {"", "제품특허"}
    )
    product_records = domestic_product_records
    expiry_candidates = tuple(
        record
        for record in product_records
        if _text(record.payload, "patent_no", "DOMESTIC_PATENT_NO")
        and _text(record.payload, "expiration_date", "expiry_date", "EXPRY_DATE")
    )
    registered_expiry_candidates = tuple(
        record
        for record in expiry_candidates
        if _status_rank(
            _text(record.payload, "status", "listed_status", "PATENT_STATUS")
        )
        == 2
    )
    representative_record = max(
        registered_expiry_candidates or expiry_candidates or product_records,
        key=lambda record: (
            _text(record.payload, "expiration_date", "expiry_date", "EXPRY_DATE"),
            _text(record.payload, "patent_no", "DOMESTIC_PATENT_NO"),
        ),
        default=None,
    )
    representative = (
        {
            "product": _text(representative_record.payload, "product", "ITEM_NAME"),
            "patent_no": _text(
                representative_record.payload, "patent_no", "DOMESTIC_PATENT_NO"
            ),
            "status": _text(
                representative_record.payload,
                "status",
                "listed_status",
                "PATENT_STATUS",
            )
            or "원천 미제공",
            "expiration_date": _text(
                representative_record.payload,
                "expiration_date",
                "expiry_date",
                "EXPRY_DATE",
            ),
        }
        if representative_record is not None
        else {}
    )
    combos = {
        (
            _text(record.payload, "patent_no", "DOMESTIC_PATENT_NO"),
            _text(record.payload, "product_item_seq", "ITEM_SEQ"),
        )
        for record in product_records
    }
    patents = {patent_no for patent_no, _item_seq in combos if patent_no}
    statuses = Counter(
        _text(record.payload, "status", "listed_status", "PATENT_STATUS")
        or "원천 미제공"
        for record in product_records
    )
    return DerivedCoreCard(
        **shared,
        entity=representative.get("product") or None,
        metric="특허",
        representative=representative,
        distributions={"status": dict(sorted(statuses.items()))},
        full_stats={
            "product_combination_count": len(combos),
            "patent_number_count": len(patents),
            "product_patent_numbers": sorted(patents),
        },
        temporal_stats=patent_time_axis(product_records, observed_on),
        derived_fields=("status_distribution", "product_combination_count"),
    )


def _clinical_card(
    question: str,
    records: Sequence[EvidenceRecord],
    shared: dict[str, Any],
    observed_on: date,
    query_manifest: Sequence[Mapping[str, Any]] = (),
) -> DerivedCoreCard:
    statuses = Counter(
        _text(record.payload, "overall_status", "status") or "UNKNOWN"
        for record in records
    )
    phases: Counter[str] = Counter()
    queries: Counter[str] = Counter()
    sponsors: Counter[str] = Counter()
    direct_combination_count = 0
    late_phase_count = 0
    for record in records:
        record_phases = _strings(
            record.payload.get("phases") or record.payload.get("phase")
        )
        for phase in record_phases:
            phases[phase] += 1
        if any(_is_late_phase(phase) for phase in record_phases):
            late_phase_count += 1
        for query in _strings(record.payload.get("matched_query")):
            queries[query] += 1
        sponsor = _text(record.payload, "lead_sponsor", "sponsor")
        if sponsor:
            sponsors[sponsor] += 1
        if "직접 관련 확인" in _text(record.payload, "relevance_status"):
            direct_combination_count += 1
    representative_record = max(
        records,
        key=lambda record: (
            _text(record.payload, "last_update_date", "last_update_post_date"),
            _text(record.payload, "nct_id"),
        ),
        default=None,
    )
    representative = (
        {
            "nct_id": _text(representative_record.payload, "nct_id"),
            "title": _text(representative_record.payload, "brief_title", "title"),
            "status": _text(representative_record.payload, "overall_status", "status"),
            "phases": _strings(
                representative_record.payload.get("phases")
                or representative_record.payload.get("phase")
            ),
            "last_update_date": _text(
                representative_record.payload,
                "last_update_date",
                "last_update_post_date",
            ),
            "sponsor": _text(representative_record.payload, "lead_sponsor", "sponsor"),
        }
        if representative_record is not None
        else {}
    )
    ingredient_combination = next(
        (query for query in queries if " and " in query.casefold()),
        next(iter(queries), ""),
    )
    normalized_question = question.casefold()
    explicit_query = next(
        (
            query
            for query in queries
            if query.casefold() in normalized_question and " and " not in query.casefold()
        ),
        "",
    )
    entity = explicit_query or _question_entity(
        question,
        ("클리니컬스", "클리니컬", "임상시험", "임상", "clinical", "trial"),
    )
    completed_count = sum(
        count
        for status, count in statuses.items()
        if status.casefold() in {"completed", "완료"}
    )
    active_count = sum(
        count
        for status, count in statuses.items()
        if status.casefold()
        in {
            "recruiting",
            "not_yet_recruiting",
            "active_not_recruiting",
            "enrolling_by_invitation",
            "진행",
            "모집중",
        }
    )
    split = next(
        (
            manifest
            for manifest in query_manifest
            if manifest.get("lane") == "query_breakdown"
        ),
        {},
    )
    surface_aggregate = next(
        (
            manifest
            for manifest in query_manifest
            if manifest.get("lane") == "surface_full_aggregate"
        ),
        {},
    )
    direct_related_count = int(
        surface_aggregate.get("direct_related_count", direct_combination_count)
    )
    direct_status_counts = dict(
        surface_aggregate.get("direct_status_counts") or statuses
    )
    direct_phase_counts = dict(
        surface_aggregate.get("direct_phase_counts") or phases
    )
    completed_count = int(direct_status_counts.get("COMPLETED", 0))
    active_count = sum(
        int(count)
        for status, count in direct_status_counts.items()
        if status.casefold()
        in {
            "recruiting",
            "not_yet_recruiting",
            "active_not_recruiting",
            "enrolling_by_invitation",
            "진행",
            "모집중",
        }
    )
    return DerivedCoreCard(
        **shared,
        entity=entity or None,
        metric="임상시험",
        representative=representative,
        distributions={
            "status": dict(sorted(direct_status_counts.items())),
            "phase": dict(sorted(direct_phase_counts.items())),
            "query": dict(sorted(queries.items())),
        },
        full_stats={
            "query_count": len(queries),
            "direct_combination_count": direct_combination_count,
            "direct_related_count": direct_related_count,
            "direct_status_counts": direct_status_counts,
            "direct_phase_counts": direct_phase_counts,
            "late_phase_count": late_phase_count,
            "late_phase_ratio_pct": round(
                late_phase_count / max(int(shared["received_count"]), 1) * 100,
                2,
            ),
            "completed_count": completed_count,
            "active_count": active_count,
            "ingredient_combination": ingredient_combination,
            "top_sponsors": tuple(
                sorted(sponsors.items(), key=lambda item: (-item[1], item[0]))[:2]
            ),
            "query_breakdown": dict(split) if split else {},
        },
        temporal_stats=clinical_time_axis(records, observed_on),
        derived_fields=(
            "status_distribution",
            "phase_distribution",
            "query_distribution",
        ),
    )


def _disease_card(
    question: str,
    records: Sequence[EvidenceRecord],
    shared: dict[str, Any],
    query_spec: Sequence[str],
) -> DerivedCoreCard:
    requested_axes = requested_hira_axes(question)
    candidates = [record for record in records if _number(record.payload) is not None]
    primary_candidates = _question_disease_records(question, candidates, query_spec)
    mapping = hira_disease_mapping(question)
    scoped_records = (
        primary_candidates
        if mapping is not None
        else primary_candidates or candidates
    )
    summary_payload, summary_value, summary_method = hira_summary_payload(
        tuple(record.payload for record in scoped_records)
    )
    representative_record = next(
        (
            record
            for record in scoped_records
            if summary_payload is not None
            and (record.payload is summary_payload or record.payload == summary_payload)
        ),
        None,
    )
    payload = representative_record.payload if representative_record else {}
    codes = sorted(
        {
            _text(record.payload, "sickCd", "sick_cd", "disease_code", "code")
            for record in scoped_records
            if _text(record.payload, "sickCd", "sick_cd", "disease_code", "code")
        }
    )
    entity = (
        mapping.canonical_name
        if mapping is not None
        else _question_entity(question, ("환자수", "환자 수", "유병률"))
        or _text(payload, "sickNm", "sick_nm", "disease_name")
    )
    representative_period = _period(payload)
    representative_code = _text(
        payload,
        "sickCd",
        "sick_cd",
        "disease_code",
        "code",
    )
    representative_scope = tuple(
        record
        for record in scoped_records
        if _period(record.payload) == representative_period
        and _text(
            record.payload,
            "sickCd",
            "sick_cd",
            "disease_code",
            "code",
        )
        == representative_code
    )
    structure_records = representative_scope or tuple(scoped_records)
    care_representatives = (
        _dimension_representatives(structure_records, _normalized_care)
        if "patient_type" in requested_axes
        else {}
    )
    sex_representatives = (
        _dimension_representatives(structure_records, _normalized_sex)
        if "sex" in requested_axes
        else {}
    )
    ratio = (
        _matched_care_ratio(structure_records)
        if "patient_type" in requested_axes
        else None
    )
    code_representatives = {
        code: (
            _disease_representative_payload(code_records)
            if requested_axes
            else _disease_total_representative_payload(code_records)
        )
        for code in codes
        if (
            code_records := tuple(
                record
                for record in scoped_records
                if _text(
                    record.payload,
                    "sickCd",
                    "sick_cd",
                    "disease_code",
                    "code",
                )
                == code
            )
        )
    }
    available_periods = sorted(
        {_period(record.payload) for record in scoped_records if _period(record.payload)}
    )
    requested_year_match = re.search(r"\b(20\d{2})\s*년", question)
    requested_year = requested_year_match.group(1) if requested_year_match else ""
    scope_notices: list[str] = []
    if any(term in question for term in ("주요 국가", "국가별", "국가 비교", "국가의")):
        scope_notices.append("보유 HIRA 원천은 한국 단일 국가 자료입니다.")
    if requested_year and available_periods and requested_year not in available_periods:
        scope_notices.append(
            f"요청한 {requested_year}년 대신 최신 가용 {available_periods[-1]}년 기준으로 조정했습니다."
        )
    observed_parent_codes = tuple(
        dict.fromkeys(code.replace(".", "").replace("_", "")[:3] for code in codes)
    )
    parent_codes = catalog_parent_codes_for_name(entity or "") or observed_parent_codes
    population = select_catalog_population(question, parent_codes)
    layer = requested_population_layer(question)
    scope_notices.append(
        f"{'세분류' if layer == 'subcode' else '부모 코드'} 기준 · "
        f"카탈로그 스냅샷 {population.metadata.snapshot_date} 기준"
    )
    provided_subcodes = tuple(
        code
        for code in codes
        if code.replace(".", "").replace("_", "") in population.resolution.child_codes
    )
    if layer == "subcode":
        scope_notices.append(
            f"세분류 {len(population.resolution.child_codes)}개 중 "
            f"{len(provided_subcodes)}개 통계 제공"
        )
    scoped_shared = {
        **shared,
        "evidence_ids": tuple(record.evidence_id for record in scoped_records),
        "matched_count": len(scoped_records),
        "visible_rows": tuple(dict(record.payload) for record in scoped_records[:10]),
    }
    metric_scopes = _hira_metric_scopes(scoped_records) if requested_axes else ()
    distributions = {}
    if "sex" in requested_axes:
        distributions["sex"] = _count_values(
            scoped_records,
            ("sex", "gender", "sexCd"),
        )
    if "age" in requested_axes:
        distributions["age"] = _count_values(
            scoped_records,
            ("age", "age_group", "ageCd"),
        )
    derived_fields = ["code_count"]
    if "patient_type" in requested_axes:
        derived_fields.extend(("care_representatives", "outpatient_inpatient_ratio"))
    if "sex" in requested_axes:
        derived_fields.append("sex_representatives")
    return DerivedCoreCard(
        **scoped_shared,
        entity=entity or None,
        metric="유병률" if "유병률" in question else "환자수",
        period=_period(payload) or None,
        unit="%" if "유병률" in question else "명",
        representative={
            "code": _text(payload, "sickCd", "sick_cd", "disease_code", "code"),
            "value": summary_value,
            "dimension": "전체" if summary_method == "aggregate" else _dimension(payload),
            "is_total": summary_method == "aggregate",
        },
        distributions=distributions,
        full_stats={
            "codes": codes,
            "code_count": len(codes),
            "code_representatives": code_representatives,
            "care_representatives": care_representatives,
            "sex_representatives": sex_representatives,
            "outpatient_inpatient_ratio": ratio,
            "summary_method": summary_method,
            "reconciliation": hira_row_reconciliation(
                tuple(record.payload for record in scoped_records)
            ),
            "metric_scopes": metric_scopes,
            "available_periods": available_periods,
            "scope_notices": scope_notices,
            "population_layer": layer,
            "catalog_snapshot": {
                "fetched_at": population.metadata.fetched_at,
                "total_count": population.metadata.total_count,
                "page_count": population.metadata.page_count,
            },
            "subcode_coverage": {"expected": len(population.resolution.child_codes), "provided": len(provided_subcodes)},
            "requested_axes": requested_axes,
            "unrequested_axis_material_suppressed": not requested_axes,
        },
        derived_fields=tuple(derived_fields),
    )


def _hira_metric_scopes(
    records: Sequence[EvidenceRecord],
) -> tuple[dict[str, Any], ...]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records:
        payload = record.payload
        period = _period(payload)
        code = _text(payload, "sickCd", "sick_cd", "disease_code", "code")
        source_tool = _text(payload, "_source_tool", "source_tool")
        gender = _text(payload, "sex", "gender", "sexCd")
        age = _text(payload, "age", "age_group", "ageCd")
        value = _number(payload)
        if not period or not code or not gender or not age or value is None:
            continue
        scope = grouped.setdefault(
            (period, code, source_tool),
            {
                "period": period,
                "code": code,
                "source_tool": source_tool,
                "gender_totals": {},
                "age_totals": {},
                "evidence_ids": [],
            },
        )
        gender_totals = scope["gender_totals"]
        age_totals = scope["age_totals"]
        gender_totals[gender] = gender_totals.get(gender, 0) + value
        age_totals[age] = age_totals.get(age, 0) + value
        scope["evidence_ids"].append(record.evidence_id)
    return tuple(
        {
            **scope,
            "gender_totals": dict(sorted(scope["gender_totals"].items())),
            "age_totals": dict(sorted(scope["age_totals"].items())),
            "evidence_ids": tuple(dict.fromkeys(scope["evidence_ids"])),
        }
        for _key, scope in sorted(grouped.items())
    )


def _nedrug_card(
    question: str,
    records: Sequence[EvidenceRecord],
    shared: dict[str, Any],
    observed_on: date,
) -> DerivedCoreCard:
    representative_record = max(
        records,
        key=lambda record: (
            _text(record.payload, "approval_date", "ITEM_PERMIT_DATE"),
            record.evidence_id,
        ),
        default=None,
    )
    payload = representative_record.payload if representative_record else {}
    return DerivedCoreCard(
        **shared,
        entity=(
            _text(payload, "item_name", "ITEM_NAME")
            or _question_entity(question, ("허가", "의약품"))
            or None
        ),
        metric="허가·재심사",
        representative={
            "approval_date": _text(payload, "approval_date", "ITEM_PERMIT_DATE"),
            "company": _text(payload, "company", "ENTP_NAME"),
        },
        temporal_stats=nedrug_time_axis(records, observed_on),
        derived_fields=("approval_elapsed_years", "reexamination_remaining_months"),
    )


def _question_disease_records(
    question: str,
    records: Sequence[EvidenceRecord],
    query_spec: Sequence[str],
) -> list[EvidenceRecord]:
    mapping = hira_disease_mapping(question)
    if mapping is not None:
        return [
            record
            for record in records
            if hira_record_matches_question(question, record.payload)
        ]
    explicit_codes = tuple(
        dict.fromkeys(re.findall(r"\b[A-Z]\d{2}(?:\.\d+)?\b", question.upper()))
    )
    if explicit_codes:
        matches = [
            record
            for record in records
            if _text(
                record.payload,
                "sickCd",
                "sick_cd",
                "disease_code",
                "code",
            )
            in explicit_codes
        ]
        if matches:
            return matches

    stopwords = {
        "환자수",
        "환자",
        "유병률",
        "알려줘",
        "알려주세요",
        "현황",
        "국내",
    }
    question_terms = tuple(
        term
        for term in re.findall(r"[가-힣A-Za-z0-9]+", question)
        if len(term) >= 2 and term not in stopwords
    )
    scored_name_matches = [
        (score, record)
        for record in records
        if (
            score := max(
                (
                    len(term)
                    for term in question_terms
                    if term.casefold()
                    in _text(
                        record.payload,
                        "sickNm",
                        "sick_nm",
                        "disease_name",
                    ).casefold()
                ),
                default=0,
            )
        )
    ]
    best_name_score = max(
        (score for score, _record in scored_name_matches),
        default=0,
    )
    name_matches = [
        record
        for score, record in scored_name_matches
        if score == best_name_score
    ]
    if name_matches:
        return name_matches

    query_code = next(
        (
            match.group(0)
            for query in query_spec
            if (match := re.search(r"\b[A-Z]\d{2}(?:\.\d+)?\b", query.upper()))
        ),
        "",
    )
    if not query_code:
        return []
    return [
        record
        for record in records
        if _text(
            record.payload,
            "sickCd",
            "sick_cd",
            "disease_code",
            "code",
        )
        == query_code
    ]


def _disease_representative_payload(
    records: Sequence[EvidenceRecord],
) -> dict[str, Any]:
    record = max(
        records,
        key=lambda candidate: (
            _period(candidate.payload),
            _is_total_row(candidate.payload),
            _number(candidate.payload) or 0,
        ),
        default=None,
    )
    if record is None:
        return {}
    return {
        "value": _number(record.payload),
        "period": _period(record.payload),
        "dimension": _dimension(record.payload),
        "is_total": _is_total_row(record.payload),
        "disease_name": _text(
            record.payload,
            "sickNm",
            "sick_nm",
            "disease_name",
        ),
        "evidence_id": record.evidence_id,
    }


def _disease_total_representative_payload(
    records: Sequence[EvidenceRecord],
) -> dict[str, Any]:
    payload, value, method = hira_summary_payload(
        tuple(record.payload for record in records)
    )
    if payload is None:
        return {}
    evidence_id = next(
        (
            record.evidence_id
            for record in records
            if record.payload is payload or record.payload == payload
        ),
        "",
    )
    return {
        "value": value,
        "period": _period(payload),
        "dimension": "전체",
        "is_total": method in {"aggregate", "detail_sum"},
        "disease_name": _text(
            payload,
            "sickNm",
            "sick_nm",
            "disease_name",
        ),
        "evidence_id": evidence_id,
        "summary_method": method,
    }


def _market_card(
    question: str,
    records: Sequence[EvidenceRecord],
    shared: dict[str, Any],
    answer_contract: AnswerContract | None = None,
) -> DerivedCoreCard:
    metric = "점유율" if "점유율" in question else "매출"
    candidates = [
        record
        for record in records
        if _market_value(record.payload, metric) is not None
    ]
    requested_period = _question_period(question)
    exact_period = tuple(
        record for record in candidates if _period(record.payload) == requested_period
    )
    representative_record = max(
        exact_period or candidates,
        key=lambda record: (_period(record.payload), record.evidence_id),
        default=None,
    )
    payload = representative_record.payload if representative_record else {}
    entity = _text(payload, "brand", "brand_name", "product", "entity") or None
    relation_inputs = _market_relation_inputs(
        records,
        entity=entity,
        period_count=(
            answer_contract.required_period_count
            if answer_contract is not None
            else None
        ),
    )
    if entity is None:
        entity = _text(relation_inputs, "brand") or None
    series = _market_series(
        candidates,
        metric=metric,
        entity=entity,
        source=(
            _text(payload, "data_source", "source", "dataset")
            or "내부 데이터마트"
        ),
    )
    source_periods = tuple(
        sorted(
            {
                _period(record.payload)
                for record in candidates
                if _period(record.payload)
            }
        )
    )
    scope_notices: tuple[str, ...] = ()
    if (
        metric == "매출"
        and source_periods
        and series
        and source_periods[0] != series[0]["period"]
    ):
        scope_notices = (
            f"0과 결측을 구분해 매출 데이터 시작 {series[0]['period']}부터 표시했습니다.",
        )
    return DerivedCoreCard(
        **shared,
        entity=entity,
        metric=metric,
        period=_period(payload) or None,
        unit="%" if metric == "점유율" else "억원",
        representative={
            "value": _market_value(payload, metric),
            "source": _text(payload, "data_source", "source", "dataset")
            or "내부 데이터마트",
        },
        full_stats={
            "periods": sorted(
                {
                    _period(record.payload)
                    for record in records
                    if _period(record.payload)
                }
            ),
            "series": series,
            "scope_notices": scope_notices,
            "relation_inputs": relation_inputs,
        },
        derived_fields=("latest_period",),
    )


def _market_series(
    records: Sequence[EvidenceRecord],
    *,
    metric: str,
    entity: str | None,
    source: str,
) -> tuple[dict[str, Any], ...]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for record in sorted(
        records,
        key=lambda candidate: (_period(candidate.payload), candidate.evidence_id),
    ):
        payload = record.payload
        period = _period(payload)
        value = _market_value(payload, metric)
        if not period or value is None:
            continue
        record_entity = _text(payload, "brand", "brand_name", "product", "entity")
        if entity and record_entity and record_entity.casefold() != entity.casefold():
            continue
        record_source = (
            _text(payload, "data_source", "source", "dataset")
            or "내부 데이터마트"
        )
        if record_source.casefold() != source.casefold():
            continue
        measure = _text(payload, "measure", "metric").casefold()
        if metric == "점유율" and measure in {
            "prescription_volume",
            "prescription volume",
            "volume",
            "처방량",
        }:
            continue
        series_id = "|".join((record_source, entity or record_entity, metric))
        rows[(series_id, period)] = {
            "series": series_id,
            "period": period,
            "value": value,
            "source": record_source,
        }
    ordered = tuple(rows[key] for key in sorted(rows, key=lambda item: (item[1], item[0])))
    return _trim_leading_unavailable_sales(ordered) if metric == "매출" else ordered


def _trim_leading_unavailable_sales(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Drop only a leading zero run before the first observed non-zero sale."""
    first_observed = next(
        (
            index
            for index, row in enumerate(rows)
            if isinstance(row.get("value"), (int, float)) and row.get("value") != 0
        ),
        None,
    )
    if first_observed in (None, 0):
        return tuple(dict(row) for row in rows)
    return tuple(dict(row) for row in rows[first_observed:])


def _market_relation_inputs(
    records: Sequence[EvidenceRecord],
    *,
    entity: str | None,
    period_count: int | None = None,
) -> dict[str, Any]:
    sales_records = tuple(
        record
        for record in records
        if _period(record.payload)
        and _text(record.payload, "measure", "metric").casefold()
        in {"sales", "sale", "revenue", "매출"}
        and _market_value(record.payload, "매출") is not None
    )
    if entity is None:
        brand_counts = Counter(
            _text(record.payload, "brand", "brand_name", "product", "entity")
            for record in sales_records
        )
        brand_counts.pop("", None)
        entity = min(
            brand_counts,
            key=lambda brand: (-brand_counts[brand], brand),
            default="",
        )
    matching = tuple(
        record
        for record in sales_records
        if _text(record.payload, "brand", "brand_name", "product", "entity")
        == entity
    )
    by_period: dict[str, EvidenceRecord] = {}
    for record in matching:
        by_period.setdefault(_period(record.payload), record)
    period_limit = period_count or 12
    recent = tuple(by_period[period] for period in sorted(by_period)[-period_limit:])
    brand_series = tuple(
        {
            "period": _period(record.payload),
            "value_억원": _market_value(record.payload, "매출"),
            "ms_pct": _first_number(
                record.payload,
                ("ms_pct", "share", "market_share", "share_pct", "점유율"),
            ),
            "rank": _first_number(record.payload, ("rank", "ranking")),
            "evidence_id": record.evidence_id,
        }
        for record in recent
    )
    market_series = tuple(
        {
            "period": point["period"],
            "value_억원": float(
                (
                    Decimal(str(point["value_억원"]))
                    / Decimal(str(point["ms_pct"]))
                    * Decimal(100)
                ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
            ),
            "evidence_id": point["evidence_id"],
        }
        for point in brand_series
        if point["ms_pct"] not in (None, 0)
    )
    rich_record = next(
        (
            record
            for record in records
            if isinstance(record.payload.get("level_top5_trend_series"), Sequence)
            and not isinstance(
                record.payload.get("level_top5_trend_series"), (str, bytes)
            )
        ),
        None,
    )
    competitors: tuple[dict[str, Any], ...] = ()
    market_name = "시장 전체"
    if rich_record is not None:
        competitors = tuple(
            {
                **dict(competitor),
                "series": tuple(
                    {**dict(point), "evidence_id": rich_record.evidence_id}
                    for point in competitor.get("series") or ()
                    if isinstance(point, Mapping)
                ),
            }
            for competitor in rich_record.payload.get("level_top5_trend_series") or ()
            if isinstance(competitor, Mapping)
        )
    return {
        "brand": entity or "",
        "metric": "sales",
        "market_name": market_name,
        "brand_value_series_10pt": brand_series,
        "market_size_series": market_series,
        "level_top5_trend_series": competitors,
    }


def _file_aggregate_card(
    question: str,
    records: Sequence[EvidenceRecord],
    shared: dict[str, Any],
) -> DerivedCoreCard:
    record = next((record for record in records if _is_sql_record(record)), None)
    payload = record.payload if record else {}
    trace = _mapping(payload.get("sql_detail"))
    if not trace:
        trace = _mapping(payload.get("sql_trace"))
    if not trace:
        trace = _mapping(payload.get("detail"))
    analytics_response = _mapping(trace.get("analytics_response"))
    if analytics_response:
        return _file_analytics_card(payload, trace, analytics_response, shared)
    analytics_answer = str(payload.get("deterministic_answer") or "").strip()
    analytics_schema = _mapping(trace.get("analytics_schema"))
    unsupported_dimension = (
        str(trace.get("query_route") or "") == "unsupported"
        and str(trace.get("query_route_reason") or "")
        == "capability_dimension_missing"
    )
    if unsupported_dimension and not analytics_schema:
        capabilities = _mapping(trace.get("capabilities"))
        analytics_schema = _mapping(capabilities.get("capabilities"))
    if analytics_answer and (analytics_schema or unsupported_dimension):
        available_dimensions = trace.get("available_dimensions") or analytics_schema.get(
            "dimensions"
        )
        return DerivedCoreCard(
            **shared,
            entity=(
                _plain_file_slot(_raw_text(payload, "document_name", "file_name"))
                or None
            ),
            metric="분석 차원 안내",
            file_facts={
                "document_name": _plain_file_slot(
                    _raw_text(payload, "document_name", "file_name")
                ),
                "sheet_name": _plain_file_slot(
                    _raw_text(payload, "sheet_name", "sheet")
                ),
                "analytics_answer": analytics_answer,
                "analytics_schema": dict(analytics_schema),
                "available_dimensions": tuple(
                    str(value) for value in available_dimensions or ()
                ),
                "query_result": {"kind": "file_analytics", "rows": ()},
            },
        )
    aggregate_values = _mapping(trace.get("aggregate_values"))
    query_result = _file_query_result(trace)
    applied_row_count = query_result.get("applied_rows")
    if not isinstance(applied_row_count, (int, float)):
        applied_row_count = aggregate_values.get("applied_rows")
    if not isinstance(applied_row_count, (int, float)):
        columns = tuple(str(value) for value in trace.get("columns") or ())
        rows = tuple(trace.get("rows") or ())
        try:
            applied_index = tuple(value.casefold() for value in columns).index(
                "applied_rows"
            )
        except ValueError:
            applied_index = -1
        if applied_index >= 0 and rows and isinstance(rows[0], (list, tuple)):
            first_row = rows[0]
            if applied_index < len(first_row) and isinstance(
                first_row[applied_index], (int, float)
            ):
                applied_row_count = first_row[applied_index]
    period = (
        _text(payload, "period", "requested_period")
        or _question_period_display(question)
    )
    sheet_name = _plain_file_slot(_raw_text(payload, "sheet_name", "sheet"))
    auxiliary_aggregates = _file_auxiliary_aggregates(trace)
    result_kind = str(query_result.get("kind") or "single_value")
    derived_metrics = (
        _file_derived_metrics(aggregate_values, applied_row_count)
        if result_kind == "single_value"
        else {}
    )
    derived_metrics.update(_file_result_derived_metrics(query_result))
    representative_value = aggregate_values.get("total_value")
    if result_kind in {"period_comparison", "time_series"}:
        periods = tuple(query_result.get("periods") or ())
        if periods and isinstance(periods[-1], Mapping):
            representative_value = periods[-1].get("value")
    elif result_kind in {"top_n", "group_by"}:
        result_rows = tuple(query_result.get("rows") or ())
        if result_rows and isinstance(result_rows[0], Mapping):
            representative_value = result_rows[0].get("value")
    return DerivedCoreCard(
        **shared,
        entity=_plain_file_slot(_raw_text(payload, "document_name", "file_name"))
        or None,
        metric={
            "period_comparison": "기간 비교",
            "time_series": "월별 추이",
            "top_n": "상위 순위",
            "group_by": "구성 분포",
        }.get(result_kind, "총액"),
        period=period or None,
        unit="원",
        representative={
            "value": representative_value,
            "applied_rows": applied_row_count,
            "sheet_name": sheet_name,
            "period": period,
        },
        file_facts={
            "document_name": _plain_file_slot(
                _raw_text(payload, "document_name", "file_name")
            ),
            "sheet_name": sheet_name,
            "period": period,
            "executed_sql": _text(trace, "executed_sql", "sql"),
            "total_row_count": trace.get("total_row_count"),
            "applied_row_count": applied_row_count,
            "aggregate_values": aggregate_values,
            "query_result": query_result,
            "auxiliary_aggregates": auxiliary_aggregates,
            "derived_metrics": derived_metrics,
        },
        derived_fields=tuple(derived_metrics),
    )


_FILE_ANALYTICS_DM_TYPES: dict[str, str] = {
    "category_sales": "volume",
    "category_growth": "yearly_growth",
    "category_cagr": "cagr",
    "brand_market_share": "ms_share",
    "sell_in_price": "sellin_price",
    "sell_out_price": "sellout_price",
    "brand_growth": "yearly_growth",
    "brand_cagr": "cagr",
    "sales": "volume",
    "units": "volume",
    "sales_yoy": "yearly_growth",
    "units_yoy": "yearly_growth",
}


def _file_analytics_card(
    payload: Mapping[str, Any],
    trace: Mapping[str, Any],
    response: Mapping[str, Any],
    shared: dict[str, Any],
) -> DerivedCoreCard:
    operation = str(response.get("operation") or "")
    rows = tuple(
        row
        for row in (trace.get("rows") or response.get("rows") or ())
        if isinstance(row, list)
    )
    years = tuple(year for year in response.get("years") or () if isinstance(year, int))
    period = str(trace.get("period") or "")
    latest_rows = (
        tuple(
            row
            for row in rows
            if operation != "category_overview"
            or (len(row) > 1 and row[1] == years[-1])
        )
        if years
        else rows
    )
    metrics = _file_analytics_surface_metrics(
        response.get("metrics") or (), latest_rows
    )
    analytics_answer = str(payload.get("deterministic_answer") or "").strip()
    analytics_table_markdown = str(
        payload.get("analytics_table_markdown")
        or trace.get("analytics_table_markdown")
        or ""
    ).strip() or _file_analytics_table(operation, rows)
    query_rows = tuple(
        {
            "rank": index,
            "label": str(row[0]) if row else "",
            "value": (
                row[2]
                if operation == "category_overview" and len(row) > 2
                else next(
                    (
                        value
                        for value in reversed(row)
                        if isinstance(value, (int, float))
                    ),
                    None,
                )
                if operation == "query_ir"
                else row[3]
            ),
            "composition_pct": (
                row[4]
                if operation == "category_overview" and len(row) > 4
                else None
            ),
        }
        for index, row in enumerate(latest_rows, 1)
        if len(row) >= (1 if operation == "query_ir" else 4)
    )
    representative = query_rows[0] if query_rows else {}
    analytics_shared = {
        **shared,
        "visible_rows": tuple(
            {str(index): value for index, value in enumerate(row)} for row in rows
        ),
    }
    return DerivedCoreCard(
        **analytics_shared,
        entity=(
            _plain_file_slot(_raw_text(payload, "document_name", "file_name"))
            or None
        ),
        metric=_file_analytics_metric_label(operation, trace, years),
        period=period or None,
        unit="원",
        representative={**representative, "period": period},
        file_facts={
            "document_name": _plain_file_slot(
                _raw_text(payload, "document_name", "file_name")
            ),
            "sheet_name": _plain_file_slot(_raw_text(payload, "sheet_name", "sheet")),
            "period": period,
            "analytics_operation": operation,
            "analytics_request": dict(_mapping(trace.get("analytics_request"))),
            "analytics_answer": analytics_answer,
            "analytics_table_markdown": analytics_table_markdown,
            "analytics_columns": tuple(
                str(column) for column in response.get("columns") or ()
            ),
            "analytics_metrics": metrics,
            "analytics_rows": rows,
            "brand_candidate_filter": dict(
                _mapping(trace.get("brand_candidate_filter"))
            ),
            "query_result": {
                "kind": "file_analytics",
                "rows": query_rows,
                "applied_rows": len(rows),
            },
        },
        derived_fields=tuple(
            dict.fromkeys(
                _FILE_ANALYTICS_DM_TYPES[str(metric.get("name"))]
                for metric in metrics
                if str(metric.get("name")) in _FILE_ANALYTICS_DM_TYPES
            )
        ),
    )


def _file_analytics_metric_label(
    operation: str,
    trace: Mapping[str, Any],
    years: tuple[int, ...],
) -> str:
    request = _mapping(trace.get("analytics_request"))
    target = str(request.get("dimension_value") or "").strip()
    latest = str(years[-1]) if years else ""
    if operation == "category_overview":
        return " ".join(part for part in (target, latest, "선두 브랜드 매출") if part)
    if operation in {"brand_monthly", "brand_monthly_yoy"}:
        brand = str(request.get("brand_value") or "").strip()
        period = str(
            request.get("target_period")
            or next(iter(request.get("periods") or ()), "")
        )
        return " ".join(part for part in (brand, period, "파일 매출·수량") if part)
    return " ".join(part for part in (target, "성장 순위") if part)


def _file_analytics_surface_metrics(
    raw_metrics: Sequence[Any],
    visible_rows: Sequence[Sequence[Any]],
) -> tuple[dict[str, Any], ...]:
    aggregate_names = {"category_sales", "category_growth", "category_cagr"}
    brand_names = {
        "brand_market_share",
        "sell_in_price",
        "sell_out_price",
        "brand_growth",
        "brand_cagr",
        "sales",
        "units",
        "sales_yoy",
        "units_yoy",
    }
    visible_labels = {
        str(row[0]).strip()
        for row in visible_rows[:10]
        if row and str(row[0]).strip()
    }
    selected: list[dict[str, Any]] = []
    for raw_metric in raw_metrics:
        if not isinstance(raw_metric, Mapping):
            continue
        name = str(raw_metric.get("name") or "")
        period = str(raw_metric.get("period") or "")
        period_label = period.rsplit(":", 1)[-1].strip() if ":" in period else ""
        if name not in aggregate_names and not (
            name in brand_names
            and (not period_label or period_label in visible_labels)
        ):
            continue
        inputs = tuple(
            {"name": str(item.get("name"))}
            for item in raw_metric.get("inputs") or ()
            if isinstance(item, Mapping) and item.get("name")
        )
        selected.append(
            {
                "name": name,
                "value": raw_metric.get("value"),
                "unit": str(raw_metric.get("unit") or ""),
                "period": period,
                "formula": str(raw_metric.get("formula") or ""),
                "inputs": inputs,
            }
        )
    return tuple(selected)


def _file_analytics_table(
    operation: str,
    rows: Sequence[Sequence[Any]],
) -> str:
    if operation == "category_overview":
        headers = (
            "브랜드",
            "연도",
            "매출",
            "수량",
            "M/S",
            "판매가(SO)",
            "매입가(SI)",
            "YoY 성장률(%)",
            "CAGR(%)",
        )
    elif operation in {"brand_monthly", "brand_monthly_yoy"}:
        headers = (
            "브랜드",
            "대상 기간",
            "비교 기간",
            "매출",
            "전년 동월 매출",
            "매출 YoY(%)",
            "수량",
            "전년 동월 수량",
            "수량 YoY(%)",
        )
    else:
        headers = ("카테고리", "시작 매출", "종료 매출", "CAGR")
    formatted_rows = tuple(
        tuple(
            _file_analytics_cell(value, headers[index])
            for index, value in enumerate(row[: len(headers)])
        )
        for row in rows
    )
    header = "| " + " | ".join(headers) + " |"
    divider = "| " + " | ".join("---" for _ in headers) + " |"
    body = tuple("| " + " | ".join(row) + " |" for row in formatted_rows)
    return "\n".join((header, divider, *body))


def _file_analytics_cell(value: Any, header: str) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, str):
        return value.replace("|", "\\|").replace("\n", " ").strip()
    if header == "연도" and isinstance(value, (int, float)):
        return str(int(value))
    formatted = _format_number(value)
    if header in {
        "M/S",
        "CAGR",
        "YoY 성장률(%)",
        "CAGR(%)",
        "매출 YoY(%)",
        "수량 YoY(%)",
    }:
        return f"{formatted}%"
    if header in {
        "매출",
        "시작 매출",
        "종료 매출",
        "SO가",
        "SI가",
        "판매가(SO)",
        "매입가(SI)",
        "전년 동월 매출",
    }:
        return f"{formatted}원"
    return formatted


def _file_analytics_derived_metrics(
    cards: Sequence[DerivedCoreCard],
) -> tuple[DerivedMetricCard, ...]:
    derived: list[DerivedMetricCard] = []
    for card_index, card in enumerate(cards):
        raw_metrics = card.file_facts.get("analytics_metrics")
        if not isinstance(raw_metrics, Sequence) or isinstance(
            raw_metrics, (str, bytes)
        ):
            continue
        for metric_index, raw_metric in enumerate(raw_metrics):
            if not isinstance(raw_metric, Mapping):
                continue
            metric_name = str(raw_metric.get("name") or "")
            metric_type = _FILE_ANALYTICS_DM_TYPES.get(metric_name)
            value = raw_metric.get("value")
            if metric_type is None or not isinstance(value, (int, float, str)):
                continue
            raw_inputs = raw_metric.get("inputs")
            inputs = tuple(
                dict.fromkeys(
                    str(item.get("name"))
                    for item in raw_inputs or ()
                    if isinstance(item, Mapping) and item.get("name")
                )
            )
            period = str(raw_metric.get("period") or "") or None
            entity = card.entity or "업로드 파일"
            if metric_name in {"brand_growth", "brand_cagr"} and period and ":" in period:
                period, entity = (part.strip() for part in period.rsplit(":", 1))
            safe_period = re.sub(
                r"[^0-9a-z]+", "_", (period or "all").casefold()
            ).strip("_")
            derived.append(
                DerivedMetricCard(
                    id=f"dm_file_{card_index}_{metric_index}_{metric_type}_{safe_period}",
                    type=metric_type,
                    entity=entity,
                    value=value,
                    unit=str(raw_metric.get("unit") or ""),
                    period=period,
                    inputs=inputs,
                    formula=str(raw_metric.get("formula") or ""),
                )
            )
    return tuple(derived)


def _file_auxiliary_aggregates(trace: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw = trace.get("auxiliary_aggregates")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(dict(item) for item in raw if isinstance(item, Mapping))


_FILE_PERIOD_RESULT_RE = re.compile(r"period_(20\d{2})_(0[1-9]|1[0-2])$")
_FILE_VALUE_COLUMN_RE = re.compile(
    r"(?:total|sum|value|amount|sales|revenue|count|avg|average|maximum)",
    re.IGNORECASE,
)


def _file_query_result(trace: Mapping[str, Any]) -> dict[str, Any]:
    stored = _mapping(trace.get("result_facts"))
    if stored:
        return stored

    columns = tuple(str(value) for value in trace.get("columns") or ())
    raw_rows = trace.get("rows")
    rows = tuple(
        tuple(row)
        for row in raw_rows or ()
        if isinstance(row, Sequence) and not isinstance(row, (str, bytes))
    )
    if not columns or not rows:
        return {"kind": "single_value", "rows": (), "columns": columns}

    lowered = tuple(column.casefold() for column in columns)
    applied_index = next(
        (index for index, column in enumerate(lowered) if column == "applied_rows"),
        None,
    )
    period_columns = tuple(
        (f"{match.group(1)}-{match.group(2)}", index)
        for index, column in enumerate(lowered)
        if (match := _FILE_PERIOD_RESULT_RE.fullmatch(column)) is not None
    )
    if len(period_columns) >= 2:
        first_row = rows[0]
        period_indices = {index for _period, index in period_columns}
        dimension_index = next(
            (
                index
                for index in range(len(columns))
                if index != applied_index and index not in period_indices
            ),
            None,
        )
        periods = tuple(
            {
                "period": period,
                "value": _decimal_to_number(value),
            }
            for period, index in period_columns
            if index < len(first_row)
            and (value := _finite_decimal(first_row[index])) is not None
        )
        result: dict[str, Any] = {
            "kind": "period_comparison" if len(periods) == 2 else "time_series",
            "columns": columns,
            "periods": periods,
            "rows": rows,
        }
        if dimension_index is not None and dimension_index < len(first_row):
            result["label"] = str(first_row[dimension_index])
        if applied_index is not None and applied_index < len(first_row):
            applied = _finite_decimal(first_row[applied_index])
            if applied is not None:
                result["applied_rows"] = _decimal_to_number(applied)
        if len(periods) >= 2:
            first = _finite_decimal(periods[0]["value"])
            last = _finite_decimal(periods[-1]["value"])
            if first is not None and last is not None:
                change = last - first
                result["change_value"] = _decimal_to_number(change)
                if first != 0:
                    result["change_pct"] = _percentage(change, first)
        return result

    value_indices = tuple(
        index
        for index, column in enumerate(columns)
        if index != applied_index and _FILE_VALUE_COLUMN_RE.search(column)
    )
    value_index = value_indices[0] if value_indices else None
    if value_index is None:
        return {
            "kind": "single_value",
            "columns": columns,
            "rows": rows,
        }

    dimension_index = next(
        (
            index
            for index in range(len(columns))
            if index != applied_index and index not in value_indices
        ),
        None,
    )
    sql = _text(trace, "executed_sql", "sql")
    if dimension_index is not None:
        values = tuple(
            value
            for row in rows
            if value_index < len(row)
            and (value := _finite_decimal(row[value_index])) is not None
        )
        total = sum(values, Decimal(0))
        output_rows: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, start=1):
            if max(dimension_index, value_index) >= len(row):
                continue
            value = _finite_decimal(row[value_index])
            if value is None:
                continue
            item: dict[str, Any] = {
                "rank": rank,
                "label": str(row[dimension_index]),
                "value": _decimal_to_number(value),
            }
            if applied_index is not None and applied_index < len(row):
                applied = _finite_decimal(row[applied_index])
                if applied is not None:
                    item["applied_rows"] = _decimal_to_number(applied)
            if total != 0:
                item["composition_pct"] = _percentage(value, total)
            output_rows.append(item)
        kind = "top_n" if re.search(r"\bLIMIT\s+\d+", sql, re.IGNORECASE) else "group_by"
        applied_values = tuple(
            _finite_decimal(row[applied_index])
            for row in rows
            if applied_index is not None and applied_index < len(row)
        )
        return {
            "kind": kind,
            "columns": columns,
            "dimension": columns[dimension_index],
            "value_column": columns[value_index],
            "rows": tuple(output_rows),
            "result_total_value": _decimal_to_number(total),
            "applied_rows": _decimal_to_number(
                sum((value for value in applied_values if value is not None), Decimal(0))
            ),
        }

    first_row = rows[0]
    value = (
        _finite_decimal(first_row[value_index])
        if value_index < len(first_row)
        else None
    )
    result = {
        "kind": "single_value",
        "columns": columns,
        "rows": rows,
        "value": _decimal_to_number(value) if value is not None else None,
        "values": {
            columns[index]: _decimal_to_number(candidate)
            for index in value_indices
            if index < len(first_row)
            and (candidate := _finite_decimal(first_row[index])) is not None
        },
    }
    if applied_index is not None and applied_index < len(first_row):
        applied = _finite_decimal(first_row[applied_index])
        if applied is not None:
            result["applied_rows"] = _decimal_to_number(applied)
    return result


def _file_result_derived_metrics(result: Mapping[str, Any]) -> dict[str, Any]:
    derived: dict[str, Any] = {}
    total = result.get("result_total_value")
    if isinstance(total, (int, float)):
        derived["result_total_value"] = total
    result_rows = result.get("rows")
    if isinstance(result_rows, Sequence) and not isinstance(result_rows, (str, bytes)):
        composition = {
            str(row.get("label")): row.get("composition_pct")
            for row in result_rows
            if isinstance(row, Mapping)
            and row.get("label") is not None
            and isinstance(row.get("composition_pct"), (int, float))
        }
        if composition:
            derived["composition_pct_by_label"] = composition
    for source, target in (
        ("change_value", "period_change_value"),
        ("change_pct", "period_change_pct"),
    ):
        value = result.get(source)
        if isinstance(value, (int, float)):
            derived[target] = value
    return derived


def _decimal_to_number(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)


def _percentage(value: Decimal, total: Decimal) -> float:
    return float(
        (value * Decimal(100) / total).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def _file_derived_metrics(
    aggregate_values: Mapping[str, Any],
    applied_row_count: Any,
) -> dict[str, Any]:
    total_value = _finite_decimal(aggregate_values.get("total_value"))
    row_count = _finite_decimal(applied_row_count)
    if total_value is None or row_count is None or row_count <= 0:
        return {}

    total_eok = (total_value / Decimal(100000000)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    average_won = (total_value / row_count).quantize(
        Decimal(1),
        rounding=ROUND_HALF_UP,
    )
    average_manwon = (total_value / row_count / Decimal(10000)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return {
        "total_value_eok": total_eok,
        "total_value_eok_display": f"{total_eok:,.2f}억원",
        "average_per_row_won": int(average_won),
        "average_per_row_display": f"약 {average_manwon:,.2f}만원",
    }


def _finite_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _document_summary_card(
    question: str,
    records: Sequence[EvidenceRecord],
    shared: dict[str, Any],
    *,
    answer_contract: AnswerContract | None = None,
) -> DerivedCoreCard:
    chunks = tuple(record for record in records if _is_vdb_record(record))
    body_chunks = _deduplicate_summary_chunks(
        record for record in chunks if _is_summary_input_eligible(record)
    )
    work_mode = (
        "list_extract"
        if answer_contract is not None
        and answer_contract.answer_shape is AnswerShape.DOCUMENT_LIST_EXTRACT
        else "summary"
        if answer_contract is not None
        and answer_contract.answer_shape is AnswerShape.DOCUMENT_SUMMARY
        else "summary"
        if is_document_summary_request(question)
        else "targeted"
    )
    summary_mode = work_mode == "summary"
    summary_chunks = (
        _distributed_summary_chunks(body_chunks, limit=8)
        if summary_mode
        else _targeted_document_chunks(question, body_chunks, limit=8)
    )
    selected_shared = {
        **shared,
        "evidence_ids": tuple(record.evidence_id for record in summary_chunks),
        "matched_count": len(chunks),
        "visible_count": len(summary_chunks),
        "visible_rows": tuple(dict(record.payload) for record in summary_chunks),
    }
    targeted_facts = (
        _targeted_document_facts(question, summary_chunks, limit=3)
        if not summary_mode
        else ()
    )
    list_items = (
        _document_list_items(question, summary_chunks)
        if work_mode == "list_extract"
        else ()
    )
    return DerivedCoreCard(
        **selected_shared,
        entity=(
            _plain_file_slot(
                _raw_text(summary_chunks[0].payload, "document_name", "file_name")
            )
            if summary_chunks
            else None
        ),
        metric="문서 요약",
        representative={
            "content": _text(summary_chunks[0].payload, "content", "text", "excerpt")
            if summary_chunks
            else ""
        },
        file_facts={
            "mode": "summary" if summary_mode else "targeted",
            "work_mode": work_mode,
            "question": _plain_file_slot(question),
            "document_name": (
                _plain_file_slot(
                    _raw_text(summary_chunks[0].payload, "document_name", "file_name")
                )
                if summary_chunks
                else ""
            ),
            "pages": tuple(
                page
                for record in summary_chunks
                if (
                    page := record.payload.get("page")
                    or record.payload.get("page_number")
                )
                is not None
            ),
            "chunks": tuple(
                _plain_file_slot(
                    _text(record.payload, "content", "text", "excerpt")
                )
                for record in summary_chunks
                if _text(record.payload, "content", "text", "excerpt")
            ),
            "targeted_facts": targeted_facts,
            "list_items": list_items,
            "searched_chunk_count": len(summary_chunks),
            "prefer_deterministic_core": bool(
                targeted_facts and _is_document_change_question(question)
            ),
        },
    )


_DOCUMENT_QUERY_STOPWORDS = frozenset(
    {
        "알려줘",
        "알려주세요",
        "보여줘",
        "보여주세요",
        "찾아줘",
        "찾아주세요",
        "관련",
        "주요",
        "기준",
        "문서",
        "파일",
        "업로드",
        "내용",
        "대한",
        "에서",
    }
)

_DOCUMENT_QUERY_CONCEPT_ALIASES: tuple[
    tuple[tuple[str, ...], tuple[str, ...]], ...
] = (
    (("가이드라인", "권고"), ("guideline", "recommendation", "recommended")),
    (("신규", "새로운", "최근"), ("new", "newly", "recent", "recently", "latest")),
    (("비스테로이드", "비 스테로이드"), ("nonsteroidal", "non-steroidal", "steroid-free")),
    (("외용", "도포", "크림"), ("topical", "cream", "ointment")),
    (("생물학제제", "바이오의약품"), ("biologic", "biological")),
    (("최초", "첫 승인"), ("first", "initial")),
    (("승인",), ("approve", "approved", "approval")),
    (("중증",), ("severe",)),
    (("3상", "임상 3상"), ("phase iii", "phase 3", "iii")),
)

_DOCUMENT_CHANGE_QUERY_TERMS = (
    "개정",
    "변경",
    "수정",
    "달라진",
    "업데이트",
    "update",
    "revision",
)
_DOCUMENT_CHANGE_MARKERS = (
    "개정",
    "변경",
    "수정",
    "삭제",
    "철회",
    "추가",
    "신설",
    "상향",
    "하향",
    "강화",
    "완화",
    "확대",
    "축소",
    "재구성",
    "새롭게",
    "revised",
    "removed",
    "withdrawn",
    "added",
)
_DOCUMENT_STRONG_CHANGE_MARKERS = (
    "삭제",
    "철회",
    "상향",
    "하향",
    "강화",
    "완화",
    "확대",
    "축소",
    "removed",
    "withdrawn",
)
_DOCUMENT_GENERIC_CHANGE_CONTEXT = (
    "진료지침 개요",
    "제작되",
    "발간되",
    "개발범위",
    "개발 범위",
)
_DOCUMENT_CHANGE_PROCESS_CONTEXT = (
    "향후 계획",
    "수행할 예정",
    "개정 작업의 연속성",
    "개발 및 제작과정",
    "문헌검색",
    "참고문헌 검색",
    "체계적문헌고찰",
)


def _targeted_document_chunks(
    question: str,
    records: Sequence[EvidenceRecord],
    *,
    limit: int,
) -> tuple[EvidenceRecord, ...]:
    terms = tuple(
        dict.fromkeys(
            token
            for token in re.findall(r"[0-9A-Za-z가-힣]+", question.casefold())
            if len(token) >= 2 and token not in _DOCUMENT_QUERY_STOPWORDS
        )
    )
    if not records or not terms:
        return _distributed_summary_chunks(records, limit=limit)

    ranked = tuple(
        sorted(
            enumerate(records),
            key=lambda indexed: (
                -sum(
                    len(term)
                    for term in terms
                    if term
                    in _text(
                        indexed[1].payload,
                        "content",
                        "text",
                        "excerpt",
                    ).casefold()
                ),
                indexed[0],
            ),
        )
    )
    if not any(
        any(
            term
            in _text(record.payload, "content", "text", "excerpt").casefold()
            for term in terms
        )
        for record in records
    ):
        return _distributed_summary_chunks(records, limit=limit)
    return tuple(record for _index, record in ranked[:limit])


def _targeted_document_facts(
    question: str,
    records: Sequence[EvidenceRecord],
    *,
    limit: int,
) -> tuple[str, ...]:
    """Select answer-bearing sentences, including cross-language concept matches."""
    match_groups = _document_query_match_groups(question)
    change_question = _is_document_change_question(question)
    ranked: list[tuple[int, int, int, int, str]] = []
    for record_index, record in enumerate(records):
        content = _text(record.payload, "content", "text", "excerpt")
        section = _text(record.payload, "section", "section_title", "heading")
        for sentence_index, sentence in enumerate(_sentences(content)):
            normalized = sentence.casefold()
            matched_groups = sum(
                any(alias in normalized for alias in aliases)
                for aliases in match_groups
            )
            named_entity_pairs = len(
                re.findall(r"\b[A-Z][A-Za-z0-9-]*\s*\([^)]+\)", sentence)
            )
            change_score = (
                _document_change_fact_score(sentence, section)
                if change_question
                else 0
            )
            score = matched_groups * 3 + named_entity_pairs * 4 + change_score
            if change_question and change_score <= 0:
                continue
            if score:
                clean_sentence = re.sub(
                    r"^\s*[-•]+\s*",
                    "",
                    _plain_file_slot(sentence),
                )
                if not clean_sentence:
                    continue
                ranked.append(
                    (
                        -score,
                        -matched_groups,
                        record_index,
                        sentence_index,
                        clean_sentence,
                    )
                )
    selected: list[str] = []
    seen: set[str] = set()
    if change_question:
        minimum_score = 1
    else:
        minimum_score = (
            max(1, (max(-entry[0] for entry in ranked) * 3 + 4) // 5)
            if ranked
            else 1
        )
    for negative_score, *_rank, sentence in sorted(ranked):
        if -negative_score < minimum_score:
            continue
        normalized = " ".join(sentence.casefold().split())
        if normalized in seen:
            continue
        seen.add(normalized)
        selected.append(sentence)
        if len(selected) >= max(1, limit):
            break
    return tuple(selected)


def _is_document_change_question(question: str) -> bool:
    normalized = question.casefold()
    return any(term in normalized for term in _DOCUMENT_CHANGE_QUERY_TERMS)


def _document_change_fact_score(sentence: str, section: str) -> int:
    normalized = sentence.casefold()
    context = f"{section} {sentence}".casefold()
    if any(term in context for term in _DOCUMENT_CHANGE_PROCESS_CONTEXT):
        return 0
    marker_count = sum(marker in normalized for marker in _DOCUMENT_CHANGE_MARKERS)
    if not marker_count:
        return 0

    score = marker_count * 4
    score += sum(marker in normalized for marker in _DOCUMENT_STRONG_CHANGE_MARKERS) * 4
    score += min(
        4,
        len(re.findall(r"\d+(?:[.,/]\d+)*\s*(?:%|mmhg|세|년|개월)?", sentence)),
    )
    if "기존" in normalized and any(
        marker in normalized for marker in ("에서", "보다", "대비")
    ):
        score += 3
    if any(term in context for term in ("주요 수정", "변화된 권고", "변경사항")):
        score += 3
    if any(term in context for term in _DOCUMENT_GENERIC_CHANGE_CONTEXT):
        score -= 8
    return max(0, score)


def _document_query_match_groups(question: str) -> tuple[tuple[str, ...], ...]:
    normalized = question.casefold()
    literal_groups = tuple(
        (token,)
        for token in dict.fromkeys(re.findall(r"[0-9a-z가-힣]+", normalized))
        if len(token) >= 2 and token not in _DOCUMENT_QUERY_STOPWORDS
    )
    concept_groups = tuple(
        tuple(dict.fromkeys((*triggers, *aliases)))
        for triggers, aliases in _DOCUMENT_QUERY_CONCEPT_ALIASES
        if any(trigger in normalized for trigger in triggers)
    )
    return (*literal_groups, *concept_groups)


def _document_question_subject(question: str) -> str:
    subject = " ".join(question.split()).strip()
    subject = re.sub(
        r"^(?:업로드한?\s*(?:파일|문서)(?:에서|의)?|이\s*문서에서)\s*",
        "",
        subject,
    )
    subject = re.sub(
        r"\s*(?:알려\s*줘(?:요)?|알려주세요|찾아\s*줘(?:요)?|찾아주세요|"
        r"보여\s*줘(?:요)?|보여주세요)\s*[.!?]*$",
        "",
        subject,
    ).strip()
    subject = re.sub(
        r"\s*(?:인가요|일까요|인가|입니까|습니까)\s*[.!?]*$",
        "",
        subject,
    ).strip()
    return re.sub(r"(?:을|를)$", "", subject).strip() or "질문한 항목"


def _render_patent_core(card: DerivedCoreCard) -> str:
    item = card.representative
    product_count = int(card.full_stats.get("product_combination_count") or 0)
    if not product_count or not item:
        return (
            "식약처 의약품 특허목록에서 직접 관련 제품특허 해당 없음으로 "
            "확인됐습니다. 기타특허를 제품특허 대표값으로 대체하지 않았습니다. "
            "확인 자료원은 식약처 의약품 특허목록입니다."
        )
    patent_no = item.get("patent_no") or "특허번호 미제공"
    status = item.get("status") or "원천 미제공"
    expiry = item.get("expiration_date") or "만료일 미제공"
    entity = item.get("product") or card.entity or "요청 제품"
    population = f"직접 관련 제품특허 {product_count}건 기준, "
    return (
        f"{population}식약처 의약품 특허목록에서 등록 상태 중 만료일이 가장 늦은 "
        f"{entity} 관련 특허 {patent_no}의 "
        f"상태는 {status}이며 존속기간 만료일은 {expiry}입니다. "
        f"같은 선정 기준의 품목은 {entity}입니다. "
        "확인 자료원은 식약처 의약품 특허목록입니다."
    )


def _render_clinical_core(card: DerivedCoreCard) -> str:
    item = card.representative
    nct_id = item.get("nct_id") or "식별자 미제공"
    status = item.get("status") or "상태 미제공"
    phase = ", ".join(item.get("phases") or ()) or "단계 미제공"
    updated = item.get("last_update_date") or "갱신일 미제공"
    phase_counts = _distribution_text(card.distributions.get("phase", {}))
    direct = card.full_stats.get(
        "direct_related_count", card.full_stats.get("direct_combination_count", 0)
    )
    late = card.full_stats.get("late_phase_count", 0)
    late_ratio = card.full_stats.get("late_phase_ratio_pct", 0)
    completed = card.full_stats.get("completed_count", 0)
    active = card.full_stats.get("active_count", 0)
    combination = card.full_stats.get("ingredient_combination") or "성분 조합 미제공"
    entity = card.entity or "요청 대상"
    subject = (
        entity
        if combination.casefold() == entity.casefold()
        else f"{entity}({combination})"
    )
    title = item.get("title") or "시험명 미제공"
    sponsors = ", ".join(
        f"{name} {count}건" for name, count in card.full_stats.get("top_sponsors", ())
    )
    base = (
        f"{subject} 관련 임상은 ClinicalTrials.gov 원천 수신 "
        f"{card.received_count}건 중 조합 직접 관련 {direct}건입니다. "
        f"완료 {completed}건·진행 {active}건이며 3상 이상 {late}건({late_ratio}%)이고, "
        f"전체 단계 분포는 {phase_counts or '분류값 미제공'}입니다. "
        f"최근 갱신은 {updated} {title}({nct_id} · {status} · {phase})이고 주요 스폰서는 "
        f"{sponsors or '원천 미제공'}입니다."
    )
    split = card.full_stats.get("query_breakdown")
    if not isinstance(split, Mapping):
        return base
    by_query = split.get("by_query")
    global_stats = split.get("global")
    if not isinstance(by_query, Sequence) or len(by_query) < 2:
        return base
    summaries = [
        f"{item.get('query')} 수신 {item.get('records_received', 0)}건·직접 관련 "
        f"{item.get('records_direct_related', 0)}건·고유 {item.get('records_unique', 0)}건"
        for item in by_query
        if isinstance(item, Mapping) and item.get("query")
    ]
    duplicates = (
        int(global_stats.get("cross_query_duplicates_removed") or 0)
        if isinstance(global_stats, Mapping)
        else 0
    )
    if not summaries:
        return base
    return (
        f"{base} 질의별로 {'; '.join(summaries)}이며, "
        f"질의 간 중복 {duplicates}건을 제거했습니다."
    )


def _render_nedrug_core(
    card: DerivedCoreCard,
    contract: AnswerContract | None = None,
) -> str:
    if (
        contract is not None
        and contract.answer_shape is AnswerShape.MULTI_FIELD_LOOKUP
    ):
        rows = tuple(
            sentence
            for row in card.visible_rows
            for sentence in _nedrug_multi_field_sentences(row, contract)
        )
        return " ".join(rows)
    approval_date = normalize_surface_dates(card.representative.get("approval_date"))
    if not approval_date:
        return ""
    entity = card.entity or "조회 품목"
    company = card.representative.get("company")
    company_sentence = f" 제조사는 {company}입니다." if company else ""
    return (
        f"식품의약품안전처 기준, {entity}의 허가일은 {approval_date}입니다."
        f"{company_sentence}"
    )


def _nedrug_multi_field_sentences(
    row: Mapping[str, Any],
    contract: AnswerContract,
) -> tuple[str, ...]:
    item = _text(row, "item_name", "ITEM_NAME", "product_name")
    if not item:
        return ()
    field_aliases = {
        "허가일": ("approval_date", "ITEM_PERMIT_DATE", "permit_date"),
        "재심사기간": (
            "reexamination_date",
            "REEXAM_DATE",
            "reexam_date",
            "REEXAM_TARGET_DATE",
        ),
        "변경일": ("change_date", "CHANGE_DATE", "last_change_date"),
    }
    fields = {
        metric: normalize_surface_dates(_text(row, *field_aliases[metric]))
        or "원천 미제공"
        for metric in contract.required_metrics
        if metric in field_aliases
    }
    first_fields = tuple(
        f"{metric} {fields[metric]}"
        for metric in ("허가일", "재심사기간")
        if metric in fields
    )
    sentences = (
        (
            f"식품의약품안전처 기준, {item}은 "
            f"{' · '.join(first_fields)}입니다."
        )
        if first_fields
        else "",
    )
    if "변경일" in fields:
        sentences = (*sentences, f"{item}의 최근 변경일은 {fields['변경일']}입니다.")
    return tuple(sentence for sentence in sentences if sentence)


def _render_disease_core(card: DerivedCoreCard) -> str:
    item = card.representative
    codes = card.full_stats.get("codes") or []
    entity = card.entity or "요청 질환"
    if card.period:
        entity = re.sub(
            rf"^{re.escape(str(card.period))}(?:년)?\s*",
            "",
            entity,
        ).strip()
    code_label = ""
    if len(codes) == 1:
        code_label = f"({codes[0]})"
    elif codes:
        code_label = f"({codes[0]} 등 {len(codes)}개 상병)"
    period_label = f"{card.period}년" if card.period else "요청 기간"
    subject = f"{period_label} {entity}{code_label}"
    value = item.get("value")
    formatted = _format_number(value)
    if value is None:
        return ""
    sexes = card.full_stats.get("sex_representatives") or {}
    ratio = card.full_stats.get("outpatient_inpatient_ratio")
    structure_parts: list[str] = []
    if isinstance(ratio, (int, float)):
        structure_parts.append(f"외래가 입원의 {_format_number(ratio)}배")
    sex_parts = tuple(
        f"{sex} {_format_number(data.get('value'))}{card.unit or ''}"
        for sex in ("남", "여")
        if isinstance((data := sexes.get(sex, {})).get("value"), (int, float))
    )
    if sex_parts:
        structure_parts.append("남녀 대표 확인값은 " + "·".join(sex_parts))
    sentences = [
        *tuple(str(notice) for notice in card.full_stats.get("scope_notices", ())),
        f"건강보험심사평가원 기준, {subject} 총 환자수는 {formatted}{card.unit or ''}입니다.",
    ]
    if structure_parts:
        sentences.append(" · ".join(structure_parts) + "입니다.")
    reconciliation = card.full_stats.get("reconciliation") or {}
    if reconciliation.get("status") == "mismatch" and reconciliation.get("reason"):
        sentences.append(str(reconciliation["reason"]))
    return " ".join(sentences)


def _render_market_core(
    card: DerivedCoreCard,
    contract: AnswerContract | None = None,
) -> str:
    item = card.representative
    value = item.get("value")
    entity = card.entity or "요청 대상"
    source = item.get("source") or "내부 데이터마트"
    if value is None:
        return ""
    if contract is not None and contract.answer_shape is AnswerShape.TIME_SERIES:
        all_series = tuple(
            row
            for row in card.full_stats.get("series", ())
            if isinstance(row, Mapping)
            and row.get("period")
            and isinstance(row.get("value"), (int, float))
        )
        period_limit = contract.required_period_count or 12
        series = all_series[-period_limit:]
        if series:
            values = " · ".join(
                f"{row['period']} {_format_number(row['value'])}{card.unit or ''}"
                for row in series
            )
            scope = (
                f"최근 {len(series)}개 기간 기준이며, 전체 기간은 표를 참조해 주세요."
                if len(all_series) > len(series)
                else (
                    f"요청 {contract.required_period_count}개월 중 보유 {len(series)}개월"
                    f"({series[0]['period']}~{series[-1]['period']}) 기준입니다."
                    if contract.required_period_count
                    and len(series) < contract.required_period_count
                    else f"총 {len(series)}개 기간을 오름차순으로 표시했습니다."
                )
            )
            return (
                f"{source} 기준 {entity} {card.metric or '매출'} 시계열은 {values}입니다. "
                f"{scope}"
            )
    return (
        f"{source}의 최신 확인 기간 {card.period or '기간 미제공'} 기준 {entity} "
        f"{card.metric or '매출'}은 {_format_number(value)}{card.unit or ''}입니다. "
        f"대표값은 수신 {card.received_count}건의 기간을 오름차순 비교해 가장 최근 행에서 선택했습니다. "
        "다른 기간 값은 같은 자료원과 지표 단위로 분리됩니다."
    )


def _render_file_aggregate_core(
    card: DerivedCoreCard,
    contract: AnswerContract | None = None,
) -> str:
    facts = card.file_facts
    query_result = _mapping(facts.get("query_result"))
    kind = str(query_result.get("kind") or "single_value")
    document = facts.get("document_name") or "업로드 파일"
    sheet = facts.get("sheet_name") or "시트 미제공"
    applied_rows = query_result.get("applied_rows")
    row_sentence = (
        f"SQL 집계에는 적용 {_format_number(applied_rows)}행이 기록됐습니다."
        if isinstance(applied_rows, (int, float))
        else ""
    )

    if kind == "file_analytics":
        answer = str(facts.get("analytics_answer") or "").strip()
        return answer

    if kind in {"period_comparison", "time_series"}:
        periods = tuple(
            period
            for period in query_result.get("periods") or ()
            if isinstance(period, Mapping)
            and isinstance(period.get("value"), (int, float))
        )
        if len(periods) >= 2:
            first, last = periods[0], periods[-1]
            change = query_result.get("change_value")
            change_pct = query_result.get("change_pct")
            if isinstance(change, (int, float)):
                if change > 0:
                    change_text = f"{_format_number(change)}원"
                    if isinstance(change_pct, (int, float)):
                        change_text += f"({_format_number(change_pct)}% 증가)"
                elif change < 0:
                    change_text = f"{_format_number(abs(change))}원"
                    if isinstance(change_pct, (int, float)):
                        change_text += f"({_format_number(abs(change_pct))}% 감소)"
                else:
                    change_text = "변동 없음"
            else:
                change_text = "증감 미제공"
            return " ".join(
                part
                for part in (
                    (
                        f"{query_result.get('label') or document}의 {sheet} 시트 총액은 "
                        f"{first.get('period')} {_format_number(first.get('value'))}원에서 "
                        f"{last.get('period')} {_format_number(last.get('value'))}원으로 "
                        f"변했고, 증감은 {change_text}입니다."
                    ),
                    row_sentence,
                )
                if part
            )

    if kind in {"top_n", "group_by"}:
        rows = tuple(
            row
            for row in query_result.get("rows") or ()
            if isinstance(row, Mapping)
            and row.get("label") is not None
            and isinstance(row.get("value"), (int, float))
        )
        if rows:
            if kind == "top_n":
                top_k = contract.top_k if contract is not None else 3
                values = ", ".join(
                    f"{row.get('rank')}위 {row.get('label')} "
                    f"{_format_number(row.get('value'))}원"
                    for row in rows[:top_k]
                )
                first_sentence = (
                    f"{document}의 {sheet} 시트 상위 결과는 {values}입니다."
                )
            else:
                values = ", ".join(
                    (
                        f"{row.get('label')} {_format_number(row.get('value'))}원"
                        + (
                            f"({_format_number(row.get('composition_pct'))}%)"
                            if isinstance(row.get("composition_pct"), (int, float))
                            else ""
                        )
                    )
                    for row in rows[:3]
                )
                first_sentence = (
                    f"{document}의 {sheet} 시트 구성 분포는 {values}입니다."
                )
            result_total = query_result.get("result_total_value")
            denominator = query_result.get("denominator_total")
            if kind == "top_n" and isinstance(denominator, (int, float)):
                total_sentence = (
                    f"비중 분모인 같은 선택 범위 합계는 "
                    f"{_format_number(denominator)}원입니다."
                )
            elif kind == "top_n" and isinstance(result_total, (int, float)):
                total_sentence = (
                    "비중 분모는 원천에서 별도 제공되지 않았고, 반환된 상위 결과 "
                    f"합계는 {_format_number(result_total)}원입니다."
                )
            else:
                total_sentence = (
                    f"반환된 구분값 합계는 {_format_number(result_total)}원입니다."
                    if isinstance(result_total, (int, float))
                    else ""
                )
            return " ".join(
                part
                for part in (first_sentence, total_sentence, row_sentence)
                if part
            )

    aggregates = {
        **_mapping(facts.get("aggregate_values")),
        **_mapping(query_result.get("values")),
    }
    total_value = aggregates.get("total_value")
    total_quantity = aggregates.get("total_quantity")
    if isinstance(total_value, (int, float)) and isinstance(
        total_quantity, (int, float)
    ):
        value_text = (
            f"{_format_number(total_value)}원이고 판매수량은 "
            f"{_format_number(total_quantity)} UNIT"
        )
    else:
        value = total_value
        if not isinstance(value, (int, float)):
            value = next(
                (
                    candidate
                    for key, candidate in aggregates.items()
                    if key != "applied_rows" and isinstance(candidate, (int, float))
                ),
                None,
            )
        value_text = (
            f"{_format_number(value)}원"
            if isinstance(value, (int, float))
            else "집계값 미제공"
        )
    applied_rows = facts.get("applied_row_count")
    row_sentence = (
        f"SQL 집계에는 적용 {_format_number(applied_rows)}행이 기록됐습니다."
        if isinstance(applied_rows, (int, float))
        else "SQL 집계 적용 행수는 원천에서 제공되지 않았습니다."
    )
    result_rows = facts.get("total_row_count")
    result_sentence = (
        f"실행 결과는 {_format_number(result_rows)}개 집계 행으로 반환되었습니다."
        if isinstance(result_rows, (int, float))
        else ""
    )
    return " ".join(
        part
        for part in (
            (
                f"{facts.get('document_name') or '업로드 파일'}의 "
                f"{facts.get('sheet_name') or '시트 미제공'} 시트에서 "
                f"{facts.get('period') or '요청 기간'} 총액은 {value_text}입니다."
            ),
            row_sentence,
            result_sentence,
        )
        if part
    )


def _render_document_summary_core(card: DerivedCoreCard) -> str:
    if card.file_facts.get("work_mode") == "list_extract":
        return _render_document_list_extract_core(card)
    chunks = tuple(
        str(value).strip()
        for value in card.file_facts.get("chunks", ())
        if str(value).strip()
    )
    if not chunks:
        return ""
    document = card.file_facts.get("document_name") or card.entity or "업로드 문서"
    if card.file_facts.get("mode") == "targeted":
        targeted_facts = tuple(
            str(value).strip()
            for value in card.file_facts.get("targeted_facts", ())
            if str(value).strip()
        )
        localized_patient_count = _localized_document_patient_count_fact(
            str(card.file_facts.get("question") or ""),
            str(document),
            (*targeted_facts, *chunks),
        )
        if localized_patient_count:
            return localized_patient_count
        if targeted_facts:
            named_pairs = tuple(
                dict.fromkeys(
                    match.group(0)
                    for fact in targeted_facts
                    for match in re.finditer(
                        r"\b[A-Z][A-Za-z0-9-]*\s*\([^)]+\)",
                        fact,
                    )
                )
            )
            if named_pairs:
                subject = _document_question_subject(
                    str(card.file_facts.get("question") or "")
                )
                return (
                    f"{document}는 {subject} 관련 항목으로 "
                    f"{' · '.join(named_pairs)}를 제시합니다."
                )
            return " ".join(targeted_facts[:3])
        direct_facts = tuple(
            dict.fromkeys(
                fact
                for chunk in chunks
                if (fact := _complete_sentence(chunk))
            )
        )
        return " ".join(direct_facts[:3])
    facts = tuple(
        dict.fromkeys(fact for chunk in chunks if (fact := _summary_fact(chunk)))
    )
    identity = f"{document}는 업로드된 문서의 본문 근거를 요약한 자료입니다."
    return " ".join((identity, *facts[:5]))


def _render_document_list_extract_core(card: DerivedCoreCard) -> str:
    question = str(card.file_facts.get("question") or "")
    requested_phase = _requested_document_phase(question)
    items = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in card.file_facts.get("list_items", ())
            if str(value).strip()
            and (
                requested_phase is None
                or _document_phase_matches(str(value), requested_phase)
            )
        )
    )
    if not items:
        return ""
    document = card.file_facts.get("document_name") or card.entity or "업로드 문서"
    searched = card.file_facts.get("searched_chunk_count")
    scope = (
        f"검색된 본문 청크 {_format_number(searched)}건 기준"
        if isinstance(searched, int)
        else "검색된 본문 청크 기준"
    )
    label = f"{requested_phase}상" if requested_phase is not None else "요청 목록"
    return " ".join(
        (
            f"{document}의 {label} 항목은 {scope} {_format_number(len(items))}건입니다.",
            *items,
        )
    )


def _document_list_items(
    question: str,
    records: Sequence[EvidenceRecord],
) -> tuple[str, ...]:
    requested_phase = _requested_document_phase(question)
    items: list[str] = []
    for record in records:
        content = _text(record.payload, "content", "text", "excerpt")
        units = _document_content_units(content)
        for unit in units:
            if not unit or (
                requested_phase is not None
                and not _document_phase_matches(unit, requested_phase)
            ):
                continue
            items.append(unit)
    return tuple(dict.fromkeys(items))


def _document_content_units(content: str) -> tuple[str, ...]:
    normalized = " ".join(content.split())
    if "|" not in normalized:
        return _sentences(normalized) or (_complete_sentence(normalized),)
    rows = tuple(
        row.strip(" |").strip()
        for row in re.split(r"\|\|\s*(?=\|)", normalized)
        if row.strip(" |").strip()
    )
    return rows or (_complete_sentence(normalized),)


def _requested_document_phase(question: str) -> int | None:
    match = re.search(
        r"(?:(?P<ko>[1-4])\s*상|phase\s*(?P<en>[1-4]))",
        question,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return int(match.group("ko") or match.group("en"))


def _document_phase_matches(value: str, phase: int) -> bool:
    roman = {1: "i", 2: "ii", 3: "iii", 4: "iv"}[phase]
    return re.search(
        rf"(?:{phase}\s*상|phase\s*(?:{phase}|{roman})\b|"
        rf"\|\s*{roman}\s*(?:\||$)|\b{roman}\b)",
        value,
        flags=re.IGNORECASE,
    ) is not None


def _localized_document_patient_count_fact(
    question: str,
    document: str,
    facts: Sequence[str],
) -> str:
    if not any(term in question for term in ("환자수", "환자 수")):
        return ""
    for fact in facts:
        match = re.search(
            r"(?P<value>\d+(?:\.\d+)?)\s+million\s+"
            r"(?:(?:diagnosed|prevalent)\s+){0,2}cases?\b",
            fact,
            flags=re.IGNORECASE,
        )
        if match is None:
            continue
        value_man = _decimal_to_number(Decimal(match.group("value")) * Decimal(100))
        subject = _document_question_subject(question)
        return (
            f"{document}에 따르면, {subject}는 약 "
            f"{_format_number(value_man)}만 명입니다."
        )
    return ""


def _is_sql_record(record: EvidenceRecord) -> bool:
    payload = record.payload
    route = _text(payload, "route", "document_route", "file_route").casefold()
    return (
        route == "sql"
        or "sql" in record.result_kind.casefold()
        or bool(payload.get("sql_trace"))
        or bool(payload.get("sql_detail"))
    )


def _is_summary_navigation_chunk(value: str) -> bool:
    text = " ".join(value.split()).casefold()
    if not text:
        return True
    if text.startswith(
        (
            "목차",
            "차례",
            "table of contents",
            "contents",
            "인사말",
            "발간사",
            "머리말",
            "preface",
            "foreword",
            "cover",
        )
    ):
        return True
    lines = tuple(line.strip() for line in value.splitlines() if line.strip())
    if bool(lines) and sum(line.startswith("|") for line in lines) >= 2:
        return True
    if (
        text.endswith(("?", "？"))
        and len(text) <= 160
        and any(
            token in text
            for token in ("무엇", "어떻게", "언제", "알려", "정리", "보여")
        )
    ):
        return True
    return bool(
        re.fullmatch(
            r".{1,100}\|\s*(?:19|20)\d{2}[-./]\d{1,2}[-./]\d{1,2}",
            text,
            flags=re.IGNORECASE,
        )
    )


def _is_summary_input_eligible(record: EvidenceRecord) -> bool:
    explicit = record.payload.get("summary_input_eligible")
    if isinstance(explicit, bool):
        return explicit
    return not _is_summary_navigation_chunk(
        _text(record.payload, "content", "text", "excerpt")
    )


def _deduplicate_summary_chunks(
    records: Iterable[EvidenceRecord],
) -> tuple[EvidenceRecord, ...]:
    unique: list[EvidenceRecord] = []
    seen: set[str] = set()
    for record in records:
        content = " ".join(
            _text(record.payload, "content", "text", "excerpt").casefold().split()
        )
        if not content or content in seen:
            continue
        seen.add(content)
        unique.append(record)
    return tuple(unique)


def _distributed_summary_chunks(
    records: Sequence[EvidenceRecord],
    *,
    limit: int,
) -> tuple[EvidenceRecord, ...]:
    ordered = tuple(sorted(records, key=_summary_page_order))
    if len(ordered) <= limit:
        return ordered
    indexes = tuple(
        round(index * (len(ordered) - 1) / (limit - 1))
        for index in range(limit)
    )
    return tuple(ordered[index] for index in indexes)


def _summary_page_order(record: EvidenceRecord) -> tuple[int, int, tuple[int, int, int, str]]:
    page = record.payload.get("page") or record.payload.get("page_number")
    try:
        page_number = int(page)
    except (TypeError, ValueError):
        page_number = 10**9
    return (page_number == 10**9, page_number, _summary_chunk_rank(record))


def _summary_chunk_rank(record: EvidenceRecord) -> tuple[int, int, int, str]:
    content = _text(record.payload, "content", "text", "excerpt")
    page = record.payload.get("page") or record.payload.get("page_number")
    numeric = bool(re.search(r"\d[\d,.]*\s*(?:%|명|건|원|년|개월)", content))
    prose_tokens = len(re.findall(r"[0-9A-Za-z가-힣]+", content))
    page_number = int(page) if isinstance(page, int) else 10**9
    return (0 if numeric else 1, -prose_tokens, page_number, record.evidence_id)


def _is_vdb_record(record: EvidenceRecord) -> bool:
    payload = record.payload
    if _is_sql_record(record):
        return False
    route = _text(payload, "route", "document_route", "file_route").casefold()
    return (
        route == "vdb"
        or "chunk" in record.result_kind.casefold()
        or bool(_text(payload, "content", "excerpt"))
    )


def _text(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return " ".join(str(value).split())
    return ""


def _raw_text(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _plain_file_slot(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("|", " ")
    text = re.sub(r"[*`#>~]+", "", text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    return text.strip()


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _status_rank(value: str) -> int:
    normalized = value.casefold()
    return 2 if "등록" in normalized and "소멸" not in normalized else 1


def _number(payload: Mapping[str, Any]) -> int | float | None:
    for key in (
        "patient_count",
        "patientCount",
        "ptntCnt",
        "ptnt_cnt",
        "value",
        "count",
    ):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            cleaned = value.replace(",", "").strip()
            if re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
                return float(cleaned) if "." in cleaned else int(cleaned)
    return None


def _market_value(payload: Mapping[str, Any], metric: str) -> int | float | None:
    if metric == "점유율":
        return _first_number(
            payload,
            ("ms_pct", "share", "market_share", "share_pct", "점유율"),
        )

    direct_eok = _first_number(
        payload,
        ("value_억원", "sales_억원", "value_recent_억원"),
    )
    if direct_eok is not None:
        return direct_eok

    sales_krw = _first_number(payload, ("sales_krw", "value_krw"))
    if sales_krw is not None:
        return float(sales_krw) / 100_000_000

    legacy_sales = _first_number(
        payload,
        ("sales", "sellout", "amount", "market_value", "total_value", "매출"),
    )
    if legacy_sales is not None:
        return legacy_sales

    measure = _text(payload, "measure", "metric").casefold()
    if measure not in {"sales", "sale", "revenue", "매출"}:
        return None
    value = _first_number(payload, ("value",))
    if value is None:
        return None
    unit = _text(payload, "unit_label", "unit").casefold()
    if unit in {"krw", "원"}:
        return float(value) / 100_000_000
    if unit in {"억원", "100m krw"}:
        return value
    return None


def _first_number(
    payload: Mapping[str, Any], keys: Sequence[str]
) -> int | float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            cleaned = value.replace(",", "").replace("%", "").strip()
            if re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
                return float(cleaned) if "." in cleaned else int(cleaned)
    return None


def _period(payload: Mapping[str, Any]) -> str:
    return _text(payload, "period", "year_month", "yyyymm", "month", "year", "PERIOD")


def _question_period(question: str) -> str:
    korean = re.search(r"(?P<year>20\d{2})\s*년\s*(?P<month>\d{1,2})\s*월", question)
    if korean:
        return f"{korean.group('year')}-{int(korean.group('month')):02d}"
    compact = re.search(r"(?P<year>20\d{2})[-./](?P<month>\d{1,2})", question)
    if compact:
        return f"{compact.group('year')}-{int(compact.group('month')):02d}"
    return ""


def _question_period_display(question: str) -> str:
    korean = re.search(r"(?P<year>20\d{2})\s*년\s*(?P<month>\d{1,2})\s*월", question)
    if korean:
        return f"{korean.group('year')}년 {int(korean.group('month'))}월"
    return _question_period(question)


def _axis_position(question: str, card_type: AnswerType) -> int:
    normalized = question.casefold()
    terms_by_type: dict[AnswerType, tuple[str, ...]] = {
        "disease": ("환자", "유병률", "상병", "성별", "연령"),
        "market": ("매출", "총액", "점유율", "sellout", "sell out"),
        "patent": ("특허", "만료"),
        "clinical": ("임상", "clinical", "trial", "nct"),
        "file_aggregate": ("총액", "합계", "집계", "sellout", "sell out"),
        "document_summary": ("요약", "문서", "pdf", "파일"),
        "mixed": (),
        "general": (),
    }
    positions = tuple(
        position
        for term in terms_by_type[card_type]
        if (position := normalized.find(term)) >= 0
    )
    return min(positions, default=len(normalized) + 1)


def _requested_card_types(question: str) -> frozenset[AnswerType]:
    requested = {
        card_type
        for card_type in ("disease", "market", "patent", "clinical")
        if _axis_position(question, cast(AnswerType, card_type)) <= len(question)
    }
    normalized = question.casefold()
    has_file_reference = any(
        term in normalized for term in ("업로드", "파일", "문서", "pdf", "엑셀", "xlsx")
    )
    if has_file_reference and any(
        term in normalized
        for term in (
            "총액",
            "합계",
            "집계",
            "sellin",
            "sell in",
            "sellout",
            "sell out",
            "매출",
            "수량",
            "점유율",
            "성장률",
            "cagr",
            "가격",
        )
    ):
        requested.add("file_aggregate")
    elif has_file_reference:
        requested.add("document_summary")
    return frozenset(requested)


def _is_late_phase(phase: str) -> bool:
    normalized = phase.casefold().replace(" ", "")
    return any(token in normalized for token in ("phase3", "phase4", "3상", "4상"))


def _dimension_representatives(
    records: Sequence[EvidenceRecord],
    normalizer: Callable[[Mapping[str, Any]], str],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[EvidenceRecord]] = {}
    for record in records:
        label = normalizer(record.payload)
        if label and _number(record.payload) is not None:
            grouped.setdefault(label, []).append(record)
    representatives: dict[str, dict[str, Any]] = {}
    for label, candidates in grouped.items():
        selected = max(
            candidates,
            key=lambda record: (
                _is_total_row(record.payload),
                _number(record.payload) or 0,
                record.evidence_id,
            ),
        )
        representatives[label] = {
            "value": _number(selected.payload),
            "code": _text(
                selected.payload,
                "sickCd",
                "sick_cd",
                "disease_code",
                "code",
            ),
            "dimension": _dimension(selected.payload),
            "evidence_id": selected.evidence_id,
        }
    return dict(sorted(representatives.items()))


def _matched_care_ratio(records: Sequence[EvidenceRecord]) -> float | None:
    """Compare outpatient and inpatient values only within an identical grain."""

    grouped: dict[tuple[str, str, str, str], dict[str, float]] = {}
    for record in records:
        care = _normalized_care(record.payload)
        value = _number(record.payload)
        if care not in {"외래", "입원"} or value is None:
            continue
        scope = (
            _text(record.payload, "sickCd", "sick_cd", "disease_code", "code"),
            _period(record.payload),
            _normalized_sex(record.payload),
            _text(record.payload, "age", "age_group", "ageCd"),
        )
        if not all(scope[:3]):
            continue
        bucket = grouped.setdefault(scope, {})
        bucket[care] = max(float(value), bucket.get(care, float("-inf")))

    comparable = tuple(
        values
        for values in grouped.values()
        if values.get("입원", 0) > 0 and "외래" in values
    )
    if not comparable:
        return None
    selected = max(comparable, key=lambda values: values["외래"])
    return round(selected["외래"] / selected["입원"], 2)


def _normalized_care(payload: Mapping[str, Any]) -> str:
    raw = _text(payload, "inpatOpat", "care_type", "visit_type").casefold()
    if raw in {"외래", "outpatient", "o"} or "외래" in raw:
        return "외래"
    if raw in {"입원", "inpatient", "i"} or "입원" in raw:
        return "입원"
    return ""


def _normalized_sex(payload: Mapping[str, Any]) -> str:
    raw = _text(payload, "sex", "gender", "sexCd").casefold()
    if raw in {"남", "남성", "male", "m", "1"}:
        return "남"
    if raw in {"여", "여성", "female", "f", "2"}:
        return "여"
    return ""


def _contains_markdown_table(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if not (line.startswith("|") and line.endswith("|")):
            continue
        separator = lines[index + 1]
        if separator.startswith("|") and re.fullmatch(
            r"\|(?:\s*:?-{3,}:?\s*\|)+",
            separator,
        ):
            return True
    return False


def _dimension(payload: Mapping[str, Any]) -> str:
    parts = tuple(
        value
        for keys in (
            ("inpatOpat", "care_type", "visit_type"),
            ("sex", "gender", "sexCd"),
            ("age", "age_group", "ageCd"),
        )
        if (value := _text(payload, *keys))
    )
    return "·".join(parts)


def _is_total_row(payload: Mapping[str, Any]) -> bool:
    return hira_is_aggregate_row(payload)


def _count_values(
    records: Sequence[EvidenceRecord], keys: tuple[str, ...]
) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                value for record in records if (value := _text(record.payload, *keys))
            ).items()
        )
    )


def _question_entity(question: str, suffixes: Sequence[str]) -> str:
    normalized = " ".join(question.split())
    positions = [normalized.find(suffix) for suffix in suffixes if suffix in normalized]
    if not positions:
        return ""
    prefix = normalized[: min(positions)].strip()
    return re.sub(r"(?:알려줘|보여줘|현황|기준으로도)$", "", prefix).strip()


def _distribution_text(values: Mapping[str, int]) -> str:
    return ", ".join(f"{key} {value}건" for key, value in values.items())


def _format_number(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _sentences(value: str) -> tuple[str, ...]:
    return tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", value)
        if sentence.strip()
    )


def _complete_sentence(value: str) -> str:
    text = " ".join(value.split()).strip()
    if not text:
        return ""
    boundary = re.search(r"^(.{1,300}?[.!?])(?:\s|$)", text)
    if boundary:
        return boundary.group(1)
    return f"{text[:300].rstrip(' ,;:')}입니다."


def _summary_fact(value: str) -> str:
    sentence = _complete_sentence(value)
    quantitative = re.match(
        r"^(?P<subject>.+?)(?:은|는|이|가)\s*(?P<value>-?\d[\d,.]*\s*%?)"
        r"(?:로|으로)?(?:\s*확인)?(?:됩니다|입니다|이다)?[.!?]?$",
        sentence,
    )
    if quantitative:
        subject = quantitative.group("subject").strip()
        value_text = quantitative.group("value").replace(" ", "")
        return f"문서는 {subject}을 {value_text}로 제시합니다."
    return ""


__all__ = [
    "AnswerType",
    "DerivedCoreCard",
    "FactDigest",
    "build_fact_digest",
    "classify_answer_type",
    "render_core_answer",
    "render_file_analytics_tables",
    "render_hira_statistics_tables",
]
