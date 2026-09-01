from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
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
    total_reported = 0
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
            call_total = optional_int(mapping(call.get("render_data")).get("totalCount"))
            total_reported += max(call_total or 0, len(call_records))
            for record_index, raw_record in enumerate(call_records, start=1):
                evidence_id = f"{source}:{result_index}:{call_index}:{record_index}"
                record = normalize_generic_record(source, raw_record)
                anchor_id = public_anchor_id(source, record)
                records.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        anchor_id=anchor_id,
                        source=source,
                        result_kind=f"{source}_record",
                        payload={**record, "evidence_id": evidence_id},
                        source_refs=record_refs(record, anchor_id=anchor_id),
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
            total_reported=max(total_reported, len(records)),
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
            if source == "hira":
                return _hira_call_records(call, render_data, records)
            if source == "mart":
                return [
                    _mart_record_with_context(record, render_data) for record in records
                ]
            return [dict(record) for record in records]
    if source == "web" and any(
        render_data.get(field) not in (None, "")
        for field in ("title", "url", "snippet", "summary", "content")
    ):
        return [dict(render_data)]
    if source == "mart" and render_data:
        series = mapping_list(render_data.get("brand_value_series_10pt"))
        if series:
            brand = text(render_data.get("brand") or render_data.get("anchor_brand"))
            records: list[dict[str, Any]] = []
            for point in series:
                record = _mart_record_with_context(point, render_data)
                if brand:
                    record.setdefault("brand", brand)
                measure = text(record.get("measure"))
                if measure.casefold() == "sales" or point.get("value_억원") not in (
                    None,
                    "",
                ):
                    sales_krw = _first_present(point, "value_krw", "sales_krw")
                    if sales_krw is None:
                        sales_krw = _eok_to_krw(point.get("value_억원"))
                    if sales_krw is not None:
                        record["sales_krw"] = sales_krw
                records.append(record)
            return records
        return [dict(render_data)]
    return [dict(call)]


def _mart_record_with_context(
    record: Mapping[str, Any], render_data: Mapping[str, Any]
) -> dict[str, Any]:
    enriched = dict(record)
    context = {
        "_source_identity": render_data.get("source") or render_data.get("source_name"),
        "brand": render_data.get("brand") or render_data.get("anchor_brand"),
        "metric": render_data.get("metric"),
        "measure": render_data.get("measure"),
        "market_id": render_data.get("market_id"),
        "market_name": render_data.get("market_name"),
        "view_type": render_data.get("view_type"),
        "unit_label": render_data.get("unit_label"),
    }
    for key, value in context.items():
        if value not in (None, ""):
            enriched.setdefault(key, value)
    return enriched


def _hira_call_records(
    call: Mapping[str, Any],
    render_data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    request = mapping(render_data.get("request"))
    total_count = optional_int(render_data.get("totalCount"))
    request_limit = optional_int(render_data.get("request_limit"))
    received_count = len(records)
    tool = text(call.get("tool"))
    expanded: list[dict[str, Any]] = []
    for raw_record in records:
        record = dict(raw_record)
        breakdown = mapping_list(record.get("sexBreakdown"))
        rows = breakdown or [record]
        parent = {key: value for key, value in record.items() if key != "sexBreakdown"}
        for row in rows:
            merged = {**parent, **dict(row)} if breakdown else dict(row)
            if request.get("year") not in (None, ""):
                merged.setdefault("year", request["year"])
            if request.get("sickCd") not in (None, ""):
                merged.setdefault("sickCd", request["sickCd"])
            merged.update(
                {
                    "_source_tool": tool,
                    "_source_total_count": total_count,
                    "_source_received_count": received_count,
                    "_source_request_limit": request_limit,
                }
            )
            expanded.append(merged)
    return expanded


def normalize_generic_record(source: str, raw_record: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(raw_record)
    if source == "mart":
        record.update(_mart_public_fields(record))
    elif source == "hira" and "ptntCnt" in record:
        record["patient_count"] = _hira_integer(record.get("ptntCnt"))
        units = record.get("units")
        if isinstance(units, Mapping) and isinstance(units.get("ptntCnt"), str):
            record["units"] = {**units, "patient_count": units["ptntCnt"]}
        if (
            isinstance(units, Mapping)
            and units.get("rvdInsupBrdnAmt") == "원"
            and record.get("rvdInsupBrdnAmt") not in (None, "")
        ):
            record["cost_krw"] = record["rvdInsupBrdnAmt"]
            record["units"] = {**record.get("units", {}), "cost_krw": "원"}
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
    elif source == "document":
        content, content_truncated = _bounded_text(text(record.get("content")), limit=4000)
        record.update(
            {
                "document_name": text(record.get("document_name")) or "업로드 문서",
                "section": text(record.get("section")) or None,
                "content": content or None,
                "content_truncated": content_truncated,
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
                    or record.get("title")
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


def _hira_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    normalized = str(value).strip().replace(",", "")
    if not normalized:
        return None
    try:
        parsed = Decimal(normalized)
    except InvalidOperation:
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _mart_public_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    tool = text(record.get("tool"))
    render_data = mapping(record.get("render_data"))
    candidate: Mapping[str, Any] = render_data or record
    brand = text(candidate.get("anchor_brand") or candidate.get("brand"))
    has_ei_ms_wrapper = bool(mapping(candidate.get("ei_ms")))

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
    measure = text(candidate.get("measure")).casefold()
    metric = text(candidate.get("metric")).casefold()
    unit_label = text(candidate.get("unit_label")).casefold()
    legacy_sales_wrapper = tool in {"entity_bundle", "cause_card_data"} or has_ei_ms_wrapper
    sales_compatible = (
        measure == "sales"
        or metric == "sales"
        or legacy_sales_wrapper
        or unit_label in {"krw", "억원"}
        or _first_present(candidate, "sales_krw", "brand_sales_krw", "value_krw")
        is not None
        or candidate.get("value_억원") not in (None, "")
    )
    share_compatible = (
        measure in {"share", "market_share"}
        or metric in {"share", "market_share"}
        or _first_present(candidate, "market_share", "ms_recent_pct", "ms_pct")
        is not None
        and not measure
        and not metric
    )
    sales = (
        _first_present(
            candidate,
            "sales_krw",
            "brand_sales_krw",
            "value_krw",
            "value",
        )
        if sales_compatible
        else None
    )
    market_share = (
        _first_present(
            candidate,
            "market_share",
            "ms_recent_pct",
            "ms_pct",
            *(("value",) if share_compatible else ()),
        )
        if sales_compatible or share_compatible
        else None
    )
    if market_share is None and (sales_compatible or share_compatible):
        market_share = _first_present(segment, "ms_recent_pct")
    return {
        key: value
        for key, value in {
            "brand": brand or None,
            "period": text(candidate.get("period")) or None,
            "market_id": text(candidate.get("market_id")) or None,
            "market_name": text(candidate.get("market_name")) or None,
            "view_type": text(candidate.get("view_type")) or None,
            "metric": text(candidate.get("metric")) or None,
            "measure": text(candidate.get("measure")) or None,
            "sales_krw": sales,
            "market_share": market_share,
        }.items()
        if value not in (None, "")
    }


def _eok_to_krw(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(Decimal(str(value)) * Decimal("100000000"))
    except (InvalidOperation, ValueError):
        return None


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
    if lane == "kr_primary":
        item_seq = text(record.get("product_item_seq") or record.get("ITEM_SEQ"))
        if item_seq:
            visible = f"{visible}:{item_seq}"
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


def record_refs(
    record: Mapping[str, Any],
    *,
    anchor_id: str | None = None,
) -> tuple[SourceReference, ...]:
    url = text(record.get("url") or record.get("source_url"))
    if not url:
        return ()
    return (
        SourceReference(
            url=url,
            title=_source_reference_title(record),
            published_at=text(record.get("published_at") or record.get("published_date")) or None,
            anchor_id=anchor_id,
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
    anchors_by_url: dict[str, set[str]] = {}
    for group in groups:
        for ref in group:
            if ref.anchor_id:
                anchors_by_url.setdefault(ref.url, set()).add(ref.anchor_id)
            current = by_url.get(ref.url)
            if current is None or (not current.title and ref.title):
                by_url[ref.url] = ref
    return tuple(
        ref.model_copy(
            update={
                "anchor_id": (
                    min(anchors_by_url.get(url, ()))
                    if len(anchors_by_url.get(url, ())) == 1
                    else None
                )
            }
        )
        for url, ref in by_url.items()
    )


def public_anchor_id(source: str, record: Mapping[str, Any]) -> str | None:
    """Return a stable semantic anchor, never one derived from list position."""
    if source == "nedrug":
        value = text(record.get("item_seq") or record.get("ITEM_SEQ"))
        return f"nedrug:{value}" if value else None
    if source == "web":
        value = text(record.get("url"))
        return _hashed_anchor("web", (value,)) if value else None
    if source == "mart":
        parts = (
            text(record.get("brand") or record.get("anchor_brand")),
            text(
                record.get("market_identifier")
                or record.get("market")
                or record.get("market_name")
                or record.get("atc4")
            ),
            text(record.get("period") or record.get("date")),
        )
        return _hashed_anchor("mart", parts) if parts[0] and parts[2] else None
    if source == "hira":
        parts = (
            text(record.get("sickCd") or record.get("sick_cd")),
            text(record.get("year") or record.get("period") or record.get("date")),
            text(record.get("sex") or record.get("gender")),
            text(record.get("age") or record.get("age_group")),
        )
        return _hashed_anchor("hira:stat", parts) if parts[0] and parts[1] else None
    if source == "document":
        parts = (
            text(record.get("document_name") or record.get("file_name")),
            text(record.get("page") or record.get("page_number") or record.get("slide")),
            text(record.get("chunk_id") or record.get("record_id")),
        )
        return _hashed_anchor("document", parts) if parts[0] and any(parts[1:]) else None
    if source == "openfda":
        parts = (
            text(record.get("application_number") or record.get("set_id") or record.get("id")),
            text(record.get("product_name") or record.get("brand_name")),
        )
        return _hashed_anchor("openfda", parts) if any(parts) else None
    return None


def patent_anchor_id(lane: str, record: Mapping[str, Any]) -> str | None:
    jurisdiction = {"kr_primary": "KR", "us_secondary": "US", "news": "NEWS"}[lane]
    patent_number = text(
        record.get("patent_no")
        or record.get("PATENT_NO")
        or record.get("patent_number")
    )
    if patent_number:
        if lane == "kr_primary":
            item_seq = text(record.get("product_item_seq") or record.get("ITEM_SEQ"))
            if item_seq:
                return f"patent:{jurisdiction}:{patent_number}:{item_seq}"
        return f"patent:{jurisdiction}:{patent_number}"
    if lane == "us_secondary":
        parts = (
            text(record.get("application_number")),
            text(record.get("product_number") or record.get("product_no")),
        )
        return _hashed_anchor("patent:US", parts) if all(parts) else None
    if lane == "news":
        url = text(record.get("url"))
        return _hashed_anchor("patent:NEWS", (url,)) if url else None
    return None


def _hashed_anchor(prefix: str, parts: Sequence[str]) -> str:
    canonical = "\x1f".join(part.casefold().strip() for part in parts)
    return f"{prefix}:{sha256(canonical.encode('utf-8')).hexdigest()[:20]}"


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
