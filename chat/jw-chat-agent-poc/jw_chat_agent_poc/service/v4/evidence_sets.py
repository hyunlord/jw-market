from __future__ import annotations

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
        for source in ("nedrug", "hira", "openfda", "clinicaltrials", "web", "patent")
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
            if manifest:
                manifests.append(manifest)
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
    records = tuple(
        EvidenceRecord(
            evidence_id=f"ct:{text(record.get('nct_id')).upper()}",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={**record, "evidence_id": f"ct:{text(record.get('nct_id')).upper()}"},
            source_refs=record_refs(record),
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


def _patent_set(results: Sequence[SourceResult], observed_on: date) -> EvidenceSet:
    records: list[EvidenceRecord] = []
    failures: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    partial_reasons: list[str] = []
    pagination_complete = True
    received = 0
    for result in results:
        if result.status != "ok":
            failures.append(result_failure(result))
        lanes = mapping(result.payload).get("patent_lanes")
        if not isinstance(lanes, Mapping):
            continue
        for lane_name in ("kr_primary", "us_secondary", "news"):
            lane = mapping(lanes.get(lane_name))
            received += optional_int(lane.get("records_received")) or 0
            source_limit = optional_int(lane.get("source_limit"))
            source_limit_reached = lane.get("source_limit_reached") is True
            identifier_exclusions = optional_int(lane.get("identifier_exclusions")) or 0
            product_patent_rows = optional_int(lane.get("product_patent_rows")) or 0
            non_product_exclusions = optional_int(lane.get("non_product_exclusions")) or 0
            manifests.append(
                {
                    "lane": lane_name,
                    "records_received": optional_int(lane.get("records_received")) or 0,
                    "records_unique": optional_int(lane.get("records_unique")) or 0,
                    "source_limit": source_limit,
                    "source_limit_reached": source_limit_reached,
                    "identifier_exclusions": identifier_exclusions,
                    "product_patent_rows": product_patent_rows,
                    "non_product_exclusions": non_product_exclusions,
                }
            )
            if source_limit_reached:
                pagination_complete = False
                partial_reasons.append(
                    f"{lane_name} 조회가 상류 호출 상한 {source_limit or '미상'}건에 도달"
                )
            for index, raw_record in enumerate(mapping_list(lane.get("records")), start=1):
                record = {**raw_record, "as_of_date": observed_on.isoformat()}
                evidence_id = patent_evidence_id(lane_name, record, index)
                result_kind = "web_document" if lane_name == "news" else "structured_patent_record"
                records.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        source="patent",
                        result_kind=result_kind,
                        payload={**record, "evidence_id": evidence_id},
                        source_refs=record_refs(record),
                    )
                )
    records = dedupe_records(records)
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
            records.append(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    source="hira",
                    result_kind="policy_document",
                    payload={**render_data, "evidence_id": evidence_id},
                    source_refs=record_refs(
                        {**render_data, "url": render_data.get("source_url") or call.get("safe_url")}
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
