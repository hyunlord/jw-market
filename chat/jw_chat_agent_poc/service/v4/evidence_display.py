from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet
from jw_chat_agent_poc.service.v4.source_labels import SOURCE_LABELS

_MAX_QUERY_CHARS = 240
_MAX_VALUE_CHARS = 600
_MAX_SEARCH_NODES = 160

_IDENTIFIER_FIELDS: dict[str, tuple[str, ...]] = {
    "clinicaltrials": ("nct_id", "nctId", "study_id"),
    "patent": ("patent_number", "patent_no", "application_number"),
    "mart": ("brand", "brand_name", "product_name", "record_id"),
    "nedrug": ("item_name", "product_name", "item_seq"),
    "hira": ("disease_code", "sick_code", "kcd_code"),
    "openfda": ("generic_name", "active_ingredient", "substance_name"),
    "web": ("url", "title"),
    "document": ("chunk_id", "file_name", "sheet_name", "record_id"),
    "prior_turn": ("turns_ago", "previous_question"),
}

_DISPLAY_FIELDS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "clinicaltrials": (
        ("시험명", ("brief_title", "official_title", "title")),
        ("상태", ("overall_status", "status")),
        ("단계", ("phase", "phases")),
        ("스폰서", ("lead_sponsor", "sponsor_name", "sponsor")),
        ("최근 갱신일", ("last_update_post_date", "last_update", "updated_at")),
    ),
    "mart": (
        ("시장", ("market", "market_name", "atc_name", "atc_code")),
        ("브랜드", ("brand", "brand_name", "product_name")),
        ("기간", ("period", "year_month", "month", "year")),
        ("매출", ("sales", "sales_value", "value", "amount")),
        ("점유율", ("market_share", "share", "ms_share")),
        ("순위", ("rank", "market_rank")),
    ),
    "patent": (
        ("특허번호", ("patent_number", "patent_no", "application_number")),
        ("상태", ("status", "patent_status")),
        ("만료일", ("expiry_date", "expiration_date", "patent_expiry")),
        ("구분", ("patent_type", "type", "category")),
    ),
    "hira": (
        ("상병코드", ("disease_code", "sick_code", "kcd_code")),
        ("기간", ("period", "year_month", "year")),
        ("구분", ("category", "division", "visit_type", "admission_type")),
        ("성별", ("sex", "gender")),
        ("연령", ("age_group", "age")),
        ("환자수", ("patient_count", "patients", "value")),
    ),
    "openfda": (
        ("성분", ("generic_name", "active_ingredient", "substance_name")),
        ("항목", ("reaction", "event", "category", "title")),
        ("요지", ("summary", "description", "text", "excerpt")),
    ),
    "nedrug": (
        ("품목", ("item_name", "product_name")),
        ("품목번호", ("item_seq", "product_code")),
        ("허가일", ("approval_date", "permit_date")),
        ("재심사 만료일", ("reexam_date", "reexamination_date")),
        ("최근 변경일", ("change_date", "last_change_date", "updated_at")),
    ),
    "document": (
        ("파일", ("file_name", "document_name")),
        ("시트", ("sheet_name", "table_name")),
        ("페이지", ("page", "page_number")),
        ("행 요지", ("summary", "content_excerpt", "excerpt", "text")),
    ),
    "web": (
        ("제목", ("title",)),
        ("게시일", ("published_at", "date")),
        ("요지", ("snippet", "summary", "excerpt")),
        ("주소", ("url",)),
        ("수집 시각", ("collected_at",)),
        ("본문 상태", ("content_status",)),
    ),
    "prior_turn": (
        ("이전 질문", ("previous_question",)),
        ("답변 시각", ("previous_answered_at",)),
        ("가져온 항목", ("carried_items", "identifiers")),
        ("원 출처", ("original_sources",)),
        ("이번 턴 재조회", ("requeried",)),
        ("재조회 원천", ("requery_sources",)),
    ),
}


def build_evidence_display_catalog(
    evidence_sets: Sequence[EvidenceSet],
    *,
    evidence_ids: Sequence[str],
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    requested = tuple(dict.fromkeys(identifier for identifier in evidence_ids if identifier))
    records = {
        record.evidence_id: (evidence_set, record)
        for evidence_set in evidence_sets
        for record in evidence_set.records
    }
    catalog: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    for evidence_id in requested:
        matched = records.get(evidence_id)
        if matched is None:
            missing.append(evidence_id)
            continue
        evidence_set, record = matched
        source = _source_key(record.source or evidence_set.source)
        display: dict[str, object] = {
            "evidence_id": evidence_id,
            "source_name": SOURCE_LABELS.get(source, record.source or evidence_set.source),
            "identifier": _public_identifier(record, source),
            "query": _query_text(evidence_set.query_spec),
            "counts": {
                "received": evidence_set.coverage.records_received,
                "direct_related": evidence_set.coverage.records_relevant,
            },
            "record": _display_record(record.payload, source),
        }
        aggregate = _aggregate_evidence(evidence_set)
        if aggregate is not None:
            display["aggregate"] = aggregate
        catalog[evidence_id] = display
    return catalog, {
        "requested_count": len(requested),
        "included_count": len(catalog),
        "missing_count": len(missing),
        "missing_ids": missing,
    }


def _source_key(source: str) -> str:
    if source in {"document_rag", "document_sql"}:
        return "document"
    if source in {"web_news", "tavily", "tavily_mcp"}:
        return "web"
    return source.split(":", 1)[0]


def _aggregate_evidence(evidence_set: EvidenceSet) -> dict[str, object] | None:
    aggregate = next(
        (
            item
            for item in evidence_set.query_manifest
            if item.get("lane") == "surface_full_aggregate"
        ),
        None,
    )
    if aggregate is None:
        return None
    distributions = {
        "status": dict(aggregate.get("direct_status_counts") or {}),
        "phase": dict(aggregate.get("direct_phase_counts") or {}),
        "sponsor": dict(aggregate.get("direct_sponsor_counts") or {}),
    }
    return {
        "received": evidence_set.coverage.records_received,
        "direct_related": int(
            aggregate.get("direct_related_count")
            or evidence_set.coverage.records_relevant
            or 0
        ),
        "distributions": distributions,
        "population_caption": (
            f"직접 관련 {int(aggregate.get('direct_related_count') or 0):,}건 기준"
        ),
    }


def _query_text(queries: Sequence[str]) -> str:
    text = " / ".join(dict.fromkeys(" ".join(query.split()) for query in queries if query.strip()))
    return text[:_MAX_QUERY_CHARS]


def _public_identifier(record: EvidenceRecord, source: str) -> str:
    value = _first_value(record.payload, _IDENTIFIER_FIELDS.get(source, ()))
    if value:
        return value
    return record.evidence_id.split(":", 1)[-1]


def _display_record(payload: Mapping[str, Any], source: str) -> dict[str, str]:
    fields = _DISPLAY_FIELDS.get(source, _DISPLAY_FIELDS["web"])
    output: dict[str, str] = {}
    for label, aliases in fields:
        value = _first_value(payload, aliases)
        if value:
            output[label] = (
                "예"
                if source == "prior_turn" and label == "이번 턴 재조회" and value == "True"
                else "아니요"
                if source == "prior_turn" and label == "이번 턴 재조회" and value == "False"
                else value
            )
    return output


def _first_value(payload: Mapping[str, Any], aliases: Sequence[str]) -> str:
    wanted = {alias.casefold() for alias in aliases}
    queue: deque[object] = deque((payload,))
    visited = 0
    while queue and visited < _MAX_SEARCH_NODES:
        current = queue.popleft()
        visited += 1
        if isinstance(current, Mapping):
            for key, value in current.items():
                if str(key).casefold() in wanted:
                    scalar = _scalar_text(value)
                    if scalar:
                        return scalar
            queue.extend(current.values())
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            queue.extend(current[:20])
    return ""


def _scalar_text(value: object) -> str:
    if value is None or isinstance(value, (Mapping, bytes, bytearray)):
        return ""
    if isinstance(value, Sequence) and not isinstance(value, str):
        scalars = [_scalar_text(item) for item in value[:8]]
        text = ", ".join(item for item in scalars if item)
    elif isinstance(value, (str, int, float, bool)):
        text = " ".join(str(value).split())
    else:
        return ""
    return text[:_MAX_VALUE_CHARS]


__all__ = ["build_evidence_display_catalog"]
