from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date
from hashlib import sha256
import json
from typing import Final

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.claim_ir import ClaimArgument, ClaimIR
from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet
from jw_chat_agent_poc.service.v4.narrative_recomputation import (
    ALLOWED_T2_OPERATORS,
    T2Operator,
    RecomputationEvidence,
    compute_value,
)
from jw_chat_agent_poc.service.v4.narrative_values import (
    DATE_FIELDS,
    FIELD_LABELS,
    GROUP_FIELDS,
    NUMERIC_FIELDS,
    display_field_value,
    display_number,
    field_value,
    numeric_value,
)
from jw_chat_agent_poc.service.v4.source_labels import public_source_label


MAX_T2_CLAIMS: Final = 2_147_483_647  # Compatibility export; relations are uncapped.


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RealizedClaim(_FrozenModel):
    claim: ClaimIR
    text: str
    recomputation: RecomputationEvidence


def build_relation_claims(
    evidence_sets: Sequence[EvidenceSet],
    rendered_ids: frozenset[str],
) -> tuple[RealizedClaim, ...]:
    output: list[RealizedClaim] = []
    for evidence_set in evidence_sets:
        records = tuple(
            record for record in evidence_set.records if record.evidence_id in rendered_ids
        )
        if len(records) < 2:
            continue
        label = public_source_label(evidence_set.source)
        output.append(
            _relation(
                "COUNT",
                records,
                None,
                f"{label}에서 확인된 레코드는 {len(records)}건입니다.",
            )
        )
        output.extend(_field_relations(records, label))
        output.extend(_derived_relations(evidence_set, records, label))
    return tuple(output)


def _derived_relations(
    evidence_set: EvidenceSet,
    records: Sequence[EvidenceRecord],
    source_label: str,
) -> tuple[RealizedClaim, ...]:
    output: list[RealizedClaim] = []
    for record in records:
        fields_and_operator = (
            (("competitive_market_share", "overall_market_share"), "CER", "경쟁시장 대비 전체시장 점유율 비"),
            (("sales_share", "volume_share"), "PRICE_MIX_INDEX", "매출 점유율 대비 물량 점유율 비"),
            (("brand_growth", "market_growth", "share_change_contribution"), "GROWTH_DECOMP", "브랜드 성장 분해"),
        )
        for fields, operator, label in fields_and_operator:
            if not all(numeric_value(field_value(record, field)) is not None for field in fields):
                continue
            path = "|".join(f"payload.{field}" for field in fields)
            value = compute_value(operator, (record,), path)
            output.append(
                _relation(
                    operator,
                    (record,),
                    None,
                    f"{source_label}의 {label}는 재계산값 {value}입니다.",
                    field_path=path,
                )
            )
    share_records = tuple(
        record
        for record in records
        if numeric_value(field_value(record, "market_share")) is not None
    )
    if len(share_records) >= 2:
        path = "payload.market_share"
        cr5 = compute_value("CONCENTRATION_CR5", share_records, path)
        zscores = compute_value("PEER_ZSCORE", share_records, path)
        output.append(
            _relation(
                "CONCENTRATION_CR5",
                share_records,
                None,
                f"{source_label}의 상위 5개 점유율 합(CR5)은 {cr5}입니다.",
                field_path=path,
            )
        )
        output.append(
            _relation(
                "PEER_ZSCORE",
                share_records,
                None,
                f"{source_label}의 경쟁군 점유율 표준화 값은 레코드별로 계산되었습니다.",
                field_path=path,
            )
        )
    if evidence_set.source == "clinicaltrials":
        output.extend(_clinical_relations(evidence_set, records, source_label))
    if evidence_set.source == "patent":
        output.extend(_patent_relations(records, source_label))
    return tuple(output)


def _clinical_relations(
    evidence_set: EvidenceSet,
    records: Sequence[EvidenceRecord],
    source_label: str,
) -> tuple[RealizedClaim, ...]:
    output: list[RealizedClaim] = []
    phases = tuple(record for record in records if field_value(record, "phase") or field_value(record, "phases"))
    if phases:
        value = compute_value("PHASE3_SHARE", phases, None)
        if isinstance(value, dict):
            output.append(_relation("PHASE3_SHARE", phases, None, f"{source_label} 임상 {value['total']}건 중 후기 단계(3상 이상)는 {value['late_count']}건, {float(value['share_pct']):.1f}%입니다."))
    countries = tuple(record for record in records if field_value(record, "countries"))
    if countries:
        value = compute_value("COUNTRY_SHARE", countries, "payload.countries")
        if isinstance(value, dict):
            output.append(_relation("COUNTRY_SHARE", countries, "countries", f"{source_label}의 국가별 비중은 {_distribution_text(value)}이며 국내 기관 포함 여부는 {'확인됨' if any('Korea' in str(key) or '대한민국' in str(key) for key in value) else '확인되지 않음'}입니다."))
    sponsors = tuple(record for record in records if field_value(record, "sponsor"))
    if sponsors:
        value = compute_value("SPONSOR_TYPE_SHARE", sponsors, None)
        if isinstance(value, dict):
            names = "·".join(dict.fromkeys(field_value(record, "sponsor") or "" for record in sponsors))
            output.append(_relation("SPONSOR_TYPE_SHARE", sponsors, None, f"{source_label}의 스폰서 유형은 제약사 {value['pharma_count']}건({float(value['pharma_share_pct']):.1f}%), 병원·대학 {value['institution_count']}건이며 스폰서는 {names}입니다."))
    enrollments = tuple(record for record in records if numeric_value(field_value(record, "enrollment")) is not None)
    if enrollments:
        value = compute_value("MEAN_NUMERIC", enrollments, "payload.enrollment")
        if isinstance(value, (int, float)):
            output.append(_relation("MEAN_NUMERIC", enrollments, "enrollment", f"{source_label} 임상의 평균 대상자수는 {display_number(value)}명입니다."))
    starts = tuple(record for record in records if field_value(record, "start_date"))
    observed = date.fromisoformat(evidence_set.retrieved_at[:10])
    cutoff = observed.replace(year=observed.year - 3).isoformat()
    if starts:
        path = f"payload.start_date|cutoff={cutoff}"
        value = compute_value("RECENT_SHARE", starts, path)
        if isinstance(value, dict):
            output.append(_relation("RECENT_SHARE", starts, None, f"{source_label}의 {cutoff} 이후 최근 3년 신규 등록 비중은 {value['recent_count']}건, {float(value['share_pct']):.1f}%입니다.", field_path=path))
    return tuple(output)


def _patent_relations(
    records: Sequence[EvidenceRecord],
    source_label: str,
) -> tuple[RealizedClaim, ...]:
    output: list[RealizedClaim] = []
    for field, label in (("patent_type", "특허구분별 비중"), ("extinction_reason", "소멸 사유별 비중")):
        field_records = tuple(record for record in records if field_value(record, field))
        if field_records:
            value = compute_value("GROUP_SHARE", field_records, f"payload.{field}")
            if isinstance(value, dict):
                output.append(_relation("GROUP_SHARE", field_records, field, f"{source_label}의 {label}은 {_distribution_text(value)}입니다."))
    pms_records = tuple(record for record in records if field_value(record, "pms_end_date") or field_value(record, "extinction_date"))
    value = compute_value("PMS_RESIDUAL_DAYS", pms_records, None)
    if isinstance(value, dict):
        residual_days = int(value["residual_days"])
        if residual_days >= 0:
            interval = (
                f"최종 특허 소멸일 {value['latest_extinction_date']}부터 PMS 종료일 "
                f"{value['pms_end_date']}까지 {residual_days}일"
            )
        else:
            interval = (
                f"PMS 종료 후 소멸일까지 {abs(residual_days)}일"
                f"(PMS {value['pms_end_date']}, 최종 소멸 {value['latest_extinction_date']})"
            )
        output.append(_relation("PMS_RESIDUAL_DAYS", pms_records, None, f"{source_label} 기준 {interval}이며 특허 소멸과 제네릭 진입 가능 시점은 같지 않습니다."))
    owners = tuple(record for record in records if field_value(record, "owner"))
    if owners:
        owner_names = "·".join(dict.fromkeys(field_value(record, "owner") or "" for record in owners))
        counts = compute_value("GROUP_COUNT", owners, "payload.owner")
        if isinstance(counts, dict):
            jw_count = sum(count for owner, count in counts.items() if "JW" in owner.upper() or "제이더블유" in owner)
            output.append(_relation("GROUP_SHARE", owners, "owner", f"{source_label}에서 확인된 권리자는 {owner_names}이며 JW 자사 명의 특허는 {jw_count}건입니다."))
    return tuple(output)


def _distribution_text(value: dict[str, int | float | str]) -> str:
    names = tuple(key.removesuffix("|count") for key in value if key.endswith("|count"))
    return ", ".join(
        f"{name} {value[f'{name}|count']}건({float(value[f'{name}|share_pct']):.1f}%)"
        for name in names
    )


def _field_relations(
    records: Sequence[EvidenceRecord],
    source_label: str,
) -> tuple[RealizedClaim, ...]:
    output: list[RealizedClaim] = []
    for field in GROUP_FIELDS:
        field_records = tuple(
            record for record in records if field_value(record, field) is not None
        )
        if len(field_records) < 2:
            continue
        values = tuple(display_field_value(record, field) for record in field_records)
        counts = Counter(value for value in values if value is not None)
        label = FIELD_LABELS.get(field, field)
        partial = len(field_records) < len(records)
        if len(counts) > 1:
            sentence = _group_count_sentence(
                field,
                field_records,
                counts,
                source_label,
                label,
                partial=partial,
            )
            output.append(
                _relation(
                    "GROUP_COUNT",
                    field_records,
                    field,
                    sentence,
                )
            )
        elif counts:
            common = next(iter(counts))
            subject = (
                f"{source_label}에서 {label}가 제공된 레코드는"
                if partial
                else f"{source_label}에서 확인된 레코드는"
            )
            output.append(
                _relation(
                    "COMMON_VALUE",
                    field_records,
                    field,
                    f"{subject} 모두 {label} {common}입니다.",
                )
            )
    output.extend(_date_relations(records, source_label))
    for field in NUMERIC_FIELDS:
        field_records = tuple(
            record
            for record in records
            if numeric_value(field_value(record, field)) is not None
        )
        values = tuple(numeric_value(field_value(record, field)) for record in field_records)
        present = tuple(value for value in values if value is not None)
        if len(present) >= 2 and len(set(present)) > 1:
            output.append(
                _relation(
                    "COMPARE_NUMERIC",
                    field_records,
                    field,
                    f"{source_label} 레코드의 {FIELD_LABELS.get(field, field)}은 최소 "
                    f"{display_number(min(present))}, 최대 {display_number(max(present))}입니다.",
                )
            )
    return tuple(output)


def _group_count_sentence(
    field: str,
    records: Sequence[EvidenceRecord],
    counts: Counter[str],
    source_label: str,
    label: str,
    *,
    partial: bool,
) -> str:
    total = len(records)
    maximum = max(counts.values())
    leaders = tuple(sorted(value for value, count in counts.items() if count == maximum))
    supplied = f"{label}가 제공된 " if partial else ""
    if field in {"overall_status", "status"}:
        if len(leaders) == 1:
            subject = f"{leaders[0]}{_subject_particle(leaders[0])} {maximum}건으로 가장 많습니다"
        else:
            subject = f"{'·'.join(leaders)}가 각각 {maximum}건으로 공동 최다입니다"
        return (
            f"{source_label}의 {supplied}총 {total}건 중 {subject}."
        )
    if field in {"phase", "phases"}:
        late_count = sum(
            1
            for record in records
            if any(
                token in (field_value(record, field) or "").upper()
                for token in ("PHASE3", "PHASE4")
            )
        )
        share = "절반" if late_count * 2 == total else f"{late_count / total:.0%}"
        return (
            f"{source_label}의 {supplied}총 {total}건 중 후기 단계(3상 이상)는 "
            f"{late_count}건으로 {share}입니다."
        )
    groups = ", ".join(
        f"{value} {count}건" for value, count in sorted(counts.items())
    )
    return f"{source_label}의 {supplied}{label} 분포는 {groups}입니다."


def _subject_particle(value: str) -> str:
    last = value[-1]
    if "가" <= last <= "힣":
        return "이" if (ord(last) - ord("가")) % 28 else "가"
    return "이"


def _date_relations(
    records: Sequence[EvidenceRecord],
    source_label: str,
) -> tuple[RealizedClaim, ...]:
    output: list[RealizedClaim] = []
    for field in DATE_FIELDS:
        field_records = tuple(
            record for record in records if field_value(record, field) is not None
        )
        values = tuple(field_value(record, field) for record in field_records)
        present = tuple(value for value in values if value is not None)
        if len(present) < 2:
            continue
        output.append(
            _relation(
                "RANGE",
                field_records,
                field,
                f"{source_label} 레코드의 {FIELD_LABELS.get(field, field)} 범위는 "
                f"{min(present)}부터 "
                f"{max(present)}까지입니다.",
            )
        )
        if len(set(present)) > 1:
            output.append(
                _relation(
                    "ORDER_BY_TIME",
                    field_records,
                    field,
                    f"{source_label} 레코드의 {FIELD_LABELS.get(field, field)} 순으로 "
                    f"보면 {min(present)}이 "
                    f"가장 이르고 {max(present)}이 가장 늦습니다.",
                )
            )
        simultaneous = Counter(present).most_common(1)[0]
        if simultaneous[1] >= 2:
            output.append(
                _relation(
                    "SIMULTANEITY",
                    field_records,
                    field,
                    f"{source_label} 레코드는 {simultaneous[0]}에 "
                    f"{simultaneous[1]}건이 같은 시점으로 확인됩니다.",
                )
            )
    return tuple(output)


def _relation(
    operator_id: T2Operator,
    records: Sequence[EvidenceRecord],
    field: str | None,
    sentence: str,
    *,
    field_path: str | None = None,
) -> RealizedClaim:
    record_ids = tuple(record.evidence_id for record in records)
    field_path = field_path or (f"payload.{field}" if field else None)
    proof = RecomputationEvidence(
        operator_id=operator_id,
        record_ids=record_ids,
        field_path=field_path,
        expected=compute_value(operator_id, records, field_path),
    )
    arguments = tuple(
        ClaimArgument(
            record_id=record.evidence_id,
            field_path=field_path or "evidence_id",
            value_hash=sha256(
                (field_value(record, field) if field else record.evidence_id).encode(
                    "utf-8"
                )
            ).hexdigest(),
        )
        for record in records
    )
    digest = sha256(sentence.encode("utf-8")).hexdigest()
    support = sha256(json.dumps(record_ids).encode("utf-8")).hexdigest()
    return RealizedClaim(
        claim=ClaimIR(
            claim_id=f"CLAIM-REALIZED-{digest[:16]}",
            claim_type="T2",
            predicate_id=operator_id,
            arguments=arguments,
            support_set_id=f"SUPPORT-{support[:16]}",
            operator_id=operator_id,
            entity_scope=record_ids,
            causal_level="NONE",
            modality="OBSERVED",
        ),
        text=sentence,
        recomputation=proof,
    )
