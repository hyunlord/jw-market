from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

from jw_chat_agent_poc.service.v4.clinical import normalize_clinical_detail
from jw_chat_agent_poc.service.v4.contracts import SourceResult
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
    SourceReference,
)
from jw_chat_agent_poc.service.v4.retrieval_events import failure_status_from_value


def generic_evidence_set(
    source: str,
    results: Sequence[SourceResult],
    observed_on: date,
) -> EvidenceSet:
    records: list[EvidenceRecord] = []
    failures: list[dict[str, Any]] = []
    for result_index, result in enumerate(results, start=1):
        if result.status != "ok":
            failures.append(result_failure(result))
        source_calls = calls(result)
        if not source_calls and result.status == "ok" and isinstance(result.payload, Mapping):
            source_calls = [dict(result.payload)]
        for call_index, call in enumerate(source_calls, start=1):
            if _call_failure_status(call) is not None:
                failures.append(call_failure(call))
                continue
            call_records = generic_call_records(source, call)
            for record_index, raw_record in enumerate(call_records, start=1):
                evidence_id = f"{source}:{result_index}:{call_index}:{record_index}"
                record = normalize_generic_record(source, raw_record)
                records.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        source=source,
                        result_kind=f"{source}_record",
                        payload={**record, "evidence_id": evidence_id},
                        source_refs=record_refs(record),
                    )
                )
    refs = dedupe_refs(
        [*(result_refs(result) for result in results), *(record.source_refs for record in records)]
    )
    return EvidenceSet(
        source=source,
        query_spec=tuple(dict.fromkeys(result.query for result in results)),
        retrieved_at=retrieved_at(results, observed_on),
        coverage=CoverageLedger(
            total_reported=len(records),
            records_received=len(records),
            records_unique=len(records),
        ),
        records=tuple(records),
        item_failures=tuple(failures),
        source_refs=refs,
    )


def _call_failure_status(call: Mapping[str, Any]) -> str | None:
    render_data = mapping(call.get("render_data"))
    for value in (call.get("status"), render_data.get("status")):
        classified = failure_status_from_value(value)
        if classified is not None:
            return classified
    return None


def generic_call_records(source: str, call: Mapping[str, Any]) -> list[dict[str, Any]]:
    render_data = mapping(call.get("render_data"))
    payload = mapping(render_data.get("payload"))
    candidates = (
        render_data.get("items"),
        payload.get("results"),
        payload.get("items"),
        payload.get("rows"),
        call.get("records"),
        call.get("rows"),
        call.get("items"),
    )
    for candidate in candidates:
        records = mapping_list(candidate)
        if records:
            return [dict(record) for record in records]
    if source == "mart" and render_data:
        return [dict(render_data)]
    return [dict(call)]


def normalize_generic_record(source: str, raw_record: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(raw_record)
    if source == "mart":
        record.update(_mart_public_fields(record))
    elif source == "nedrug":
        record.update(
            {
                key: value
                for key, value in {
                    "item_name": text(record.get("ITEM_NAME")) or None,
                    "company": text(record.get("ENTP_NAME")) or None,
                    "approval_date": text(record.get("ITEM_PERMIT_DATE")) or None,
                    "active_ingredient": text(
                        record.get("ITEM_INGR_NAME") or record.get("MAIN_INGR_ENG")
                    ) or None,
                    "status": text(record.get("CANCEL_NAME")) or None,
                }.items()
                if value not in (None, "")
            }
        )
    elif source == "web":
        summary = text(record.get("summary") or record.get("snippet") or record.get("content"))
        bounded_summary, summary_truncated = _bounded_text(summary, limit=800)
        url = text(record.get("url"))
        record.update(
            {
                "publisher": text(record.get("publisher")) or _publisher_from_url(url),
                "published_at": text(
                    record.get("published_at") or record.get("published_date")
                ) or None,
                "summary": bounded_summary or None,
                "summary_truncated": summary_truncated,
            }
        )
    elif source == "openfda":
        openfda = mapping(record.get("openfda"))
        label_text = _first_text(
            record.get("label_section")
            or record.get("indications_and_usage")
            or record.get("purpose")
        )
        bounded_label, label_truncated = _bounded_text(label_text, limit=800)
        record.update(
            {
                "product_name": _first_text(
                    record.get("product_name")
                    or record.get("brand_name")
                    or openfda.get("brand_name")
                ) or None,
                "active_ingredient": _first_text(
                    record.get("active_ingredient")
                    or record.get("substance_name")
                    or openfda.get("substance_name")
                    or openfda.get("generic_name")
                ) or None,
                "approval_date": text(
                    record.get("approval_date") or record.get("effective_time")
                ) or None,
                "label_section": bounded_label or None,
                "label_section_truncated": label_truncated,
            }
        )
    return record


def _mart_public_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    tool = text(record.get("tool"))
    render_data = mapping(record.get("render_data"))
    candidate: Mapping[str, Any] = render_data or record
    brand = text(candidate.get("anchor_brand") or candidate.get("brand"))

    if tool == "entity_bundle":
        bundle = mapping(record.get("entity_bundle"))
        members = mapping_list(bundle.get("members"))
        target = next(
            (
                member
                for member in members
                if text(member.get("role")).casefold() == "target"
            ),
            members[0] if members else {},
        )
        candidate = mapping(target.get("render_data"))
        brand = text(target.get("brand") or bundle.get("anchor"))
    elif mapping(candidate.get("ei_ms")):
        candidate = mapping(candidate.get("ei_ms"))
        brand = text(candidate.get("brand"))

    segment = next(
        (
            item
            for item in mapping_list(candidate.get("level_segments"))
            if text(item.get("brand")) == brand
        ),
        {},
    )
    sales = _first_present(candidate, "sales_krw", "brand_sales_krw", "value")
    market_share = _first_present(candidate, "ms_recent_pct")
    if market_share is None:
        market_share = _first_present(segment, "ms_recent_pct")
    return {
        key: value
        for key, value in {
            "brand": brand or None,
            "period": text(candidate.get("period")) or None,
            "sales_krw": sales,
            "market_share": market_share,
        }.items()
        if value not in (None, "")
    }


def _first_present(mapping_value: Mapping[str, Any], *keys: str) -> Any | None:
    return next(
        (mapping_value[key] for key in keys if mapping_value.get(key) is not None),
        None,
    )


def _publisher_from_url(url: str) -> str | None:
    host = (urlparse(url).hostname or "").casefold()
    return host.removeprefix("www.") or None


def _first_text(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return text(next((item for item in value if text(item)), ""))
    return text(value)


def _bounded_text(value: str, *, limit: int) -> tuple[str, bool]:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized, False
    return normalized[:limit].rstrip() + "...", True


def has_reimbursement(results: Sequence[SourceResult]) -> bool:
    return any(
        call.get("tool") == "hira_reimbursement_detail"
        for result in results
        for call in calls(result)
    )


def calls(result: SourceResult) -> list[dict[str, Any]]:
    payload = mapping(result.payload)
    source_calls = payload.get("calls")
    if not isinstance(source_calls, list):
        return []
    return [dict(call) for call in source_calls if isinstance(call, Mapping)]


def clinical_call_records(
    call: Mapping[str, Any],
    query: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], Mapping[str, Any]]:
    render_data = mapping(call.get("render_data"))
    detail = mapping(render_data.get("detail"))
    is_live_detail = bool(
        call.get("tool") == "clinicaltrials_study_details"
        and call.get("status") in {"live", "ok"}
        and detail
    )
    if not is_live_detail:
        payload = mapping(render_data.get("payload"))
        return (
            mapping_list(payload.get("studies")),
            dict(mapping(render_data.get("query_manifest"))),
            mapping(render_data.get("coverage")),
        )
    normalized = normalize_clinical_detail(
        detail,
        matched_queries=(query,),
        source_url=text(call.get("safe_url")),
    )
    records = [normalized] if text(normalized.get("nct_id")) else []
    return (
        records,
        {
            "query_id": query,
            "query_type": "nct_detail",
            "records_received": len(records),
            "records_unique": len(records),
            "pagination_complete": True,
        },
        {
            "total_reported": len(records),
            "records_received": len(records),
            "pagination_complete": True,
        },
    )


def result_failure(result: SourceResult) -> dict[str, Any]:
    return {
        "source": result.source,
        "query": result.query,
        "status": result.status,
        "notice": result.notice,
    }


def call_failure(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tool": text(call.get("tool")),
        "status": text(call.get("status")),
        "summary": text(call.get("summary_text")),
    }


def patent_evidence_id(lane: str, record: Mapping[str, Any], index: int) -> str:
    jurisdiction = {"kr_primary": "KR", "us_secondary": "US", "news": "NEWS"}[lane]
    visible = text(record.get("patent_no")) or text(record.get("url")) or str(index)
    return f"patent:{jurisdiction}:{visible}"


def dedupe_records(records: Iterable[EvidenceRecord]) -> list[EvidenceRecord]:
    unique: list[EvidenceRecord] = []
    seen: set[str] = set()
    for record in records:
        if record.evidence_id in seen:
            continue
        seen.add(record.evidence_id)
        unique.append(record)
    return unique


def result_refs(result: SourceResult) -> tuple[SourceReference, ...]:
    return tuple(
        SourceReference(url=citation.url)
        for citation in result.citations
        if citation.url
    )


def record_refs(record: Mapping[str, Any]) -> tuple[SourceReference, ...]:
    url = text(record.get("url") or record.get("source_url"))
    if not url:
        return ()
    return (
        SourceReference(
            url=url,
            title=_source_reference_title(record),
            published_at=text(record.get("published_at") or record.get("published_date")) or None,
        ),
    )


def _source_reference_title(record: Mapping[str, Any]) -> str | None:
    nct_id = text(record.get("nct_id") or record.get("nctId"))
    patent_no = text(record.get("patent_no") or record.get("patent_number"))
    title = text(
        record.get("title")
        or record.get("brief_title")
        or record.get("invention_title")
        or record.get("patent_title")
    )
    if nct_id:
        parts = (nct_id, title, text(record.get("overall_status") or record.get("status")))
    elif patent_no:
        parts = (
            patent_no,
            title,
            text(record.get("applicant") or record.get("owner") or record.get("assignee")),
            text(record.get("expiry_date") or record.get("expiration_date")),
            text(record.get("status")),
        )
    else:
        parts = (text(record.get("publisher")), title)
    bounded = tuple(part[:120].strip() for part in parts if part.strip())
    return " · ".join(dict.fromkeys(bounded)) or None


def dedupe_refs(groups: Iterable[Iterable[SourceReference]]) -> tuple[SourceReference, ...]:
    by_url: dict[str, SourceReference] = {}
    for group in groups:
        for ref in group:
            current = by_url.get(ref.url)
            if current is None or (not current.title and ref.title):
                by_url[ref.url] = ref
    return tuple(by_url.values())


def retrieved_at(results: Sequence[SourceResult], observed_on: date) -> str:
    dates = [citation.retrieved_at for result in results for citation in result.citations]
    if dates:
        return min(dates).astimezone(UTC).isoformat()
    return datetime(observed_on.year, observed_on.month, observed_on.day, tzinfo=UTC).isoformat()


def mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def mapping_list(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdigit() else None


def text(value: object) -> str:
    return str(value).strip() if value not in (None, "") else ""
