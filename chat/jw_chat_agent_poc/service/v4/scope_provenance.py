from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any, Final

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceSet, RenderNode


LANES: Final = (
    "mart",
    "nedrug",
    "hira",
    "openfda",
    "clinicaltrials",
    "web",
    "patent",
    "document",
)
_AUTHORITY: Final = {
    "mart": "INTERNAL_MART",
    "nedrug": "MFDS_OFFICIAL_DATABASE",
    "hira": "HIRA_OFFICIAL_DOCUMENT",
    "openfda": "FDA_OFFICIAL_DATABASE",
    "clinicaltrials": "CLINICALTRIALS_OFFICIAL_DATABASE",
    "web": "UNKNOWN",
    "document": "UPLOADED_DOCUMENT",
}
_JURISDICTION: Final = {
    "mart": "N/A",
    "nedrug": "KR",
    "hira": "KR",
    "openfda": "US",
    "clinicaltrials": "GLOBAL",
    "web": "N/A",
    "document": "N/A",
}
_ENTITY_GRAIN: Final = {
    "mart": "brand_family",
    "nedrug": "product",
    "hira": "disease_code",
    "openfda": "product",
    "clinicaltrials": "ingredient_set",
    "web": "N/A",
    "document": "N/A",
}
_ENTITY_KEYS: Final = {
    "mart": ("brand", "brand_name", "product"),
    "nedrug": ("item_name", "ITEM_NAME", "ingredient"),
    "hira": ("disease_code", "product_name", "ingredient_name"),
    "openfda": ("product", "brand_name", "ingredient"),
    "clinicaltrials": ("interventions", "intervention_names", "condition", "nct_id"),
    "web": ("title",),
    "document": ("file_name", "document_name", "title"),
}
_STRENGTH_FORM_KEYS: Final = (
    "strength",
    "dosage",
    "form",
    "dosage_form",
    "SHAPE",
    "CONT_QY",
)
_EVENT_FIELDS: Final = {
    "clinicaltrials": {
        "study_first_submit_date": "CT_REGISTRATION_DATE",
        "start_date": "CT_START_DATE",
        "completion_date": "CT_COMPLETION_DATE",
        "last_update_posted_date": "CT_UPDATE_DATE",
    },
    "patent": {
        "listed_end_date": "PATENT_LISTED_END_DATE",
        "pms_period_start": "PMS_PERIOD_START",
        "pms_period_end": "PMS_PERIOD_END",
    },
    "nedrug": {"approval_date": "APPROVAL_DATE", "change_date": "APPROVAL_CHANGE_DATE"},
    "hira": {"notice_date": "NOTICE_DATE"},
    "web": {"published_at": "PUBLICATION_DATE", "event_date": "EVENT_DATE"},
}


class ProjectionInputError(ValueError):
    """Raised when required provenance cannot be projected without guessing."""


def build_scope_provenance_projection(
    evidence_sets: Sequence[EvidenceSet],
    render_nodes: Sequence[RenderNode],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    by_source = {item.source: item for item in evidence_sets}
    projected_by_id: dict[str, dict[str, Any]] = {}
    lanes: dict[str, dict[str, Any]] = {}
    patent_entities: set[str] = set()
    patent_edges: list[dict[str, str]] = []
    edge_seen: set[tuple[str, str]] = set()
    pms_periods: list[dict[str, str]] = []
    pms_seen: set[str] = set()
    input_errors: list[dict[str, str]] = []
    for source in LANES:
        evidence = by_source.get(source)
        coverage = _coverage(evidence)
        records = []
        for record in evidence.records if evidence is not None else ():
            projected = _project_record(source, record.evidence_id, record.payload, coverage)
            records.append(projected)
            projected_by_id[record.evidence_id] = projected
            if source == "patent" and projected["authority"] == "KR_LISTED_PATENT":
                item_seq = projected.get("product_item_seq")
                patent_no = projected.get("patent_no")
                if not item_seq:
                    input_errors.append(
                        {
                            "record_id": record.evidence_id,
                            "field": "ITEM_SEQ",
                            "reason": "required_official_edge_field_missing",
                        }
                    )
                if patent_no:
                    patent_entities.add(str(patent_no))
                    edge_key = (str(item_seq), str(patent_no))
                    if edge_key not in edge_seen:
                        edge_seen.add(edge_key)
                        patent_edges.append(
                            {"product_item_seq": edge_key[0], "patent_no": edge_key[1]}
                        )
                start = projected.get("pms_period_start")
                end = projected.get("pms_period_end")
                if start and end and item_seq not in pms_seen:
                    pms_seen.add(str(item_seq))
                    pms_periods.append(
                        {
                            "product_item_seq": str(item_seq),
                            "pms_period_start": str(start),
                            "pms_period_end": str(end),
                        }
                    )
        lanes[source] = {"coverage": coverage, "record_count": len(records), "records": records}
        if source == "patent" and evidence is not None:
            for manifest in evidence.query_manifest:
                for edge in manifest.get("product_patent_edges", ()):
                    if not isinstance(edge, Mapping):
                        continue
                    edge_key = (
                        _text(edge.get("product_item_seq")),
                        _text(edge.get("patent_no")),
                    )
                    if all(edge_key) and edge_key not in edge_seen:
                        edge_seen.add(edge_key)
                        patent_edges.append(
                            {"product_item_seq": edge_key[0], "patent_no": edge_key[1]}
                        )
                for period in manifest.get("pms_periods", ()):
                    if not isinstance(period, Mapping):
                        continue
                    item_seq = _text(period.get("product_item_seq"))
                    start = _text(period.get("pms_period_start"))
                    end = _text(period.get("pms_period_end"))
                    if item_seq and start and end and item_seq not in pms_seen:
                        pms_seen.add(item_seq)
                        pms_periods.append(
                            {
                                "product_item_seq": item_seq,
                                "pms_period_start": start,
                                "pms_period_end": end,
                            }
                        )
    projection = {
        "schema": "r13b.scope-provenance.v1",
        "status": "error" if input_errors else "ok",
        "input_errors": input_errors,
        "lanes": lanes,
        "patent_entity_count": len(patent_entities),
        "product_patent_edges": patent_edges,
        "pms_periods": pms_periods,
        "relations": _relation_compatibility(render_nodes, projected_by_id),
    }
    if strict:
        validate_scope_provenance_projection(projection)
    return projection


def validate_scope_provenance_projection(projection: Mapping[str, Any]) -> None:
    input_errors = projection.get("input_errors")
    if isinstance(input_errors, list) and input_errors:
        first = input_errors[0]
        if isinstance(first, Mapping):
            raise ProjectionInputError(
                f"{first.get('record_id', 'record')} is missing {first.get('field', 'required field')}"
            )
        raise ProjectionInputError("projection has input errors")
    lanes = projection.get("lanes")
    if not isinstance(lanes, Mapping):
        raise ProjectionInputError("projection lanes are missing")
    for source, lane in lanes.items():
        if not isinstance(lane, Mapping):
            raise ProjectionInputError(f"invalid lane projection: {source}")
        records = lane.get("records")
        if not isinstance(records, list):
            raise ProjectionInputError(f"lane records are missing: {source}")
        for record in records:
            if not isinstance(record, Mapping):
                raise ProjectionInputError(f"invalid projected record: {source}")
            if source == "mart" and record.get("deterministic_origin") != "CODE":
                raise ProjectionInputError("mart deterministic_origin must be CODE")
            for key in (
                "source",
                "authority",
                "jurisdiction",
                "canonical_entity",
                "entity_grain",
                "strength_form_scope",
                "event_semantics",
                "date_precision",
                "coverage",
                "deterministic_origin",
            ):
                if key not in record:
                    raise ProjectionInputError(f"projected record is missing {key}: {source}")
            if source == "patent" and "source_lane" not in record:
                raise ProjectionInputError("projected patent record is missing source_lane")


def _project_record(
    source: str,
    record_id: str,
    payload: Mapping[str, Any],
    coverage: str,
) -> dict[str, Any]:
    if source == "patent":
        patent_lane = _text(payload.get("lane"))
        authority = _text(payload.get("authority")) or {
            "kr_primary": "KR_LISTED_PATENT",
            "us_secondary": "US_ORANGE_BOOK",
            "news": "NEWS",
        }.get(patent_lane, "UNKNOWN")
        jurisdiction = _text(payload.get("jurisdiction")) or {
            "kr_primary": "KR",
            "us_secondary": "US",
            "news": "N/A",
        }.get(patent_lane, "UNKNOWN")
        grain = "product" if authority == "KR_LISTED_PATENT" else "ingredient_set"
        entity = _first_value(payload, ("product_item_name", "product", "ingredient"))
    else:
        authority = _AUTHORITY[source]
        jurisdiction = _JURISDICTION[source]
        grain = _ENTITY_GRAIN[source]
        entity = _first_value(payload, _ENTITY_KEYS[source])
    strength_form = _first_value(payload, _STRENGTH_FORM_KEYS)
    events = _events(source, payload)
    projected: dict[str, Any] = {
        "record_id": record_id,
        "source": source,
        "authority": authority,
        "jurisdiction": jurisdiction,
        "canonical_entity": {
            "value": entity or "UNKNOWN",
            "normalized_candidate": _normalize_candidate(entity) if entity else "UNKNOWN",
        },
        "entity_grain": grain,
        "strength_form_scope": {
            "status": "SPECIFIED" if strength_form else "UNKNOWN",
            "value": strength_form or "UNKNOWN",
        },
        "event_semantics": events,
        "date_precision": _combined_precision(events),
        "coverage": coverage,
        "deterministic_origin": "CODE",
    }
    if source == "patent":
        source_record = payload.get("source_record")
        raw = source_record if isinstance(source_record, Mapping) else {}
        pms_start, pms_end = _pms_period(
            payload.get("pms_period_start"),
            payload.get("pms_period_end"),
            raw.get("PMS_END_DATE"),
        )
        projected.update(
            {
                "patent_no": _text(payload.get("patent_no")) or "UNKNOWN",
                "product_item_seq": _text(payload.get("product_item_seq") or raw.get("ITEM_SEQ")),
                "product_item_name": _text(
                    payload.get("product_item_name") or raw.get("ITEM_NAME")
                ),
                "pms_period_start": pms_start,
                "pms_period_end": pms_end,
                "pms_status_as_of": _period_status(
                    pms_start,
                    pms_end,
                    _text(payload.get("as_of_date")),
                ),
                "listed_end_date": _text(
                    payload.get("listed_end_date")
                    or payload.get("expiration_date")
                    or raw.get("DOMESTIC_END_DATE")
                ),
                "event_type": _text(payload.get("event_type")) or "UNKNOWN",
                "source_lane": patent_lane,
            }
        )
    elif source == "mart":
        projected["market_definition_version"] = "UNKNOWN"
    elif source == "hira":
        projected["notice_date_semantics"] = "UNKNOWN"
    return projected


def _coverage(evidence: EvidenceSet | None) -> str:
    if evidence is None:
        return "not_connected"
    statuses = {
        _text(item.get("status"))
        for item in evidence.item_failures
        if isinstance(item, Mapping)
    }
    if not evidence.records and statuses & {"timeout", "deadline_exceeded"}:
        return "timeout"
    if not evidence.coverage.pagination_complete:
        return "truncated"
    if evidence.coverage.partial_reasons or evidence.item_failures:
        return "partial"
    return "complete"


def _events(source: str, payload: Mapping[str, Any]) -> list[dict[str, str]]:
    fields = _EVENT_FIELDS.get(source, {})
    events = []
    for field, semantics in fields.items():
        value = _text(payload.get(field))
        if value:
            events.append(
                {
                    "field": field,
                    "semantics": semantics,
                    "value": value,
                    "date_precision": _date_precision(value),
                }
            )
    return events or [
        {"field": "UNKNOWN", "semantics": "UNKNOWN", "value": "UNKNOWN", "date_precision": "unknown"}
    ]


def _relation_compatibility(
    render_nodes: Sequence[RenderNode],
    projected_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    relations = []
    for node in render_nodes:
        if node.block_id not in {
            "narrative:cross-record-relations",
            "narrative:cross-source-fusion",
        }:
            continue
        operands = [projected_by_id[item] for item in node.record_ids if item in projected_by_id]
        reasons: list[str] = []
        missing_record_ids = [item for item in node.record_ids if item not in projected_by_id]
        if missing_record_ids:
            reasons.append("unresolved_operand")
        jurisdictions = {
            str(item["jurisdiction"])
            for item in operands
            if item["jurisdiction"] not in {"GLOBAL", "N/A", "UNKNOWN"}
        }
        if len(jurisdictions) > 1:
            reasons.append("jurisdiction")
        grains = {
            str(item["entity_grain"])
            for item in operands
            if item["entity_grain"] not in {"N/A", "UNKNOWN"}
        }
        if len(grains) > 1:
            reasons.append("entity_grain")
        entities = {
            str(item["canonical_entity"]["normalized_candidate"])
            for item in operands
            if item["canonical_entity"]["normalized_candidate"] != "UNKNOWN"
        }
        if len(entities) > 1:
            reasons.append("canonical_entity")
        relations.append(
            {
                "block_id": node.block_id,
                "record_ids": list(node.record_ids),
                "resolved_operand_count": len(operands),
                "unresolved_record_ids": missing_record_ids,
                "compatibility": "INCOMPATIBLE" if reasons else "COMPATIBLE",
                "reasons": reasons,
            }
        )
    return relations


def _first_value(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            joined = ", ".join(_text(item) for item in value if _text(item))
            if joined:
                return joined
        elif _text(value):
            return _text(value)
    return ""


def _normalize_candidate(value: str) -> str:
    return " ".join(value.casefold().split())


def _date_precision(value: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}\s*[~～]\s*\d{4}-\d{2}-\d{2}", value):
        return "range"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "day"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return "month"
    if re.fullmatch(r"\d{4}", value):
        return "year"
    return "unknown"


def _combined_precision(events: Sequence[Mapping[str, str]]) -> str:
    values = {item["date_precision"] for item in events}
    return next(iter(values)) if len(values) == 1 else "range" if "range" in values else "unknown"


def _pms_period(start: Any, end: Any, raw: Any) -> tuple[str, str]:
    start_text, end_text = _text(start), _text(end)
    if start_text and end_text:
        return start_text, end_text
    match = re.fullmatch(
        r"\s*(\d{4}-\d{2}-\d{2})\s*[~～]\s*(\d{4}-\d{2}-\d{2})\s*",
        _text(raw),
    )
    return match.groups() if match else ("", "")


def _period_status(start: str, end: str, as_of: str) -> str:
    if not start or not end or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        return "UNKNOWN"
    if as_of < start:
        return "NOT_STARTED"
    if as_of <= end:
        return "IN_PROGRESS"
    return "ENDED"


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


__all__ = [
    "ProjectionInputError",
    "build_scope_provenance_projection",
    "validate_scope_provenance_projection",
]
