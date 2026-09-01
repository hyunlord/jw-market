from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from jw_chat_agent_poc.service.v4.clinical import merge_clinical_searches
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.evidence_set_support import (
    call_failure,
    calls,
    clinical_call_records,
    dedupe_records,
    dedupe_refs,
    generic_evidence_set,
    has_reimbursement,
    mapping,
    mapping_list,
    optional_int,
    patent_anchor_id,
    patent_evidence_id,
    record_refs,
    result_failure,
    result_refs,
    retrieved_at,
    text,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
)


def build_evidence_sets(
    plan: PlannerOutput,
    results: Sequence[SourceResult],
    *,
    observed_on: date,
) -> tuple[EvidenceSet, ...]:
    grouped = {
        source: tuple(result for result in results if result.source == source)
        for source in (
            "mart", "nedrug", "hira", "openfda", "clinicaltrials", "web", "patent", "document", "prior_turn"
        )
    }
    built: list[EvidenceSet] = []
    for source, source_results in grouped.items():
        if not source_results:
            continue
        if source == "clinicaltrials":
            built.append(_clinical_set(source_results, observed_on))
        elif source == "patent":
            built.append(_patent_set(source_results, observed_on))
        elif source == "hira" and has_reimbursement(source_results):
            built.append(_policy_set(source_results, observed_on))
        else:
            built.append(generic_evidence_set(source, source_results, observed_on))
    return tuple(built)


def _clinical_set(results: Sequence[SourceResult], observed_on: date) -> EvidenceSet:
    searches: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    total_reported = 0
    after_status = 0
    received = 0
    unique_before_relevance_ids: set[str] = set()
    excluded_by_status = 0
    excluded_by_relevance = 0
    all_totals_known = True
    all_after_status_known = True
    all_status_exclusions_known = True
    pagination_complete = True
    partial_reasons: list[str] = []
    query_spec: list[str] = []
    counted_query_ids: set[str] = set()
    query_breakdown: list[dict[str, Any]] = []

    for result in results:
        query_spec.append(result.query)
        if result.status != "ok":
            failures.append(result_failure(result))
        source_calls = calls(result)
        if not source_calls:
            all_totals_known = False
            pagination_complete = False
            partial_reasons.append(
                result.notice or f"{result.source} result status={result.status}"
            )
        for call in source_calls:
            records, manifest, coverage = clinical_call_records(call, result.query)
            query_id = text(manifest.get("query_id"))
            raw_source_queries = manifest.get("source_queries")
            source_queries = (
                [text(value) for value in raw_source_queries if text(value)]
                if isinstance(raw_source_queries, (list, tuple))
                else []
            )
            matched_queries = source_queries or ([result.query] if result.query else [])
            if matched_queries:
                for record in records:
                    existing = record.get("matched_query")
                    existing_queries = (
                        [existing]
                        if isinstance(existing, str)
                        else list(existing)
                        if isinstance(existing, (list, tuple))
                        else []
                    )
                    record["matched_query"] = list(
                        dict.fromkeys((*existing_queries, *matched_queries))
                    )
            duplicate_query = bool(query_id and query_id in counted_query_ids)
            if manifest and not duplicate_query:
                manifests.append(manifest)
                if query_id:
                    counted_query_ids.add(query_id)
                query_breakdown.append(
                    _clinical_query_breakdown(
                        manifest,
                        coverage,
                        records,
                        fallback_query=result.query,
                        call_status=text(call.get("status")) or result.status,
                    )
                )
            searches.append({"query_manifest": manifest, "records": records})
            unique_before_relevance_ids.update(
                text(record.get("nct_id")).upper()
                for record in records
                if text(record.get("nct_id"))
            )
            unique_before_relevance_ids.update(
                text(exclusion.get("nct_id")).upper()
                for exclusion in mapping_list(manifest.get("relevance_exclusions"))
                if text(exclusion.get("nct_id"))
            )
            if duplicate_query:
                continue
            call_total = optional_int(coverage.get("total_reported"))
            if call_total is None:
                all_totals_known = False
            else:
                total_reported += call_total
            call_after_status = optional_int(coverage.get("records_after_status_filter"))
            if call_after_status is None:
                all_after_status_known = False
            else:
                after_status += call_after_status
            call_received = optional_int(coverage.get("records_received"))
            received += call_received if call_received is not None else len(records)
            call_excluded_status = optional_int(
                coverage.get("records_excluded_by_status")
            )
            if call_excluded_status is None:
                all_status_exclusions_known = False
            else:
                excluded_by_status += call_excluded_status
            call_excluded_relevance = optional_int(
                coverage.get("records_excluded_by_relevance")
            )
            excluded_by_relevance += call_excluded_relevance or 0
            complete = coverage.get("pagination_complete") is True
            pagination_complete = pagination_complete and complete
            reason = text(coverage.get("partial_reason"))
            if reason:
                partial_reasons.append(reason)
            if call.get("status") in {"error", "no_data", "unsupported", "timeout"}:
                failures.append(call_failure(call))

    merged = merge_clinical_searches(searches)
    if len(query_breakdown) > 1:
        per_query_unique = sum(
            int(item.get("records_unique") or 0) for item in query_breakdown
        )
        manifests.append(
            {
                "lane": "query_breakdown",
                "global": {
                    "records_received": received,
                    "records_unique": len(merged),
                    "cross_query_duplicates_removed": max(
                        per_query_unique - len(merged), 0
                    ),
                },
                "by_query": query_breakdown,
                "caption": (
                    "질의별 건수는 원 건수이며, global은 질의 간 중복을 제거한 값입니다."
                ),
            }
        )
    records = tuple(
        EvidenceRecord(
            evidence_id=f"ct:{text(record.get('nct_id')).upper()}",
            anchor_id=f"ct:{text(record.get('nct_id')).upper()}",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={**record, "evidence_id": f"ct:{text(record.get('nct_id')).upper()}"},
            source_refs=record_refs(
                record,
                anchor_id=f"ct:{text(record.get('nct_id')).upper()}",
            ),
        )
        for record in merged
        if text(record.get("nct_id"))
    )
    unique_records = dedupe_records(records)
    refs = dedupe_refs(
        [
            *(result_refs(result) for result in results),
            *(record.source_refs for record in unique_records),
        ]
    )
    coverage = CoverageLedger(
        total_reported=total_reported if all_totals_known else None,
        records_after_status_filter=(
            after_status if all_after_status_known else None
        ),
        records_received=received,
        records_unique=len(unique_before_relevance_ids),
        records_relevant=len(unique_records),
        records_excluded_by_status=(
            excluded_by_status if all_status_exclusions_known else None
        ),
        records_excluded_by_relevance=excluded_by_relevance,
        pagination_complete=pagination_complete,
        partial_reasons=tuple(dict.fromkeys(partial_reasons)),
    )
    return EvidenceSet(
        source="clinicaltrials",
        query_spec=tuple(dict.fromkeys(query_spec)),
        query_manifest=tuple(manifests),
        retrieved_at=retrieved_at(results, observed_on),
        coverage=coverage,
        records=unique_records,
        item_failures=tuple(failures),
        source_refs=refs,
    )


def _clinical_query_breakdown(
    manifest: Mapping[str, Any],
    coverage: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    fallback_query: str,
    call_status: str,
) -> dict[str, Any]:
    source_queries = manifest.get("source_queries")
    query = next(
        (
            text(value)
            for value in source_queries
            if text(value)
        ),
        text(manifest.get("compiled_expression"))
        or text(manifest.get("query_id"))
        or fallback_query,
    ) if isinstance(source_queries, (list, tuple)) else (
        text(manifest.get("compiled_expression"))
        or text(manifest.get("query_id"))
        or fallback_query
    )
    status_distribution = Counter(
        text(record.get("overall_status")) or text(record.get("status")) or "UNKNOWN"
        for record in records
    )
    phase_distribution: Counter[str] = Counter()
    sponsor_distribution: Counter[str] = Counter()
    for record in records:
        raw_phases = record.get("phases") or record.get("phase") or ()
        phases = (raw_phases,) if isinstance(raw_phases, str) else raw_phases
        if isinstance(phases, (list, tuple)):
            phase_distribution.update(text(value) for value in phases if text(value))
        sponsor = text(record.get("lead_sponsor")) or text(record.get("sponsor"))
        if sponsor:
            sponsor_distribution[sponsor] += 1
    unique_ids = {
        text(record.get("nct_id")).upper()
        for record in records
        if text(record.get("nct_id"))
    }
    direct = optional_int(manifest.get("records_direct_relevance_confirmed"))
    if direct is None:
        direct = sum(
            "직접 관련 확인" in text(record.get("relevance_status"))
            for record in records
        )
    received_count = optional_int(coverage.get("records_received"))
    return {
        "query": query,
        "expansion_grade": text(manifest.get("expansion_grade")) or "unspecified",
        "status": call_status or "unknown",
        "records_received": received_count if received_count is not None else len(records),
        "records_direct_related": direct,
        "records_unique": len(unique_ids),
        "status_distribution": dict(sorted(status_distribution.items())),
        "phase_distribution": dict(sorted(phase_distribution.items())),
        "sponsor_distribution": dict(sorted(sponsor_distribution.items())),
    }


def _patent_set(results: Sequence[SourceResult], observed_on: date) -> EvidenceSet:
    records: list[EvidenceRecord] = []
    failures: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    partial_reasons: list[str] = []
    pagination_complete = True
    received = 0
    counted_lane_results: set[str] = set()
    for result in results:
        if result.status != "ok":
            failures.append(result_failure(result))
        lanes = mapping(result.payload).get("patent_lanes")
        if not isinstance(lanes, Mapping):
            continue
        inspection_displayed_count = _patent_inspection_displayed_count(result)
        for lane_name in ("kr_primary", "us_secondary", "news"):
            lane = mapping(lanes.get(lane_name))
            result_fingerprint = _patent_lane_result_fingerprint(lane_name, lane)
            first_physical_result = result_fingerprint not in counted_lane_results
            if first_physical_result:
                counted_lane_results.add(result_fingerprint)
                received += optional_int(lane.get("records_received")) or 0
            source_limit = optional_int(lane.get("source_limit"))
            source_limit_reached = lane.get("source_limit_reached") is True
            identifier_exclusions = optional_int(lane.get("identifier_exclusions")) or 0
            product_patent_rows = optional_int(lane.get("product_patent_rows")) or 0
            non_product_exclusions = optional_int(lane.get("non_product_exclusions")) or 0
            manifest = {
                    "lane": lane_name,
                    "result_fingerprint": result_fingerprint,
                    "records_received": optional_int(lane.get("records_received")) or 0,
                    "records_unique": optional_int(lane.get("records_unique")) or 0,
                    "source_limit": source_limit,
                    "source_limit_reached": source_limit_reached,
                    "identifier_exclusions": identifier_exclusions,
                    "product_patent_rows": product_patent_rows,
                    "non_product_exclusions": non_product_exclusions,
                    "product_patent_edges": list(
                        lane.get("product_patent_edges")
                        if isinstance(lane.get("product_patent_edges"), list)
                        else []
                    ),
                    "pms_periods": list(
                        lane.get("pms_periods")
                        if isinstance(lane.get("pms_periods"), list)
                        else []
                    ),
                }
            for key in (
                "other_patent_rows",
                "product_records_unique",
                "product_patent_number_count",
                "product_item_patent_count",
                "missing_item_seq_fallback_count",
                "page_group_counts",
                "patent_type_counts",
                "patent_type_denominator",
                "pms_period_format_counts",
                "brand_scope_applied",
                "required_brand",
                "brand_scope_relevant_count",
                "brand_scope_excluded_count",
            ):
                if key in lane:
                    manifest[key] = lane[key]
            if lane_name == "kr_primary" and inspection_displayed_count is not None:
                manifest["inspection_displayed_count"] = inspection_displayed_count
            manifests.append(manifest)
            if source_limit_reached and first_physical_result:
                pagination_complete = False
                partial_reasons.append(
                    f"{lane_name} 조회가 상류 호출 상한 {source_limit or '미상'}건에 도달"
                )
            for index, raw_record in enumerate(mapping_list(lane.get("records")), start=1):
                record = {**raw_record, "as_of_date": observed_on.isoformat()}
                evidence_id = patent_evidence_id(lane_name, record, index)
                anchor_id = patent_anchor_id(lane_name, record)
                result_kind = "web_document" if lane_name == "news" else "structured_patent_record"
                records.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        anchor_id=anchor_id,
                        source="patent",
                        result_kind=result_kind,
                        payload={**record, "evidence_id": evidence_id},
                        source_refs=record_refs(record, anchor_id=anchor_id),
                    )
                )
    records = dedupe_records(records)
    canonical_product_identities = [
        {
            "evidence_id": record.evidence_id,
            "patent_no": str(record.payload.get("patent_no") or ""),
            "product_item_seq": str(record.payload.get("product_item_seq") or ""),
        }
        for record in records
        if record.payload.get("lane") == "kr_primary"
        and record.payload.get("page_group") == "제품특허"
    ]
    for manifest in manifests:
        if manifest.get("lane") == "kr_primary":
            manifest["canonical_product_identities"] = canonical_product_identities
            break
    refs = dedupe_refs(
        [
            *(result_refs(result) for result in results),
            *(record.source_refs for record in records),
        ]
    )
    return EvidenceSet(
        source="patent",
        query_spec=tuple(dict.fromkeys(result.query for result in results)),
        query_manifest=tuple(manifests),
        retrieved_at=retrieved_at(results, observed_on),
        coverage=CoverageLedger(
            total_reported=received,
            records_received=received,
            records_unique=len(records),
            pagination_complete=pagination_complete,
            partial_reasons=tuple(dict.fromkeys(partial_reasons)),
        ),
        records=tuple(records),
        item_failures=tuple(failures),
        source_refs=refs,
    )


def _patent_lane_result_fingerprint(
    lane_name: str,
    lane: Mapping[str, Any],
) -> str:
    canonical = json.dumps(
        {"lane": lane_name, "payload": lane},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _patent_inspection_displayed_count(result: SourceResult) -> int | None:
    from jw_chat_agent_poc.service.v4.inspection import _inspection_output, _raw_records

    raw_records = _raw_records(result.payload)
    if not raw_records:
        return None
    output, _metrics = _inspection_output(
        raw_records,
        len(raw_records),
        source="patent",
    )
    return int(output["displayed_record_count"])


def _policy_set(results: Sequence[SourceResult], observed_on: date) -> EvidenceSet:
    records: list[EvidenceRecord] = []
    failures: list[dict[str, Any]] = []
    for result in results:
        if result.status != "ok":
            failures.append(result_failure(result))
        for call_index, call in enumerate(calls(result), start=1):
            if call.get("tool") != "hira_reimbursement_detail":
                continue
            render_data = dict(mapping(call.get("render_data")))
            identifier = (
                text(render_data.get("notice_number"))
                or text(render_data.get("source_notice_id"))
                or str(call_index)
            )
            evidence_id = f"hira:notice:{identifier}"
            stable_identifier = text(
                render_data.get("notice_number") or render_data.get("source_notice_id")
            )
            anchor_id = f"hira:notice:{stable_identifier}" if stable_identifier else None
            records.append(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    anchor_id=anchor_id,
                    source="hira",
                    result_kind="policy_document",
                    payload={**render_data, "evidence_id": evidence_id},
                    source_refs=record_refs(
                        {
                            **render_data,
                            "url": render_data.get("source_url") or call.get("safe_url"),
                        },
                        anchor_id=anchor_id,
                    ),
                )
            )
    unique_records = dedupe_records(records)
    refs = dedupe_refs(
        [
            *(result_refs(result) for result in results),
            *(record.source_refs for record in unique_records),
        ]
    )
    return EvidenceSet(
        source="hira",
        query_spec=tuple(dict.fromkeys(result.query for result in results)),
        retrieved_at=retrieved_at(results, observed_on),
        coverage=CoverageLedger(
            total_reported=len(records),
            records_received=len(records),
            records_unique=len(unique_records),
        ),
        records=tuple(unique_records),
        item_failures=tuple(failures),
        source_refs=refs,
    )
