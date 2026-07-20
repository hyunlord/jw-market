from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.tool_use.routing_v4_types import (
    CapabilityStatus,
    RoutingV4ContractError,
    ToolSelectionSource,
)


class CapabilityResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_domain: str
    requested_capability: str
    status: CapabilityStatus
    eligible_tools: tuple[str, ...]
    typed_reason_code: str | None


class _CapabilityEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    source_domain: str
    requested_capability: str
    capability_status: CapabilityStatus
    eligible_tools: tuple[str, ...]


class CapabilityMatrix:
    def __init__(self, entries: tuple[_CapabilityEntry, ...]) -> None:
        indexed: dict[tuple[str, str], _CapabilityEntry] = {}
        for entry in entries:
            key = (entry.source_domain, entry.requested_capability)
            if key in indexed:
                raise RoutingV4ContractError(f"duplicate capability entry: {key}")
            if entry.capability_status is CapabilityStatus.SUPPORTED and not entry.eligible_tools:
                raise RoutingV4ContractError(f"supported capability has no eligible tools: {key}")
            if entry.capability_status is not CapabilityStatus.SUPPORTED and entry.eligible_tools:
                raise RoutingV4ContractError(f"unsupported capability has eligible tools: {key}")
            indexed[key] = entry
        self._entries = indexed

    @classmethod
    def from_json(cls, path: str | Path) -> CapabilityMatrix:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        statuses = tuple(payload.get("statuses") or ())
        if set(statuses) != {status.value for status in CapabilityStatus}:
            raise RoutingV4ContractError("capability matrix must declare the exact v4 four-state model")
        return cls(tuple(_CapabilityEntry.model_validate(item) for item in payload.get("entries") or ()))

    def status_for(self, source_domain: str, requested_capability: str) -> CapabilityStatus:
        return self.resolve(source_domain, requested_capability).status

    def resolve(self, source_domain: str, requested_capability: str) -> CapabilityResolution:
        entry = self._entries.get((source_domain, requested_capability))
        if entry is None:
            return CapabilityResolution(
                source_domain=source_domain,
                requested_capability=requested_capability,
                status=CapabilityStatus.UNRESOLVED,
                eligible_tools=(),
                typed_reason_code="AMBIGUOUS_INPUT",
            )
        reason_codes = {
            CapabilityStatus.SUPPORTED: None,
            CapabilityStatus.FIELD_NOT_EXPOSED: "FIELD_NOT_EXPOSED",
            CapabilityStatus.NOT_IMPLEMENTED: "CAPABILITY_NOT_IMPLEMENTED",
            CapabilityStatus.UNRESOLVED: "AMBIGUOUS_INPUT",
        }
        return CapabilityResolution(
            source_domain=entry.source_domain,
            requested_capability=entry.requested_capability,
            status=entry.capability_status,
            eligible_tools=entry.eligible_tools,
            typed_reason_code=reason_codes[entry.capability_status],
        )


def default_capability_matrix() -> CapabilityMatrix:
    entries = (
        _entry("hira", "HIRA_DISEASE_CODE_LOOKUP", "SUPPORTED", ("hira_disease_name_code",)),
        _entry(
            "hira",
            "HIRA_DISEASE_PATIENT_STATS",
            "SUPPORTED",
            (
                "hira_disease_hospitalization_outpatient_stats",
                "hira_disease_gender_age_stats",
                "hira_disease_institution_class_stats",
                "hira_disease_area_stats",
            ),
        ),
        _entry("hira", "HIRA_LABEL_EFFICACY", "FIELD_NOT_EXPOSED"),
        _entry(
            "regulatory",
            "MFDS_BASIC_PRODUCT_INFO",
            "SUPPORTED",
            ("mfds_permission_search",),
        ),
        _entry("regulatory", "MFDS_LABEL_EFFICACY", "FIELD_NOT_EXPOSED"),
        _entry("regulatory", "MFDS_DOSAGE", "FIELD_NOT_EXPOSED"),
        _entry("regulatory", "MFDS_PRECAUTIONS", "FIELD_NOT_EXPOSED"),
        _entry("regulatory", "REIMBURSEMENT_CRITERIA", "NOT_IMPLEMENTED"),
        _entry(
            "clinical_trials",
            "CLINICAL_TRIAL_SEARCH",
            "SUPPORTED",
            ("clinicaltrials_v2_search", "mfds_clinical_trial_kr"),
        ),
        _entry(
            "clinical_trials",
            "CLINICAL_TRIAL_NCT_DETAIL_FIELDS",
            "FIELD_NOT_EXPOSED",
        ),
        _entry("unresolved", "UNCLASSIFIED_EXTERNAL_REQUEST", "UNRESOLVED"),
    )
    return CapabilityMatrix(entries)


def _entry(
    source_domain: str,
    requested_capability: str,
    status: str,
    eligible_tools: tuple[str, ...] = (),
) -> _CapabilityEntry:
    return _CapabilityEntry(
        source_domain=source_domain,
        requested_capability=requested_capability,
        capability_status=CapabilityStatus(status),
        eligible_tools=eligible_tools,
    )


class ExistingAxisGate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provided_axes: tuple[str, ...]
    missing_axes: tuple[str, ...]
    reason_code: str | None
    table_allowed: bool
    may_assemble_new_axes: bool = False


def evaluate_existing_axis_gate(
    *,
    requested_axes: tuple[str, ...],
    available_axes: tuple[str, ...],
) -> ExistingAxisGate:
    requested = tuple(dict.fromkeys(requested_axes))
    available = set(available_axes)
    provided = tuple(axis for axis in requested if axis in available)
    missing = tuple(axis for axis in requested if axis not in available)
    if not provided:
        return ExistingAxisGate(
            provided_axes=(),
            missing_axes=missing,
            reason_code="FIELD_NOT_EXPOSED",
            table_allowed=False,
        )
    return ExistingAxisGate(
        provided_axes=provided,
        missing_axes=missing,
        reason_code="PARTIAL_RESULT" if missing else None,
        table_allowed=True,
    )


class ResolverPreconditionGate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active: bool
    release_gate_active: bool
    r5_edit_allowed: bool
    classification: str
    expected_gap: bool = False


def evaluate_resolver_precondition(*, exact_unique: bool) -> ResolverPreconditionGate:
    return ResolverPreconditionGate(
        active=exact_unique,
        release_gate_active=exact_unique,
        r5_edit_allowed=exact_unique,
        classification="active" if exact_unique else "out_of_scope_precondition",
    )


def selection_source_for_eligible_tools(count: int) -> ToolSelectionSource:
    if count < 0:
        raise RoutingV4ContractError("eligible tool count cannot be negative")
    if count == 0:
        return ToolSelectionSource.NONE
    if count == 1:
        return ToolSelectionSource.DETERMINISTIC_SINGLETON
    return ToolSelectionSource.LLM


def verify_claim_evidence(
    *,
    expected_evidence_ids: tuple[str, ...],
    bound_evidence_ids: tuple[str, ...],
) -> bool:
    expected = tuple(dict.fromkeys(expected_evidence_ids))
    bound = tuple(dict.fromkeys(bound_evidence_ids))
    return bool(expected) and set(expected) == set(bound)
