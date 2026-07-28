from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
import re
from typing import Any, Final

from pydantic import ValidationError

from jw_chat_agent_poc.orchestrator.markdown_formatting import allowed_numbers
from jw_chat_agent_poc.resolver import (
    AmbiguousBrandError,
    BrandResolution,
    BrandResolver,
    UnsupportedBrandError,
)
from jw_chat_agent_poc.tool_use.contracts import EvidenceFact, ToolEnvelope
from jw_chat_agent_poc.tool_use.renderer import render_evidence_claim
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    CacheLookupStatus,
    ReimbursementLookupResult,
)

_REIMBURSEMENT_TOOL: Final[str] = "hira_reimbursement_criteria"
_IDENTITY_NOTICE_PREFIX: Final[str] = "주의: 조회된 근거는 "
# A mismatched notice is withheld, not annotated. Reimbursement criteria are
# quoted into downstream reports, so a warning that precedes the notice body
# does not survive the copy: whoever pastes the criteria pastes them without
# the warning and then bills against another product's rules. The marker is
# what identifies our own withholding sentence to the notice surface, and it
# doubles as the reason the body is absent so the answer never degrades into
# "lookup failed" for a cause that is actually "the linked notice is a
# different product".
_IDENTITY_BLOCK_MARKER: Final[str] = "급여기준은 제공할 수 없습니다"
_IDENTITY_BLOCK_CODE: Final[str] = "IDENTITY_MISMATCH"
_IDENTITY_STATUSES: Final[frozenset[str]] = frozenset({"match", "mismatch", "unverifiable"})
_BLOCKED_REASONS: Final[frozenset[str]] = frozenset({"identity_mismatch"})
_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9A-Za-z가-힣_.:+-]{1,255}$"
)
LOGGER = logging.getLogger(__name__)
_PRODUCT_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"품명\s*[:：]\s*(?P<products>[^)\n|]+)",
    re.IGNORECASE,
)
_PRODUCT_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(r"\s*(?:,|/|·| 및 )\s*")
_DOSAGE_RE: Final[re.Pattern[str]] = re.compile(r"\s+\d[\w./%-]*(?:\s.*)?$", re.IGNORECASE)
_PRODUCT_FORM_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:피하주사|프리필드시린지|주사액|주사제|캡슐제|경구액|현탁액|정제|캡슐|시럽|과립|패치|정|주)$"
)


@dataclass(frozen=True, slots=True)
class _IdentityDecision:
    status: str
    match: bool | None
    notice: str = ""
    requested_brand: str | None = None
    source_variance: bool = False
    resolved_via_alias: bool = False


def reimbursement_envelope(
    result: ReimbursementLookupResult,
    *,
    subject: str,
    resolver: BrandResolver | None = None,
) -> ToolEnvelope:
    if not result.ok or result.data is None:
        error_code = result.error_code or "NO_EVIDENCE"
        if result.cache_lookup_status is CacheLookupStatus.STORE_ABSENT:
            message = (
                "급여기준 조회 기능은 현재 준비 중입니다. "
                "심사평가원(HIRA) 사이트에서 직접 확인해 주세요."
            )
        elif result.cache_lookup_status is CacheLookupStatus.BRAND_UNMATCHED:
            message = "해당 브랜드는 아직 급여기준 색인 대상이 아닙니다."
        else:
            message = {
                "TOOL_TIMEOUT": "HIRA 급여기준 실시간 조회 시간이 초과되었습니다.",
                "NO_EVIDENCE": "HIRA 보험인정기준에서 해당 제품의 기록을 찾지 못했습니다.",
            }.get(error_code, "HIRA 급여기준 원천을 확인할 수 없습니다.")
        return ToolEnvelope(
            ok=False,
            preview=message,
            evidence=(),
            raw={
                "retrieval": result.retrieval,
                "cache_status": result.cache_status.value,
                "cache_lookup_status": result.cache_lookup_status.value,
                "cache_schema": result.cache_schema,
            },
            error_code=error_code,
            error_message=message,
        )

    data = result.data
    identity = (
        _reimbursement_identity(
            subject=subject,
            raw_text=data.raw_text,
            resolver=resolver,
        )
        if resolver is not None
        else _IdentityDecision("unverifiable", None)
    )
    identity_record = _identity_record(
        identity,
        served_notice_id=data.source_notice_id,
    )
    LOGGER.info(
        "reimbursement_identity requested_brand=%s served_notice_id=%s "
        "identity_status=%s blocked_reason=%s source_variance=%s "
        "resolved_via_alias=%s",
        identity_record["requested_brand"],
        identity_record["served_notice_id"],
        identity_record["identity_status"],
        identity_record["blocked_reason"],
        identity_record["source_variance"],
        identity_record["resolved_via_alias"],
    )
    if identity.status == "mismatch":
        # Withhold before the fact exists. The notice body reaches the answer,
        # the projected evidence and the planner's message history through this
        # one fact, so declining to build it closes all three at once and keeps
        # the other product's criteria out of the model's context entirely.
        return ToolEnvelope(
            ok=False,
            preview=identity.notice,
            evidence=(),
            raw={
                "retrieval": result.retrieval,
                "cache_status": result.cache_status.value,
                "cache_lookup_status": result.cache_lookup_status.value,
                "cache_schema": result.cache_schema,
                "cache_write": result.cache_write,
                "identity_status": identity.status,
                "identity_match": identity.match,
                "identity_notice_required": bool(identity.notice),
                "identity_notice": identity.notice,
                "body_suppressed": True,
                **identity_record,
            },
            error_code=_IDENTITY_BLOCK_CODE,
            error_message=identity.notice,
        )
    fact = EvidenceFact(
        fact_id=f"hira_reimbursement:{subject}:{data.source_date or 'undated'}",
        subject=subject,
        metric="HIRA 보험인정기준 원문 (AI 요약·해석·재구성 없음)",
        value=None,
        unit=None,
        period=data.source_date,
        source_name="심사평가원(HIRA) 보험인정기준",
        source_locator=data.raw_text,
        raw_ref=data.source_url,
    )
    return ToolEnvelope(
        ok=True,
        preview=f"{subject} HIRA 보험인정기준 원문 확인 (AI 요약·해석·재구성 없음)",
        evidence=(fact,),
        raw={
            "retrieval": result.retrieval,
            "cache_status": result.cache_status.value,
            "cache_lookup_status": result.cache_lookup_status.value,
            "cache_schema": result.cache_schema,
            "cache_write": result.cache_write,
            "notice_number": data.notice_number,
            "source_url": data.source_url,
            "identity_status": identity.status,
            "identity_match": identity.match,
            "identity_notice_required": bool(identity.notice),
            "identity_notice": identity.notice,
            "body_suppressed": False,
            **identity_record,
        },
        error_code=None,
        error_message=None,
    )


def public_reimbursement_identity_fields(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        return {}
    status = raw.get("identity_status")
    match = raw.get("identity_match")
    notice_required = raw.get("identity_notice_required")
    notice = raw.get("identity_notice")
    suppressed = raw.get("body_suppressed")
    requested_brand = _bounded_identifier(raw.get("requested_brand"))
    served_notice_id = _bounded_identifier(raw.get("served_notice_id"))
    blocked_reason = raw.get("blocked_reason")
    source_variance = raw.get("source_variance")
    resolved_via_alias = raw.get("resolved_via_alias")
    if status not in _IDENTITY_STATUSES or not isinstance(notice_required, bool):
        return {}
    if match is not None and not isinstance(match, bool):
        return {}
    fields: dict[str, object] = {
        "identity_status": status,
        "identity_match": match,
        "identity_notice_required": notice_required,
        # Whether the notice body was actually withheld, kept separate from
        # whether a disclosure was decided. "Decided" and "reached the user"
        # are different claims, and only the second one protects anybody.
        "body_suppressed": suppressed if isinstance(suppressed, bool) else False,
        "requested_brand": requested_brand,
        "served_notice_id": served_notice_id,
        "blocked_reason": (
            blocked_reason if blocked_reason in _BLOCKED_REASONS else None
        ),
        "source_variance": (
            source_variance if isinstance(source_variance, bool) else False
        ),
        "resolved_via_alias": (
            resolved_via_alias if isinstance(resolved_via_alias, bool) else False
        ),
    }
    if (
        notice_required
        and isinstance(notice, str)
        and is_reimbursement_identity_notice(notice)
    ):
        fields["identity_notice"] = notice
    return fields


def reimbursement_identity_notices(tool_calls: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    notices: list[str] = []
    for call in tool_calls:
        if call.get("tool") != _REIMBURSEMENT_TOOL:
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, Mapping):
            continue
        notice = render_data.get("identity_notice")
        if (
            isinstance(notice, str)
            and is_reimbursement_identity_notice(notice)
            and notice not in notices
        ):
            notices.append(notice)
    return tuple(notices)


def is_reimbursement_identity_notice(notice: str) -> bool:
    """Recognise our own identity sentence on the notice surface.

    Both shapes are ours: the annotation that accompanies a retained body and
    the withholding sentence that replaces one. Digits stay disqualifying for
    both, because the notice appender drops any notice containing one and a
    silently dropped disclosure is worse than none.
    """

    return (
        notice.startswith(_IDENTITY_NOTICE_PREFIX)
        or _IDENTITY_BLOCK_MARKER in notice
    ) and not any(character.isdigit() for character in notice)


def _reimbursement_identity(
    *,
    subject: str,
    raw_text: str,
    resolver: BrandResolver,
) -> _IdentityDecision:
    try:
        requested = resolver.resolve(subject, allow_default=False)
    except (AmbiguousBrandError, UnsupportedBrandError):
        return _IdentityDecision("unverifiable", None)

    mentioned: list[BrandResolution] = []
    seen: set[str] = set()
    for product in _notice_product_names(raw_text):
        try:
            resolution = resolver.resolve(product, allow_default=False)
        except (AmbiguousBrandError, UnsupportedBrandError):
            continue
        if resolution.canonical_brand not in seen:
            seen.add(resolution.canonical_brand)
            mentioned.append(resolution)
    if not mentioned:
        return _decision_for_requested("unverifiable", None, requested)

    requested_molecules = _molecule_set(requested.molecule_en)
    if any(
        item.canonical_brand == requested.canonical_brand
        and _molecule_set(item.molecule_en) == requested_molecules
        for item in mentioned
    ):
        return _decision_for_requested("match", True, requested)

    related = [
        item
        for item in mentioned
        if requested_molecules & _molecule_set(item.molecule_en)
    ]
    if not related:
        return _decision_for_requested("unverifiable", None, requested)
    closest = max(
        related,
        key=lambda item: len(requested_molecules & _molecule_set(item.molecule_en)),
    )
    return _decision_for_requested(
        "mismatch",
        False,
        requested,
        notice=_block_notice(requested, closest),
    )


def _decision_for_requested(
    status: str,
    match: bool | None,
    requested: BrandResolution,
    *,
    notice: str = "",
) -> _IdentityDecision:
    return _IdentityDecision(
        status=status,
        match=match,
        notice=notice,
        requested_brand=requested.canonical_brand,
        source_variance=requested.source_variance,
        resolved_via_alias=requested.resolved_via_alias,
    )


def _identity_record(
    identity: _IdentityDecision,
    *,
    served_notice_id: object,
) -> dict[str, object]:
    return {
        "identity_status": identity.status,
        "requested_brand": _bounded_identifier(identity.requested_brand),
        "served_notice_id": _bounded_identifier(served_notice_id),
        "blocked_reason": (
            "identity_mismatch" if identity.status == "mismatch" else None
        ),
        "source_variance": identity.source_variance,
        "resolved_via_alias": identity.resolved_via_alias,
    }


def _bounded_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _IDENTIFIER_RE.fullmatch(candidate) else None


def _block_notice(requested: BrandResolution, closest: BrandResolution) -> str:
    """State the cause without reproducing any of the other product's record.

    The mismatched product is described by dosage form, never by name. Naming it
    would put the very token the withholding exists to suppress back into an
    answer that gets pasted into reports, and the form alone already tells the
    reader why their request could not be served. Nothing here is invented: the
    form is computed from the molecule set the catalog already resolved.

    No digits, because the notice surface drops any notice containing one and a
    silently dropped disclosure is worse than no disclosure at all.
    """

    specific = (
        f"요청하신 {requested.canonical_brand} {_form_label(requested.molecule_en)} "
        f"{_IDENTITY_BLOCK_MARKER}. 연결된 고시가 성분 구성이 다른 "
        f"{_form_label(closest.molecule_en)} 기준으로 확인되어, 요청하신 제품의 "
        "기준으로 사용할 수 없습니다. 확인이 필요하시면 심사평가원 고시에서 "
        "해당 제품명으로 직접 확인해 주세요."
    )
    if not any(character.isdigit() for character in specific):
        return specific
    return (
        f"요청하신 제품의 {_IDENTITY_BLOCK_MARKER}. 연결된 고시가 성분 구성이 "
        "다른 제품 기준으로 확인되어, 요청하신 제품의 기준으로 사용할 수 없습니다. "
        "확인이 필요하시면 심사평가원 고시를 직접 확인해 주세요."
    )


def _notice_product_names(raw_text: str) -> tuple[str, ...]:
    products: list[str] = []
    for match in _PRODUCT_NAME_RE.finditer(raw_text):
        for raw_product in _PRODUCT_SEPARATOR_RE.split(match.group("products")):
            product = re.sub(r"\s*등\s*$", "", raw_product).strip()
            product = _DOSAGE_RE.sub("", product).strip()
            product = _PRODUCT_FORM_RE.sub("", product).strip()
            if product and product not in products:
                products.append(product)
    return tuple(products)


def _molecule_set(molecules: Sequence[str]) -> frozenset[str]:
    return frozenset(value.strip().casefold() for value in molecules if value.strip())


def _form_label(molecules: Sequence[str]) -> str:
    return "복합제" if len(_molecule_set(molecules)) > 1 else "단일제"


def project_reimbursement_evidence(
    tool_calls: Sequence[Mapping[str, Any]],
    fact_md: str,
) -> list[dict[str, Any]]:
    """Project rendered HIRA criteria facts beside the existing F4 projection."""

    rendered_lines = frozenset(fact_md.splitlines())
    projected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for call in tool_calls:
        if call.get("tool") != _REIMBURSEMENT_TOOL or call.get("status") != "ok":
            continue
        render_data = call.get("render_data")
        if not isinstance(render_data, Mapping) or render_data.get("ok") is False:
            continue
        serialized = render_data.get("evidence")
        if not isinstance(serialized, Sequence) or isinstance(serialized, str | bytes):
            continue
        for raw_fact in serialized:
            if not isinstance(raw_fact, Mapping):
                continue
            try:
                fact = EvidenceFact.model_validate(raw_fact)
            except ValidationError:
                continue
            rendered = render_evidence_claim(fact)
            if (
                fact.fact_id in seen_ids
                or rendered not in rendered_lines
                or not (fact.source_locator or "").strip()
            ):
                continue
            seen_ids.add(fact.fact_id)
            projected.append(_legacy_reimbursement_fact(fact, rendered))
    return projected


def _legacy_reimbursement_fact(
    fact: EvidenceFact,
    rendered: str,
) -> dict[str, Any]:
    return {
        "fact_id": fact.fact_id,
        "label": fact.metric,
        "value": str(fact.source_locator or ""),
        "source": fact.source_name,
        "tool": _REIMBURSEMENT_TOOL,
        "path": fact.raw_ref or f"render_data.evidence.{fact.fact_id}",
        "period": fact.period or "",
        "allowed_numbers": list(allowed_numbers(rendered)),
        "visible": True,
        "entity": fact.subject,
        "metric": fact.metric,
        "unit": fact.unit or "",
        "source_grade": "AUTHORITATIVE",
        "view": "",
        "operand_fact_ids": [],
    }
