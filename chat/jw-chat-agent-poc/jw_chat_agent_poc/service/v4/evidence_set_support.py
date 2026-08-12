from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from jw_chat_agent_poc.service.v4.clinical import normalize_clinical_detail
from jw_chat_agent_poc.service.v4.contracts import SourceResult
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
    SourceReference,
)


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
            if call.get("status") in {"error", "no_data", "unsupported"}:
                failures.append(call_failure(call))
            evidence_id = f"{source}:{result_index}:{call_index}"
            records.append(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    source=source,
                    result_kind="external_record",
                    payload={**call, "evidence_id": evidence_id},
                    source_refs=record_refs(call),
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
            title=text(record.get("title") or record.get("brief_title")) or None,
            published_at=text(record.get("published_at") or record.get("published_date")) or None,
        ),
    )


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
