from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jw_chat_agent_poc.service.v4.fact_digest import FactDigest
from jw_chat_agent_poc.service.v4.insight_claim_verifier import (
    claim_identifiers,
    filter_group_evidence_ids,
    text_identifiers,
    verify_structured_claims,
)
from jw_chat_agent_poc.service.v4.source_labels import SOURCE_LABELS

_PROMPT_EVIDENCE_IDS_PER_CARD = 40


class ClaimType(StrEnum):
    CITE = "CITE"
    CALC = "CALC"
    OBS = "OBS"
    INTERP = "INTERP"
    HYPO = "HYPO"


class ClaimHedge(StrEnum):
    NONE = "none"
    SOFTENED = "softened"
    HYPOTHESIS = "hypothesis"


class ClaimSection(StrEnum):
    INSIGHT = "insight"
    FACTS = "facts"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InsightClaim(_FrozenModel):
    section: ClaimSection = ClaimSection.INSIGHT
    text: str = Field(min_length=1)
    claim_type: ClaimType
    evidence_ids: tuple[str, ...]
    hedge: ClaimHedge
    answers: tuple[str, ...] = ()


class ClaimEnvelope(_FrozenModel):
    claims: tuple[InsightClaim, ...] = Field(min_length=1)


class ClaimPayloadError(ValueError):
    """A structured insight payload violates the IG-3 contract."""

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details = details or {"reason_code": "payload_error"}


@dataclass(frozen=True)
class ParsedClaims:
    text: str
    insight_text: str
    facts_text: str
    claims: tuple[InsightClaim, ...]
    manifest: dict[str, object]


_JSON_FENCE_RE = re.compile(r"```(?:json)?|```", flags=re.IGNORECASE)
_MALFORMED_PROBABILITY_SUFFIX = "했습니다일 가능성"
_EVIDENCE_REQUIRED = frozenset(
    {ClaimType.CITE, ClaimType.CALC, ClaimType.OBS}
)
_QUARANTINE_MIN_SURVIVING_CLAIMS = 10


@dataclass(frozen=True)
class _DecodedClaims:
    claims: tuple[object, ...]
    mode: str


def evidence_id_catalog(digest: FactDigest) -> tuple[str, ...]:
    identifiers: dict[str, None] = {}
    for identifier in digest.visible_record_ids:
        if identifier:
            identifiers.setdefault(identifier, None)
    for card in digest.cards:
        for identifier in card.evidence_ids:
            if identifier:
                identifiers.setdefault(identifier, None)
    for metric in digest.derived_metrics:
        identifiers.setdefault(metric.id, None)
        for identifier in metric.inputs:
            if identifier and identifier in identifiers:
                identifiers.setdefault(identifier, None)
    return tuple(identifiers)


def evidence_catalog_payload(digest: FactDigest) -> list[dict[str, object]]:
    catalog: list[dict[str, object]] = []
    for index, card in enumerate(digest.cards, start=1):
        evidence_ids = tuple(dict.fromkeys(card.evidence_ids))
        catalog.append(
            {
                "kind": "fact_card",
                "card_index": index,
                "source": card.source,
                "entity": card.entity,
                "evidence_ids": list(evidence_ids[:_PROMPT_EVIDENCE_IDS_PER_CARD]),
                "evidence_id_count": len(evidence_ids),
                "evidence_ids_omitted": max(
                    0, len(evidence_ids) - _PROMPT_EVIDENCE_IDS_PER_CARD
                ),
            }
        )
    for metric in digest.derived_metrics:
        catalog.append(
            {
                "kind": "derived_metric",
                "id": metric.id,
                "metric_type": metric.type,
                "entity": metric.entity,
                "inputs": list(metric.inputs),
            }
        )
    return catalog


def parse_claim_payload(
    raw: str,
    digest: FactDigest,
    *,
    supplement_missing: bool = True,
) -> ParsedClaims:
    decoded = _decode_claim_objects(raw)
    allowed_ids = frozenset(evidence_id_catalog(digest))
    empty_evidence_reasons: list[dict[str, object]] = []
    manifest_claims: list[dict[str, object]] = []
    quarantined: list[dict[str, object]] = []
    accepted: list[tuple[int, InsightClaim]] = []

    for index, raw_claim in enumerate(decoded.claims, start=1):
        try:
            claim = InsightClaim.model_validate(raw_claim)
        except ValidationError as exc:
            quarantined.append(_schema_quarantine(index, raw_claim, exc))
            continue
        if claim.claim_type in _EVIDENCE_REQUIRED and not claim.evidence_ids:
            quarantined.append(
                _quarantine_entry(
                    index,
                    raw_claim,
                    failed_field="evidence_ids",
                    reason_code="missing_evidence_ids",
                    message=f"claim {index} ({claim.claim_type.value}) requires evidence_ids",
                )
            )
            continue
        unknown_ids = tuple(
            identifier
            for identifier in claim.evidence_ids
            if identifier not in allowed_ids
        )
        if unknown_ids:
            quarantined.append(
                _quarantine_entry(
                    index,
                    raw_claim,
                    failed_field="evidence_ids",
                    reason_code="unknown_evidence_id",
                    message=(
                        f"claim {index} references unknown evidence id(s): "
                        + ", ".join(unknown_ids)
                    ),
                )
            )
            continue
        if not claim.evidence_ids:
            empty_evidence_reasons.append(
                {
                    "claim_index": index,
                    "claim_type": claim.claim_type.value,
                    "reason": "allowed_by_contract",
                }
            )
        accepted.append((index, claim))
        manifest_claims.append(
            {
                "claim_index": index,
                "section": claim.section.value,
                "claim_type": claim.claim_type.value,
                "evidence_ids": list(claim.evidence_ids),
                "hedge": claim.hedge.value,
                "answers": list(claim.answers),
            }
        )

    quarantine_manifest: dict[str, object] = {
        "count": len(quarantined),
        "minimum_surviving_claims": _QUARANTINE_MIN_SURVIVING_CLAIMS,
        "claims": quarantined,
    }
    if quarantined and len(accepted) < _QUARANTINE_MIN_SURVIVING_CLAIMS:
        first_error = str(quarantined[0]["message"])
        details: dict[str, object] = {
            "reason_code": "quarantine_minimum_not_met",
            "surviving_claim_count": len(accepted),
            "input_claim_count": len(decoded.claims),
            "decode_mode": decoded.mode,
            "quarantine": quarantine_manifest,
        }
        raise ClaimPayloadError(
            "structured claims below quarantine minimum "
            f"({len(accepted)} < {_QUARANTINE_MIN_SURVIVING_CLAIMS}); "
            f"first error: {first_error}",
            details=details,
        )
    if not accepted:
        raise ClaimPayloadError(
            "structured insight output has no valid claims",
            details={
                "reason_code": "no_valid_claims",
                "input_claim_count": len(decoded.claims),
                "decode_mode": decoded.mode,
                "quarantine": quarantine_manifest,
            },
        )

    accepted_claims = tuple(claim for _index, claim in accepted)
    verified = verify_structured_claims(accepted_claims, digest)
    relocated: list[InsightClaim] = []
    relocation_count = 0
    for claim in verified.claims:
        if claim.section is ClaimSection.FACTS and claim.claim_type in {
            ClaimType.INTERP,
            ClaimType.HYPO,
        }:
            relocated.append(claim.model_copy(update={"section": ClaimSection.INSIGHT}))
            relocation_count += 1
        else:
            relocated.append(claim)
    routed_claims = tuple(relocated)
    insight_claims = tuple(
        claim for claim in routed_claims if claim.section is ClaimSection.INSIGHT
    )
    facts_claims = tuple(
        claim for claim in routed_claims if claim.section is ClaimSection.FACTS
    )
    insight_text = _assemble_insight_body(insight_claims)
    facts_text = " ".join(_normalize_claim_suffix(claim.text) for claim in facts_claims)
    facts_text, required_item_coverage = ensure_required_item_coverage(
        facts_text,
        routed_claims,
        digest,
        supplement_missing=supplement_missing,
    )
    section_paragraphs = _section_paragraphs(
        insight_claims=insight_claims,
        facts_claims=facts_claims,
        facts_text=facts_text,
        digest=digest,
    )
    paragraph_evidence = {
        section: {
            "total": len(paragraphs),
            "sourced": sum(not paragraph["unsourced"] for paragraph in paragraphs),
            "unsourced": sum(bool(paragraph["unsourced"]) for paragraph in paragraphs),
        }
        for section, paragraphs in section_paragraphs.items()
    }
    claim_evidence = {
        section: {
            **counts,
            "over_limit": sum(
                int(paragraph["evidence_id_count"]) > 2
                for paragraph in section_paragraphs[section]
            ),
        }
        for section, counts in paragraph_evidence.items()
    }
    group_domain_check: Counter[str] = Counter()
    evidence_supply: Counter[str] = Counter()
    for paragraphs in section_paragraphs.values():
        for paragraph in paragraphs:
            group_domain_check.update(paragraph.get("group_domain_check", {}))
            evidence_supply.update(paragraph.get("evidence_supply", {}))
    type_counts = Counter(claim.claim_type.value for claim in routed_claims)
    return ParsedClaims(
        text=f"## 종합 인사이트\n{insight_text}".rstrip(),
        insight_text=insight_text,
        facts_text=facts_text,
        claims=routed_claims,
        manifest={
            "parse_status": "parsed",
            "decode_mode": decoded.mode,
            "input_claim_count": len(decoded.claims),
            "claim_count": len(accepted_claims),
            "type_counts": dict(sorted(type_counts.items())),
            "referenced_evidence_ids": sorted(
                {
                    identifier
                    for claim in accepted_claims
                    for identifier in claim.evidence_ids
                }
            ),
            "empty_evidence_reasons": empty_evidence_reasons,
            "quarantine": quarantine_manifest,
            "claims": manifest_claims,
            "verification": verified.manifest,
            "section_relocation_count": relocation_count,
            "section_counts": {
                "facts": len(facts_claims),
                "insight": len(insight_claims),
            },
            "required_item_coverage": required_item_coverage,
            "section_paragraphs": section_paragraphs,
            "paragraph_evidence": paragraph_evidence,
            "claim_evidence": claim_evidence,
            "group_domain_check": dict(group_domain_check),
            "evidence_supply": dict(evidence_supply),
        },
    )


def ensure_required_item_coverage(
    facts_text: str,
    claims: tuple[InsightClaim, ...],
    digest: FactDigest,
    *,
    supplement_missing: bool = True,
) -> tuple[str, dict[str, object]]:
    contract = digest.answer_contract
    required_items = contract.required_items if contract is not None else ()
    required_ids = tuple(item.id for item in required_items)
    required_id_set = frozenset(required_ids)
    reported_ids = tuple(
        dict.fromkeys(
            answer_id
            for claim in claims
            if claim.section is ClaimSection.FACTS
            for answer_id in claim.answers
        )
    )
    grounded_ids = tuple(
        item.id
        for item in required_items
        if item.id not in reported_ids and _required_data_is_grounded(item, facts_text, digest)
    )
    covered_ids = tuple(
        answer_id
        for answer_id in required_ids
        if answer_id in reported_ids or answer_id in grounded_ids
    )
    missing_items = tuple(item for item in required_items if item.id not in covered_ids)
    supplements = tuple(
        f"{item.ask.rstrip('?. ')}에 대해서는 수신 근거에서 확인되지 않았습니다."
        for item in missing_items
    ) if supplement_missing else ()
    completed = " ".join(part for part in (facts_text.strip(), *supplements) if part)
    return completed, {
        "required_count": len(required_items),
        "covered_count": len(covered_ids),
        "covered_ids": list(covered_ids),
        "reported_ids": list(reported_ids),
        "grounded_ids": list(grounded_ids),
        "missing_ids": [item.id for item in missing_items],
        "supplement_count": len(supplements),
        "unknown_answer_ids": [
            answer_id for answer_id in reported_ids if answer_id not in required_id_set
        ],
        "degraded": bool(contract.required_items_degraded) if contract is not None else True,
    }


_REQUIRED_DATA_SIGNALS: tuple[tuple[str, ...], ...] = (
    ("매출", "매출액", "sales"),
    ("시장점유율", "점유율", "market share", "share"),
    ("성장률", "증감률", "growth"),
    ("순위", "rank"),
    ("환자수", "환자 수", "patient"),
    ("임상", "clinical", "nct"),
    ("특허", "patent"),
)

_UNAVAILABLE_DATA_PHRASES: tuple[str, ...] = (
    "확인되지 않았",
    "확인할 수 없",
    "연결하지 못했",
    "자료가 없",
    "데이터가 없",
    "근거가 없",
)


def _required_data_is_grounded(
    item: object,
    facts_text: str,
    digest: FactDigest,
) -> bool:
    """Infer only explicit data coverage; interpretive items still require `answers`."""

    if str(getattr(item, "kind", "")) != "data" or not re.search(r"\d", facts_text):
        return False
    ask = str(getattr(item, "ask", "")).casefold()
    facts = facts_text.casefold()
    contract = digest.answer_contract
    resolved_entities = (
        tuple(entity.casefold() for entity in contract.resolved_entities if entity.strip())
        if contract is not None
        else ()
    )
    if resolved_entities and not any(entity in facts for entity in resolved_entities):
        return False
    signal_groups = tuple(
        aliases for aliases in _REQUIRED_DATA_SIGNALS if any(alias in ask for alias in aliases)
    )
    if not signal_groups:
        return False
    clauses = tuple(
        clause.strip()
        for clause in re.split(
            r"(?:[.!?。,;]\s*|\n+|이고\s+|이며\s+|지만\s+|그러나\s+|반면\s+|다만\s+)",
            facts,
        )
        if clause.strip()
    )
    if not all(
        any(
            re.search(r"\d", clause)
            and any(alias in clause for alias in aliases)
            and not any(phrase in clause for phrase in _UNAVAILABLE_DATA_PHRASES)
            for clause in clauses
        )
        for aliases in signal_groups
    ):
        return False
    return any(card.received_count > 0 for card in digest.cards) or bool(
        digest.derived_metrics
    )


def _decode_claim_objects(raw: str) -> _DecodedClaims:
    cleaned = _JSON_FENCE_RE.sub("", raw.strip())
    starts = tuple(index for index in (cleaned.find("{"), cleaned.find("[")) if index >= 0)
    if not starts:
        raise ClaimPayloadError(
            "structured insight output has no JSON object",
            details={"reason_code": "no_json_object"},
        )
    cursor = min(starts)
    decoder = json.JSONDecoder()
    values: list[object] = []
    try:
        first, cursor = decoder.raw_decode(cleaned, cursor)
        if isinstance(first, dict) and set(first) in ({"s", "c"}, {"s", "e", "c"}):
            evidence_dictionary = _compact_evidence_dictionary(first.get("e", ()))
            return _DecodedClaims(
                tuple(
                    _expand_compact_claim(first["s"], evidence_dictionary, value)
                    for value in _compact_claim_rows(first["c"])
                ),
                "compact_envelope",
            )
        if isinstance(first, dict) and "claims" in first:
            if set(first) != {"claims"}:
                raise ClaimPayloadError(
                    "structured insight envelope has unsupported field(s)",
                    details={
                        "reason_code": "invalid_envelope_field",
                        "failed_field": "payload",
                    },
                )
            claims = first["claims"]
            if not isinstance(claims, list):
                raise ClaimPayloadError(
                    "structured insight claims field must be an array",
                    details={
                        "reason_code": "invalid_claims_container",
                        "failed_field": "claims",
                    },
                )
            return _DecodedClaims(tuple(claims), "envelope")
        if isinstance(first, list):
            return _DecodedClaims(tuple(first), "claim_array")
        values.append(first)
        while cursor < len(cleaned):
            while cursor < len(cleaned) and (cleaned[cursor].isspace() or cleaned[cursor] == ","):
                cursor += 1
            if cursor >= len(cleaned):
                break
            value, cursor = decoder.raw_decode(cleaned, cursor)
            values.append(value)
    except json.JSONDecodeError as exc:
        raise ClaimPayloadError(
            f"structured insight JSON decode failed at {exc.pos}: {exc.msg}",
            details={
                "reason_code": "invalid_json",
                "failed_field": "payload",
                "position": exc.pos,
                "excerpt": cleaned[max(0, exc.pos - 80) : exc.pos + 80],
            },
        ) from exc

    if values and all(isinstance(value, dict) for value in values):
        return _DecodedClaims(tuple(values), "concatenated_claims")
    raise ClaimPayloadError(
        "structured insight output does not contain claim objects",
        details={"reason_code": "invalid_payload_shape"},
    )


def _compact_claim_rows(value: object) -> list[object]:
    if not isinstance(value, list) or not value:
        raise ClaimPayloadError(
            "compact claim field must be a non-empty array",
            details={"reason_code": "invalid_claims_container", "failed_field": "c"},
        )
    return value


def _compact_evidence_dictionary(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ClaimPayloadError(
            "compact evidence dictionary must be an array",
            details={"reason_code": "invalid_claims_container", "failed_field": "e"},
        )
    evidence_ids = tuple(str(item) for item in value)
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ClaimPayloadError(
            "compact evidence dictionary must not contain duplicate ids",
            details={"reason_code": "duplicate_evidence_id", "failed_field": "e"},
        )
    return evidence_ids


def _expand_compact_claim(
    section: object,
    evidence_dictionary: tuple[str, ...],
    value: object,
) -> dict[str, object]:
    if section not in {"facts", "insight"}:
        raise ClaimPayloadError(
            "compact claim section is invalid",
            details={"reason_code": "invalid_enum_value", "failed_field": "s"},
        )
    if not isinstance(value, list) or not 3 <= len(value) <= 4:
        raise ClaimPayloadError(
            "compact claim row must contain text, type, evidence ids, and optional answers",
            details={"reason_code": "invalid_payload_shape", "failed_field": "c"},
        )
    text, claim_type_code, evidence_refs, *tail = value
    if not isinstance(evidence_refs, list):
        raise ClaimPayloadError(
            "compact evidence references must be an array",
            details={"reason_code": "invalid_claims_container", "failed_field": "c.evidence_refs"},
        )
    claim_type = {
        "C": "CITE",
        "K": "CALC",
        "O": "OBS",
        "I": "INTERP",
        "H": "HYPO",
    }.get(str(claim_type_code), str(claim_type_code))
    evidence_ids: list[str] = []
    for reference in evidence_refs:
        if isinstance(reference, int):
            if reference < 0 or reference >= len(evidence_dictionary):
                raise ClaimPayloadError(
                    "compact evidence reference is outside the dictionary",
                    details={"reason_code": "invalid_evidence_reference", "failed_field": "c.evidence_refs"},
                )
            evidence_ids.append(evidence_dictionary[reference])
        else:
            evidence_ids.append(str(reference))
    answers = tail[0] if tail else []
    if not isinstance(answers, list):
        raise ClaimPayloadError(
            "compact answers must be an array",
            details={"reason_code": "invalid_claims_container", "failed_field": "c.answers"},
        )
    hedge = {"INTERP": "softened", "HYPO": "hypothesis"}.get(claim_type, "none")
    return {
        "section": section,
        "text": text,
        "claim_type": claim_type,
        "evidence_ids": list(dict.fromkeys(str(item) for item in evidence_ids)),
        "hedge": hedge,
        "answers": answers,
    }


def _schema_quarantine(
    index: int,
    raw_claim: object,
    error: ValidationError,
) -> dict[str, object]:
    issue = error.errors(include_url=False)[0]
    location = issue.get("loc", ())
    failed_field = ".".join(str(part) for part in location) or "claim"
    error_type = str(issue.get("type", "schema_violation"))
    reason_code = {
        "missing": "missing_required_field",
        "enum": "invalid_enum_value",
        "extra_forbidden": "extra_field",
    }.get(error_type, "schema_violation")
    return _quarantine_entry(
        index,
        raw_claim,
        failed_field=failed_field,
        reason_code=reason_code,
        message=f"claim {index} schema violation at {failed_field}: {issue.get('msg', error_type)}",
    )


def _quarantine_entry(
    index: int,
    raw_claim: object,
    *,
    failed_field: str,
    reason_code: str,
    message: str,
) -> dict[str, object]:
    serialized = json.dumps(raw_claim, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "claim_index": index,
        "failed_field": failed_field,
        "reason_code": reason_code,
        "message": message,
        "excerpt": serialized[:160],
        "raw_claim": raw_claim,
    }


def _assemble_surface(claims: tuple[InsightClaim, ...]) -> str:
    return f"## 종합 인사이트\n{_assemble_insight_body(claims)}".rstrip()


def _assemble_insight_body(claims: tuple[InsightClaim, ...]) -> str:
    groups = (
        frozenset({ClaimType.CITE, ClaimType.CALC}),
        frozenset({ClaimType.OBS, ClaimType.INTERP}),
    )
    paragraphs = [
        " ".join(
            _normalize_claim_suffix(claim.text)
            for claim in claims
            if claim.claim_type in group
        )
        for group in groups
    ]
    paragraphs.extend(
        _normalize_claim_suffix(claim.text)
        for claim in claims
        if claim.claim_type is ClaimType.HYPO
    )
    body = "\n\n".join(paragraph for paragraph in paragraphs if paragraph)
    return body


def _section_paragraphs(
    *,
    insight_claims: tuple[InsightClaim, ...],
    facts_claims: tuple[InsightClaim, ...],
    facts_text: str,
    digest: FactDigest,
) -> dict[str, list[dict[str, object]]]:
    insight = _claim_payloads(insight_claims, digest, section="insight")
    facts = _claim_payloads(facts_claims, digest, section="facts")
    emitted_facts = " ".join(str(item["text"]) for item in facts).strip()
    coverage_suffix = (
        facts_text[len(emitted_facts) :].strip()
        if emitted_facts and facts_text.startswith(emitted_facts)
        else ""
    )
    if coverage_suffix:
        facts.append(
            {
                "text": coverage_suffix,
                "evidence": [],
                "unsourced": True,
                "evidence_id_count": 0,
                "paragraph_start": True,
            }
        )
    return {"insight": insight, "facts": facts}


def _claim_payloads(
    claims: tuple[InsightClaim, ...],
    digest: FactDigest,
    *,
    section: str,
) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    previous_group: str | None = None
    for index, claim in enumerate(claims):
        group = (
            "facts"
            if section == "facts"
            else "observation"
            if claim.claim_type in {ClaimType.CITE, ClaimType.CALC}
            else "interpretation"
            if claim.claim_type in {ClaimType.OBS, ClaimType.INTERP}
            else "hypothesis"
        )
        text = _normalize_claim_suffix(claim.text)
        duplicate = next(
            (
                existing
                for existing in payloads
                if text == str(existing["text"])
                or text.startswith(f"{existing['text']} ")
            ),
            None,
        )
        if duplicate is not None:
            duplicate_text = str(duplicate["text"])
            duplicate_support = _paragraph_payload(duplicate_text, (claim,), digest)
            _merge_paragraph_evidence(duplicate, duplicate_support, digest)
            counters = duplicate.setdefault(
                "sentence_dedup",
                {"duplicates_suppressed": 0, "groups_merged": 0, "suffixes_preserved": 0},
            )
            counters["duplicates_suppressed"] += 1
            counters["groups_merged"] += 1
            suffix = text[len(duplicate_text) :].strip()
            if not suffix:
                continue
            counters["suffixes_preserved"] += 1
            text = suffix
        payload = _paragraph_payload(text, (claim,), digest)
        payload["paragraph_start"] = (
            index == 0
            or group != previous_group
            or (section == "facts" and index % 4 == 0)
        )
        payloads.append(payload)
        previous_group = group
    return payloads


def _merge_paragraph_evidence(
    target: dict[str, object],
    incoming: dict[str, object],
    digest: FactDigest,
) -> None:
    def members(payload: dict[str, object]) -> list[dict[str, str]]:
        group = payload.get("evidence_group")
        values = group.get("members", ()) if isinstance(group, dict) else payload.get("evidence", ())
        return [
            {"evidence_id": str(item["evidence_id"]), "label": str(item.get("label") or "출처")}
            for item in values
            if isinstance(item, dict) and item.get("evidence_id")
        ]

    merged = list(
        {item["evidence_id"]: item for item in (*members(target), *members(incoming))}.values()
    )
    target["evidence_id_count"] = len(merged)
    target["unsourced"] = not merged
    target["evidence"] = merged
    target.pop("evidence_group", None)
    if len(merged) > 1:
        group = _evidence_group_payload(str(target["text"]), merged, digest)
        target["evidence"] = [group["primary"]]
        target["evidence_group"] = group
    supply = dict(target.get("evidence_supply") or {})
    supply["retained_count"] = len(merged)
    supply["candidate_count"] = max(int(supply.get("candidate_count") or 0), len(merged))
    target["evidence_supply"] = supply


def _paragraph_payload(
    text: str,
    claims: tuple[InsightClaim, ...],
    digest: FactDigest,
) -> dict[str, object]:
    metric_inputs = _metric_evidence_ids(digest)
    candidate_evidence_ids = tuple(
        dict.fromkeys(
            resolved
            for claim in claims
            for identifier in claim.evidence_ids
            for resolved in metric_inputs.get(identifier, (identifier,))
            if resolved
        )
    )
    evidence_ids = candidate_evidence_ids
    if len(evidence_ids) > 1:
        evidence_ids, group_domain_check = filter_group_evidence_ids(
            text,
            evidence_ids,
            digest,
        )
    else:
        group_domain_check = {
            "checked": len(evidence_ids),
            "retained": len(evidence_ids),
            "mismatched_removed": 0,
            "unknown_removed": 0,
        }
    evidence_members = [
        {
            "evidence_id": identifier,
            "label": _evidence_label(
                identifier,
                digest,
                claim_text=text if len(evidence_ids) == 1 else "",
            ),
        }
        for identifier in evidence_ids
    ]
    payload: dict[str, object] = {
        "text": text.strip(),
        "evidence": evidence_members,
        "unsourced": not evidence_ids,
        "evidence_id_count": len(evidence_ids),
        "group_domain_check": group_domain_check,
        "evidence_supply": {
            "candidate_count": len(candidate_evidence_ids),
            "retained_count": len(evidence_ids),
            "excluded_count": len(candidate_evidence_ids) - len(evidence_ids),
            "pre_verifier_cap_omitted": 0,
        },
    }
    if len(evidence_members) > 1:
        group = _evidence_group_payload(text, evidence_members, digest)
        payload["evidence"] = [group["primary"]]
        payload["evidence_group"] = group
    return payload


def _evidence_group_payload(
    text: str,
    evidence: list[dict[str, str]],
    digest: FactDigest,
) -> dict[str, object]:
    members = [
        {
            **item,
            "source_key": _evidence_source(item["evidence_id"], digest)[0],
            "source_label": _evidence_source(item["evidence_id"], digest)[1],
        }
        for item in evidence
    ]
    primary = next(
        (
            member
            for member in members
            if _evidence_entity(member["evidence_id"], digest) in text
            and _evidence_entity(member["evidence_id"], digest)
        ),
        members[0],
    )
    source_counts: Counter[tuple[str, str]] = Counter(
        (member["source_key"], member["source_label"]) for member in members
    )
    group_seed = "\x1f".join(
        (text.strip(), *(member["evidence_id"] for member in members))
    )
    return {
        "schema": "jw.evidence-group.v1",
        "group_id": f"eg-{hashlib.sha256(group_seed.encode()).hexdigest()[:16]}",
        "primary": dict(primary),
        "members": members,
        "source_breakdown": [
            {
                "source_key": source_key,
                "source_label": source_label,
                "count": count,
            }
            for (source_key, source_label), count in source_counts.items()
        ],
    }


def _evidence_source(evidence_id: str, digest: FactDigest) -> tuple[str, str]:
    card = next(
        (card for card in digest.cards if evidence_id in card.evidence_ids),
        None,
    )
    source = str(card.source if card is not None else "")
    source_key = _source_label_key(source)
    return source_key, SOURCE_LABELS.get(source_key, source or "출처")


def _evidence_entity(evidence_id: str, digest: FactDigest) -> str:
    return next(
        (
            str(card.entity or "").strip()
            for card in digest.cards
            if evidence_id in card.evidence_ids
        ),
        "",
    )


def _evidence_label(
    evidence_id: str,
    digest: FactDigest,
    *,
    claim_text: str = "",
) -> str:
    cards = tuple(card for card in digest.cards if evidence_id in card.evidence_ids)
    if not cards:
        return "출처"
    card = cards[0]
    source = card.source
    source_key = _source_label_key(source)
    source_label = SOURCE_LABELS.get(source_key, source)
    card_identifiers = claim_identifiers((evidence_id,), digest)
    direct_identifiers = text_identifiers(evidence_id)
    claim_text_identifiers = text_identifiers(claim_text) if claim_text else {}
    identifiers = {
        key: direct_identifiers.get(key)
        or claim_text_identifiers.get(key)
        or card_identifiers.get(key, "")
        for key in card_identifiers
    }
    suffixes: list[str] = []
    if source_key == "clinicaltrials" and identifiers.get("clinical"):
        suffixes.append(identifiers["clinical"])
    elif source_key == "patent" and identifiers.get("특허"):
        suffixes.append(identifiers["특허"])
    elif source_key == "hira":
        suffixes.extend(
            value
            for value in (identifiers.get("상병"), identifiers.get("기간"))
            if value
        )
    elif source_key in {"document", "document_rag", "document_sql"}:
        file_identifiers = _file_identifiers(card.model_dump(mode="json"))
        suffixes.extend(file_identifiers)
        if not file_identifiers and str(card.entity or "").casefold().endswith(
            (".pdf", ".xlsx", ".xls", ".csv")
        ):
            suffixes.append(str(card.entity))
    return " ".join((f"출처: {source_label}", *dict.fromkeys(suffixes)))


def _source_label_key(source: str) -> str:
    if source in SOURCE_LABELS:
        return source
    if source.startswith("patent"):
        return "patent"
    if source in {"file_sql", "file_excel_analytics"}:
        return "document_sql"
    if source in {"file_vdb", "document_vdb"}:
        return "document_rag"
    return source


def _file_identifiers(value: object) -> tuple[str, ...]:
    found: dict[str, str] = {}

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                normalized_key = str(key).casefold()
                if normalized_key in {"file_name", "filename", "document_name"}:
                    text = str(nested or "").strip()
                    if text:
                        found.setdefault("file", text)
                elif normalized_key in {"sheet", "sheet_name"}:
                    text = str(nested or "").strip()
                    if text:
                        found.setdefault("sheet", text)
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(found.values())


def _metric_evidence_ids(digest: FactDigest) -> dict[str, tuple[str, ...]]:
    source_ids = {
        identifier
        for identifier in digest.visible_record_ids
        if identifier
    }
    source_ids.update(
        identifier
        for card in digest.cards
        for identifier in card.evidence_ids
        if identifier
    )
    card_ids_by_entity: dict[str, list[str]] = {}
    for card in digest.cards:
        entity = " ".join(str(card.entity or "").casefold().split())
        if entity and card.evidence_ids:
            card_ids_by_entity.setdefault(entity, []).extend(card.evidence_ids)

    resolved: dict[str, tuple[str, ...]] = {}
    for metric in digest.derived_metrics:
        input_ids = tuple(
            identifier for identifier in metric.inputs if identifier in source_ids
        )
        entity = " ".join(str(metric.entity or "").casefold().split())
        evidence_ids = input_ids or tuple(
            dict.fromkeys(card_ids_by_entity.get(entity, ()))
        )
        resolved[metric.id] = tuple(dict.fromkeys(evidence_ids))
    return resolved


def _normalize_claim_suffix(text: str) -> str:
    return text.strip().replace(_MALFORMED_PROBABILITY_SUFFIX, "했을 가능성")


__all__ = [
    "ClaimEnvelope",
    "ClaimPayloadError",
    "ClaimSection",
    "ClaimType",
    "InsightClaim",
    "ParsedClaims",
    "ensure_required_item_coverage",
    "evidence_catalog_payload",
    "evidence_id_catalog",
    "parse_claim_payload",
]
