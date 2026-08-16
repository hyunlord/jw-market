from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceSet
from jw_chat_agent_poc.service.v4.surface_binding import prune_empty_surface_sections


DowngradeAction = Literal["retain", "delete"]


class PredicateDowngrade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    original_predicate_id: str
    predicate_id: str | None
    action: DowngradeAction
    causal_level: Literal["NONE", "TEMPORAL", "ASSOCIATION"]
    reason_code: str


class SemanticSurfaceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    transformations: tuple[dict[str, str], ...] = ()
    downgrade_count: int = 0
    deletion_count: int = 0
    removed_empty_headings: int = 0


class SemanticEvidenceContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    has_temporal_support: bool
    supported_text: str
    temporal_support_texts: tuple[str, ...] = ()
    observed_count: int
    requested_count: int
    protected_line_sha256: tuple[str, ...] = ()
    has_hira_patient_count: bool = False
    hira_code_count: int = 0


_CAUSE_SENTENCE_PATTERNS = (
    re.compile(
        r"^(?P<left>.+?)(?:이|가)\s+(?P<right>.+?)(?:을|를)\s+"
        r"(?:일으켰|야기했)(?:습니다|다)\.?$"
    ),
    re.compile(
        r"^(?P<left>.+?)(?:은|는)\s+(?P<right>.+?)\s+"
        r"때문(?:입니다|이다)\.?$"
    ),
    re.compile(
        r"^(?P<left>.+?)\s+때문에\s+(?P<right>.+?)(?:입니다|이다|습니다|다)\.?$"
    ),
    re.compile(
        r"^(?P<left>.+?)의\s+원인은\s+(?P<right>.+?)(?:입니다|이다)\.?$"
    ),
    re.compile(
        r"^(?P<left>.+?)(?:이|가)\s+(?P<right>.+?)(?:에|에게)\s+"
        r"영향을\s*(?:줬|주었|미쳤)(?:습니다|다)\.?$"
    ),
    re.compile(
        r"^(?P<left>.+?)\s+(?:causes?|caused)\s+(?P<right>.+?)\.?$",
        re.IGNORECASE,
    ),
)
_CAUSAL_ASSERTION_RE = re.compile(
    r"(?:때문|원인|기인|야기|일으(?:키|켰)|영향을|"
    r"\bcaus(?:e(?:s|d)?|ing|al(?:ity|ly)?|ation|ative)\b)",
    re.IGNORECASE,
)
_TREND_RE = re.compile(
    r"(?:전망됩니다|예상됩니다|것으로\s*(?:전망|예상)|다가온다|"
    r"증가할\s*것|감소할\s*것|확대될\s*것|축소될\s*것)"
)
_GLOBAL_ABSENCE_RE = re.compile(r"(?:전\s*세계|모든|전체).{0,40}(?:없습니다|존재하지\s*않)")
_DATE_VALUE_RE = re.compile(
    r"^20\d{2}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?$"
)
_TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_CAUSAL_LIMIT_TEXT = "[확인 한계] 인과 관계는 이 조회로 확정하지 않습니다."
_TREND_LIMIT_TEXT = "[확인 한계] 전망은 이 조회로 확정하지 않습니다."
_HIRA_RATE_OR_RISK_RE = re.compile(r"(?:발생\s*위험|발생률|유병률)")
_HIRA_LIMITATION_RE = re.compile(
    r"(?:아니|다르|판단하지|말할\s*수\s*없|확인되지|제시하지|산출하지)"
)
_HIRA_CARE_PATHWAY_RE = re.compile(
    r"(?=.*외래)(?=.*입원)(?=.*(?:만성|관리))(?=.*(?:주로|보여|시사|명확|뚜렷)).+"
)
_HIRA_RATE_LIMIT_TEXT = (
    "[확인 한계] 이 자료는 주상병 기준 청구 실인원이며, 인구 분모가 없어 "
    "성별·연령별 발생 위험이나 유병률을 판단하지 않습니다."
)
_HIRA_CARE_LIMIT_TEXT = (
    "[확인 한계] 외래·입원 실인원만으로 진료 방식이나 만성 관리 여부를 "
    "판단하지 않습니다."
)
_HIRA_SUM_LIMIT_TEXT = (
    "[확인 한계] 제시된 값은 코드별 실인원이며, 코드 간 중복 제거 여부가 "
    "확인되지 않아 합산한 총계는 제시하지 않습니다."
)


def downgrade_predicate(
    predicate_id: str,
    context: SemanticEvidenceContext,
) -> PredicateDowngrade:
    if predicate_id == "CAUSES" and context.has_temporal_support:
        return _retained(predicate_id, "TEMPORALLY_ASSOCIATED", "ASSOCIATION")
    if predicate_id == "GLOBAL_ABSENCE":
        return _retained(predicate_id, "NOT_FOUND_IN_THIS_QUERY", "NONE")
    if (
        predicate_id == "COMPLETE_COMPARISON"
        and context.observed_count < context.requested_count
    ):
        return _retained(predicate_id, "PARTIAL_SUBSET_COMPARISON", "NONE")
    return PredicateDowngrade(
        original_predicate_id=predicate_id,
        predicate_id=None,
        action="delete",
        causal_level="NONE",
        reason_code="predicate_has_no_supported_downgrade",
    )


def realize_semantic_surface(
    answer: str,
    context: SemanticEvidenceContext,
) -> SemanticSurfaceResult:
    output: list[str] = []
    transformations: list[dict[str, str]] = []
    downgrade_count = 0
    deletion_count = 0
    hira_rate_blocked = False
    hira_care_blocked = False
    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("```", "~~~")):
            output.append(line)
            continue
        if stripped.startswith("|"):
            updated, table_transformations, table_downgrades, table_deletions = (
                _sanitize_table_line(line, context)
            )
            output.append(updated)
            transformations.extend(table_transformations)
            downgrade_count += table_downgrades
            deletion_count += table_deletions
            continue
        if sha256(stripped.encode("utf-8")).hexdigest() in context.protected_line_sha256:
            output.append(line)
            continue
        policy_text, markdown_prefix = _policy_text(stripped)
        if context.has_hira_patient_count and _unsupported_hira_rate_claim(policy_text):
            transformations.append(
                {
                    "from": "HIRA_RATE_OR_RISK",
                    "to": "DELETE",
                    "reason": "patient_count_has_no_population_denominator",
                }
            )
            deletion_count += 1
            hira_rate_blocked = True
            continue
        if context.has_hira_patient_count and _HIRA_CARE_PATHWAY_RE.search(policy_text):
            transformations.append(
                {
                    "from": "HIRA_CARE_PATHWAY_INTERPRETATION",
                    "to": "DELETE",
                    "reason": "patient_counts_do_not_establish_care_pathway",
                }
            )
            deletion_count += 1
            hira_care_blocked = True
            continue
        if _TREND_RE.search(policy_text):
            transformations.append(
                {"from": "TREND_PREDICTION", "to": "DELETE", "reason": "unsupported"}
            )
            deletion_count += 1
            continue
        causal_clauses = _causal_clauses(policy_text)
        if causal_clauses is not None or _CAUSAL_ASSERTION_RE.search(policy_text):
            left, right = causal_clauses or ("", "")
            clauses_bound = _clauses_temporally_bound(left, right, context)
            decision = downgrade_predicate(
                "CAUSES",
                context.model_copy(
                    update={
                        "has_temporal_support": (
                            context.has_temporal_support and clauses_bound
                        )
                    }
                ),
            )
            if decision.action == "delete":
                deletion_count += 1
                transformations.append(
                    {"from": "CAUSES", "to": "DELETE", "reason": decision.reason_code}
                )
                continue
            indent = line[: len(line) - len(line.lstrip())]
            output.append(
                f"{indent}{markdown_prefix}[관찰적 연결] {left}와 "
                f"{right}는 시간상 함께 관찰되었습니다."
            )
            transformations.append(
                {
                    "from": "CAUSES",
                    "to": str(decision.predicate_id),
                    "reason": decision.reason_code,
                }
            )
            downgrade_count += 1
            continue
        updated = line
        if context.observed_count < context.requested_count and re.search(
            r"(?:모든|전체)\s*브랜드(?:를|가|는|의)?\s*(?:완전하게\s*)?비교",
            policy_text,
        ):
            updated = re.sub(r"(?:모든|전체)\s*브랜드", "확인된 일부 브랜드", line)
            transformations.append(
                {
                    "from": "COMPLETE_COMPARISON",
                    "to": "PARTIAL_SUBSET_COMPARISON",
                    "reason": "partial_entity_snapshot",
                }
            )
            downgrade_count += 1
        if _GLOBAL_ABSENCE_RE.search(updated):
            updated = re.sub(
                r"(?:전\s*세계|모든|전체)",
                "이번 조회 범위에서",
                updated,
                count=1,
            )
            transformations.append(
                {
                    "from": "GLOBAL_ABSENCE",
                    "to": "NOT_FOUND_IN_THIS_QUERY",
                    "reason": "query_scoped_evidence",
                }
            )
            downgrade_count += 1
        output.append(updated)
    if hira_rate_blocked and _HIRA_RATE_LIMIT_TEXT not in output:
        output.append(_HIRA_RATE_LIMIT_TEXT)
    if hira_care_blocked and _HIRA_CARE_LIMIT_TEXT not in output:
        output.append(_HIRA_CARE_LIMIT_TEXT)
    if context.has_hira_patient_count and context.hira_code_count >= 2:
        if _HIRA_SUM_LIMIT_TEXT not in output:
            output.append(_HIRA_SUM_LIMIT_TEXT)
        transformations.append(
            {
                "from": "HIRA_CODE_SUM",
                "to": "NOT_REPORTED",
                "reason": "cross_code_deduplication_unknown",
            }
        )
    surface_text, removed_empty_headings = prune_empty_surface_sections(
        "\n".join(output)
    )
    return SemanticSurfaceResult(
        text=surface_text,
        transformations=tuple(transformations),
        downgrade_count=downgrade_count,
        deletion_count=deletion_count,
        removed_empty_headings=removed_empty_headings,
    )


def _unsupported_hira_rate_claim(value: str) -> bool:
    matches = tuple(_HIRA_RATE_OR_RISK_RE.finditer(value))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        if not _HIRA_LIMITATION_RE.search(value[match.start() : end]):
            return True
    return False


def evidence_has_hira_patient_count(evidence_sets: Sequence[EvidenceSet]) -> bool:
    return any(
        evidence_set.source == "hira"
        and any(
            record.result_kind == "patient_count"
            or "ptntcnt" in {key.casefold() for key in _mapping_keys(record.payload)}
            for record in evidence_set.records
        )
        for evidence_set in evidence_sets
    )


def evidence_hira_code_count(evidence_sets: Sequence[EvidenceSet]) -> int:
    codes = {
        match.group(0).upper()
        for evidence_set in evidence_sets
        if evidence_set.source == "hira"
        for record in evidence_set.records
        for value in _scalar_values(record.payload)
        for match in re.finditer(r"(?<![A-Za-z0-9])[A-Z]\d{2,3}(?![A-Za-z0-9])", value)
    }
    return len(codes)


def _mapping_keys(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        keys: list[str] = []
        for key, nested in value.items():
            keys.append(str(key))
            keys.extend(_mapping_keys(nested))
        return tuple(keys)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(key for nested in value for key in _mapping_keys(nested))
    return ()


def _policy_text(stripped: str) -> tuple[str, str]:
    heading = re.match(r"^(?P<prefix>#{1,6}\s+)(?P<body>.+)$", stripped)
    if heading is None:
        return stripped, ""
    return heading.group("body"), heading.group("prefix")


def _sanitize_table_line(
    line: str,
    context: SemanticEvidenceContext,
) -> tuple[str, tuple[dict[str, str], ...], int, int]:
    cells = line.split("|")
    transformations: list[dict[str, str]] = []
    downgrade_count = 0
    deletion_count = 0
    for index, raw_cell in enumerate(cells):
        cell = raw_cell.strip()
        if not cell or _TABLE_SEPARATOR_RE.fullmatch(cell):
            continue
        if context.has_hira_patient_count and _unsupported_hira_rate_claim(cell):
            cells[index] = f" {_HIRA_RATE_LIMIT_TEXT} "
            transformations.append(
                {
                    "from": "HIRA_RATE_OR_RISK",
                    "to": "DELETE",
                    "reason": "patient_count_has_no_population_denominator",
                }
            )
            deletion_count += 1
            continue
        if context.has_hira_patient_count and _HIRA_CARE_PATHWAY_RE.search(cell):
            cells[index] = f" {_HIRA_CARE_LIMIT_TEXT} "
            transformations.append(
                {
                    "from": "HIRA_CARE_PATHWAY_INTERPRETATION",
                    "to": "DELETE",
                    "reason": "patient_counts_do_not_establish_care_pathway",
                }
            )
            deletion_count += 1
            continue
        if _TREND_RE.search(cell):
            cells[index] = f" {_TREND_LIMIT_TEXT} "
            transformations.append(
                {"from": "TREND_PREDICTION", "to": "DELETE", "reason": "unsupported"}
            )
            deletion_count += 1
            continue
        if _CAUSAL_ASSERTION_RE.search(cell):
            cells[index] = f" {_CAUSAL_LIMIT_TEXT} "
            transformations.append(
                {"from": "CAUSES", "to": "DELETE", "reason": "unsupported_table_claim"}
            )
            deletion_count += 1
            continue
        updated = cell
        if context.observed_count < context.requested_count and re.search(
            r"(?:모든|전체)\s*브랜드(?:를|가|는|의)?\s*(?:완전하게\s*)?비교",
            updated,
        ):
            updated = re.sub(r"(?:모든|전체)\s*브랜드", "확인된 일부 브랜드", updated)
            transformations.append(
                {
                    "from": "COMPLETE_COMPARISON",
                    "to": "PARTIAL_SUBSET_COMPARISON",
                    "reason": "partial_entity_snapshot",
                }
            )
            downgrade_count += 1
        if _GLOBAL_ABSENCE_RE.search(updated):
            updated = re.sub(
                r"(?:전\s*세계|모든|전체)",
                "이번 조회 범위에서",
                updated,
                count=1,
            )
            transformations.append(
                {
                    "from": "GLOBAL_ABSENCE",
                    "to": "NOT_FOUND_IN_THIS_QUERY",
                    "reason": "query_scoped_evidence",
                }
            )
            downgrade_count += 1
        if updated != cell:
            cells[index] = f" {updated} "
    return "|".join(cells), tuple(transformations), downgrade_count, deletion_count


def evidence_has_temporal_support(evidence_sets: Sequence[EvidenceSet]) -> bool:
    dates = {
        value
        for evidence_set in evidence_sets
        for record in evidence_set.records
        for value in _date_values(record.payload)
    }
    return len(dates) >= 2


def evidence_support_text(evidence_sets: Sequence[EvidenceSet]) -> str:
    return " ".join(
        value
        for evidence_set in evidence_sets
        for record in evidence_set.records
        for value in _scalar_values(record.payload)
    )


def evidence_temporal_support_texts(
    evidence_sets: Sequence[EvidenceSet],
) -> tuple[str, ...]:
    return tuple(
        " ".join(_scalar_values(record.payload))
        for evidence_set in evidence_sets
        for record in evidence_set.records
        if _date_values(record.payload)
    )


def _causal_clauses(sentence: str) -> tuple[str, str] | None:
    for pattern in _CAUSE_SENTENCE_PATTERNS:
        match = pattern.match(sentence)
        if match is not None:
            return match.group("left").strip(), match.group("right").strip()
    return None


def _clauses_temporally_bound(
    left: str,
    right: str,
    context: SemanticEvidenceContext,
) -> bool:
    if not context.has_temporal_support:
        return False
    normalized_left = _normalized_text(left)
    normalized_right = _normalized_text(right)
    if not normalized_left or not normalized_right:
        return False
    normalized_records = tuple(
        _normalized_text(value) for value in context.temporal_support_texts
    )
    left_records = {
        index
        for index, value in enumerate(normalized_records)
        if normalized_left in value
    }
    right_records = {
        index
        for index, value in enumerate(normalized_records)
        if normalized_right in value
    }
    return any(left_index != right_index for left_index in left_records for right_index in right_records)


def _normalized_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).casefold()


def _scalar_values(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return tuple(
            text for nested in value.values() for text in _scalar_values(nested)
        )
    if isinstance(value, (list, tuple)):
        return tuple(text for nested in value for text in _scalar_values(nested))
    return () if value in (None, "") else (str(value),)


def _date_values(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return tuple(
            date
            for nested in value.values()
            for date in _date_values(nested)
        )
    if isinstance(value, (list, tuple)):
        return tuple(date for nested in value for date in _date_values(nested))
    text = str(value)
    return (text,) if _DATE_VALUE_RE.fullmatch(text) else ()


def _retained(
    original: str,
    downgraded: str,
    causal_level: Literal["NONE", "TEMPORAL", "ASSOCIATION"],
) -> PredicateDowngrade:
    if original == downgraded:
        return PredicateDowngrade(
            original_predicate_id=original,
            predicate_id=None,
            action="delete",
            causal_level="NONE",
            reason_code="predicate_unchanged",
        )
    return PredicateDowngrade(
        original_predicate_id=original,
        predicate_id=downgraded,
        action="retain",
        causal_level=causal_level,
        reason_code="predicate_transformed",
    )
