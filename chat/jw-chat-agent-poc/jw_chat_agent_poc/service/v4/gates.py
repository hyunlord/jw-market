from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import unquote, urlparse

from jw_chat_agent_poc.service.v4.comparison_facts import comparison_numeric_tokens
from jw_chat_agent_poc.service.v4.contracts import GatedAnswer, SourceResult
from jw_chat_agent_poc.service.v4.display import normalize_answer_surface
from jw_chat_agent_poc.service.v4.reason_code_enforcement import (
    enforce_reason_codes,
    scrub_internal_release_tokens,
    typed_absence_record,
)
from jw_chat_agent_poc.service.v4.source_labels import (
    PATENT_LANES,
    normalize_public_source_surface,
    patent_lane_label,
    public_source_aliases,
    public_source_label,
)
from jw_chat_agent_poc.service.v4.source_derived_metrics import (
    build_hira_derived_outcome,
)


_NUMBER_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
_IPV4_SURFACE_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_INTERNAL_ENDPOINT_SURFACE_RE = re.compile(
    r"(?:"
    r"\b(?:localhost|mcp-[a-z0-9.-]*|code-serving-[a-z0-9.-]*|read-only[a-z0-9.-]*)"
    r"(?::\d+|/|\b)"
    r"|(?:[a-z0-9-]+\.)*svc(?:\.cluster\.local)?(?::\d+|/|\b)"
    r"|[a-z0-9.-]*-svc(?::\d+|/)"
    r")",
    re.IGNORECASE,
)
_UNSIGNED_DECREASE_RE = re.compile(
    r"(?<![-\w.])(?P<value>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:억원|%p|%|Rx)?(?:이|가)?\s*(?:감소|하락|줄(?:었|어|었습니다|었다))",
    re.IGNORECASE,
)
_RAW_WON_RE = re.compile(
    r"(?<![\w.])\d[\d,]{6,}(?:\.\d+)?\s*"
    r"(?:\(\s*(?:원|KRW)\s*\)|원|KRW)"
    r"(?:은|는|이|가|을|를|으로|에서|의)?",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(
    r"(?P<value>-?\d[\d,]*(?:\.\d+)?)\s*(?:\(\s*%(?:p)?\s*\)|%(?:p\b)?)",
    re.IGNORECASE,
)
_VALUE = r"-?\d[\d,]*(?:\.\d+)?"
_VOLUME_PATTERN = re.compile(
    rf"(?:처방량|판매량|수량)\s*(?:은|는|이|가|:)?\s*(?:약\s*)?(?P<value>{_VALUE})\s*(?:Rx|건|개)?",
    re.IGNORECASE,
)
_MART_VALUE_PATTERNS: dict[str, re.Pattern[str]] = {
    "매출": re.compile(
        rf"(?:매출(?:액)?\s*(?:은|는|이|가|:)?\s*(?:약\s*)?(?:KRW\s*)?"
        rf"(?P<after>{_VALUE})|(?:KRW\s*)?(?P<before>{_VALUE})\s*(?:억원?|원|KRW))",
        re.IGNORECASE,
    ),
    "점유율": re.compile(
        rf"(?:점유율\s*(?:은|는|이|가|:)?\s*(?:약\s*)?(?P<after>{_VALUE})|"
        rf"(?P<before>{_VALUE})\s*(?:%|퍼센트))"
    ),
    "성장률": re.compile(
        rf"(?:성장률\s*(?:은|는|이|가|:)?\s*(?:약\s*)?(?P<after>{_VALUE})|"
        rf"(?P<before>{_VALUE})\s*(?:%|퍼센트))"
    ),
    "시장 규모": re.compile(
        rf"시장\s*규모\s*(?:은|는|이|가|:)?\s*(?:약\s*)?(?P<after>{_VALUE})\s*(?:억원?|원|KRW)?",
        re.IGNORECASE,
    ),
    "순위": re.compile(r"(?P<value>\d[\d,]*)\s*위"),
    "hhi": re.compile(r"(?:HHI\D{0,12}(?P<after>\d[\d,]*(?:\.\d+)?)|(?P<before>\d[\d,]*(?:\.\d+)?)\D{0,4}HHI)", re.IGNORECASE),
    "처방량": _VOLUME_PATTERN,
    "판매량": _VOLUME_PATTERN,
    "수량": _VOLUME_PATTERN,
}
_MART_TERMS = (
    "매출",
    "점유율",
    "순위",
    "hhi",
    "성장률",
    "시장 규모",
    "시장규모",
    "처방량",
    "판매량",
    "수량",
)
_METRIC_FIELDS: dict[str, tuple[str, ...]] = {
    "매출": ("sales", "amount", "value"),
    "점유율": ("share", "percentage", "percent", "pct"),
    "순위": ("rank",),
    "hhi": ("hhi",),
    "성장률": ("growth", "yoy", "cagr", "delta"),
    "시장 규모": ("market_size", "market_value", "value"),
    "처방량": ("prescription_volume", "volume", "value"),
    "판매량": ("prescription_volume", "volume", "value"),
    "수량": ("prescription_volume", "volume", "value"),
}
_CONTEXT_FIELDS = ("period", "year", "month", "yyyymm")
_DIMENSION_LABELS = {
    "specialty": "진료과",
    "channel": "유통채널",
}
_PERCENTAGE_FIELD_MARKERS = ("pct", "percent", "percentage", "share")
_CONFIRMED_CAUSE_RE = re.compile(
    r"[^.\n]*(?:원인으로\s*확인|때문인\s*것으로\s*확인|원인은)[^.\n]*(?:\.|$)"
)
_CLAIM_PATTERNS: dict[str, re.Pattern[str]] = {
    "patient_count": re.compile(r"환자\s*수|청구\s*실인원"),
    "cost": re.compile(r"진료비|요양급여비용|보험자부담금"),
    "reimbursement": re.compile(r"급여\s*기준|투여\s*기준|고시"),
    "approval": re.compile(r"(?:허가|승인)(?:일|현황|되|받|됨|\s*문서)"),
    "label": re.compile(r"효능\s*효과|적응증|용법|용량|주의사항"),
    "patent": re.compile(r"특허|재심사|만료"),
    "study_design": re.compile(r"시험\s*디자인|무작위|눈가림|맹검"),
    "phase": re.compile(r"(?:1|2|3|4)상|PHASE", re.IGNORECASE),
    "recruitment_status": re.compile(r"모집\s*상태|진행\s*중|완료|철회"),
    "enrollment": re.compile(r"등록\s*(?:수|인원)|피험자\s*수"),
    "eligibility": re.compile(r"선정\s*기준|제외\s*기준|선정제외기준"),
    "absence_confirmation": re.compile(
        r"현재\s*(?:급여\s*기준이?\s*없습니다(?:\s*\(\s*비급여\s*\))?"
        r"|허가\s*문서를\s*확인할\s*수\s*없습니다)"
    ),
    "absence_confirmation:reimbursement": re.compile(
        r"현재\s*급여\s*기준이?\s*없습니다(?:\s*\(\s*비급여\s*\))?"
    ),
    "absence_confirmation:approval": re.compile(
        r"현재\s*허가\s*문서를\s*확인할\s*수\s*없습니다"
    ),
}
_SOURCE_TAG_ALIASES: dict[str, tuple[str, ...]] = {
    source: public_source_aliases(source)
    for source in (
        "mart",
        "nedrug",
        "hira",
        "openfda",
        "clinicaltrials",
        "web",
        "patent",
    )
}
_AUTOMATIC_SAFETY_NOTICES = (
    "HIRA 환자수는 주상병 기준 청구 실인원이며 유병률과 다릅니다.",
    "FAERS/OpenFDA는 자발적 보고 자료로 인과관계나 발생률 산출에 쓸 수 없습니다.",
    "ClinicalTrials.gov 모집상태는 갱신이 지연될 수 있습니다.",
    "특허 존속기간 만료가 곧 제네릭 진입 시점을 뜻하지 않습니다.",
)
_ACTIVE_KR_EMPTY_NOTICE = "확인된 국내 진행 중 임상시험은 없었습니다."
_ACTIVE_TRIAL_STATUSES = {
    "ACTIVE_NOT_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "NOT_YET_RECRUITING",
    "RECRUITING",
}
_HIRA_REQUEST_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"환자\s*수|청구\s*실인원"), ("ptntCnt",)),
    (
        re.compile(r"진료비|비용|요양급여비용|보험자부담금"),
        ("rvdInsupBrdnAmt", "rvdRpeTamtAmt"),
    ),
    (re.compile(r"방문\s*일수|내원\s*일수"), ("vstDdcnt",)),
    (re.compile(r"명세서\s*건수"), ("specCnt",)),
)
_HIRA_PUBLIC_FIELDS: dict[str, tuple[str, str]] = {
    "ptntCnt": ("환자수", "명"),
    "rvdInsupBrdnAmt": ("보험자부담금", "원"),
    "rvdRpeTamtAmt": ("요양급여비용총액", "원"),
    "specCnt": ("명세서건수", "건"),
    "vstDdcnt": ("방문일수", "일"),
}
_HIRA_COST_FIELDS = frozenset({"rvdInsupBrdnAmt", "rvdRpeTamtAmt"})
_MARKDOWN_SENTENCE_RE = re.compile(
    r".+?(?:[.!?](?:\s*\[출처:\s*[^\]]+\])?|$)(?=\s+|$)"
)
_HIRA_PATIENT_SURFACE_RE = re.compile(
    r"환자\s*수(?:는|은|이|가|:)?\s*(?P<value>\d[\d,]*)\s*명"
)


@dataclass(frozen=True)
class _HiraSurfaceFact:
    subject: str
    year: str
    care_type: str
    field: str
    label: str
    value: str
    display: str


def apply_v4_gates(
    question: str,
    answer: str,
    results: tuple[SourceResult, ...],
) -> GatedAnswer:
    trace: dict[str, Any] = {}
    text = answer.strip()
    mart_results = tuple(item for item in results if item.source == "mart" and item.status == "ok")

    requested_source = _requested_source(question)
    available_sources = _mart_source_labels(mart_results)
    if requested_source and requested_source not in available_sources:
        text = _without_numbers(text)
        text = _append_sentence(text, f"요청한 {requested_source} 근거를 확보하지 못했습니다. 다른 출처의 값을 {requested_source} 값으로 대체하지 않습니다.")
        trace["source_impersonation"] = {"blocked": True, "requested": requested_source}
    else:
        trace["source_impersonation"] = {"blocked": False}

    if _asks_cross_source_sum(question):
        text = _without_numbers(text)
        text = _append_sentence(text, "UBIST와 IQVIA는 측정 체계와 분모가 달라 합산하지 않습니다.")
        trace["cross_source_sum"] = {"blocked": True}
    else:
        trace["cross_source_sum"] = {"blocked": False}

    mart_numeric_question = any(term in question.casefold() for term in _MART_TERMS)
    metric_fields = _requested_metric_fields(question)
    asks_cause = any(marker in question.casefold() for marker in ("원인", "왜 ", "이유"))
    requested_display_numbers = _requested_display_numbers(
        mart_results,
        question,
        metric_fields,
    )
    allowed = _payload_numbers(
        mart_results if mart_numeric_question else results,
        allowed_fields=(
            () if asks_cause else metric_fields
        ) if mart_numeric_question else (),
    )
    comparison_tokens = comparison_numeric_tokens(mart_results)
    allowed.update(comparison_tokens)
    allowed.update(_unsigned_comparison_decreases(text, comparison_tokens))
    answer_numbers = _answer_mart_metric_numbers(text, question)
    invented = sorted(token for token in answer_numbers if _normalize_number(token) not in allowed)
    if invented and mart_results and mart_numeric_question:
        requested_metric_terms = {
            metric for metric in _MART_VALUE_PATTERNS if metric.casefold() in question.casefold()
        }
        natural_replacement = (
            next(iter(requested_display_numbers))
            if len(invented) == 1
            and len(requested_display_numbers) == 1
            and len(requested_metric_terms) == 1
            else None
        )
        text = _redact_invented_metric_values(
            text,
            invented,
            replacement=natural_replacement,
        )
        remaining = sorted(
            token
            for token in _answer_mart_metric_numbers(text, question)
            if _normalize_number(token) not in allowed
        )
        trace["mart_numeric_copy_only"] = {
            "blocked": True,
            "tokens": invented,
            "remaining_tokens": remaining,
            "full_fallback": False,
        }
    else:
        trace["mart_numeric_copy_only"] = {
            "blocked": False,
            "tokens": [],
            "full_fallback": False,
        }

    rendered_numbers = {_normalize_number(token) for token in _NUMBER_RE.findall(text)}
    metric_missing = (
        bool(requested_display_numbers)
        and requested_display_numbers.isdisjoint(rendered_numbers)
    ) or bool(invented and _render_mart_history(mart_results, question))
    can_repair = not trace["source_impersonation"]["blocked"] and not trace[
        "cross_source_sum"
    ]["blocked"]
    if metric_missing and can_repair:
        verified_summary = _render_mart_facts(
            mart_results,
            question=question,
            allowed_fields=metric_fields,
        )
        verified_summary, _ = normalize_answer_surface(verified_summary)
        text = _merge_unique_blocks(verified_summary, text)
    trace["requested_metric_surface"] = {
        "repaired": metric_missing and can_repair,
        "expected_display_numbers": sorted(requested_display_numbers),
    }

    timed_out = tuple(item for item in results if item.status == "timeout")
    trace["delayed_sources"] = list(
        dict.fromkeys(item.source for item in timed_out)
    )

    usable_evidence = tuple(
        item.evidence for item in results if item.status == "ok" and item.evidence is not None
    )
    causal_unsupported = asks_cause and bool(usable_evidence) and not any(
        evidence.causal is True for evidence in usable_evidence
    )
    confirmed_cause = bool(_CONFIRMED_CAUSE_RE.search(text))
    if causal_unsupported and confirmed_cause:
        text = _CONFIRMED_CAUSE_RE.sub("", text).strip()
        text = _append_sentence(
            text,
            "확인된 자료는 관찰 근거이므로 구체적 원인은 확인되지 않았습니다. 현재 근거로는 관련 가능성만 해석할 수 있습니다.",
        )
    trace["causal_claim_guard"] = {
        "blocked": causal_unsupported and confirmed_cause,
        "causal_evidence_available": any(
            evidence.causal is True for evidence in usable_evidence
        ),
    }

    text, reason_trace = enforce_reason_codes(text, results)
    trace["reason_code_enforcement"] = reason_trace

    text, public_source_rewrites = normalize_public_source_surface(text)
    trace["public_source_surface"] = {"rewritten": public_source_rewrites}

    raw_won_blocked = bool(_RAW_WON_RE.search(text))
    trace["surface_raw_won"] = {"blocked": raw_won_blocked}

    display_percentages = _mart_display_percentages(mart_results)
    raw_percentages = _mart_raw_percentages(mart_results) - display_percentages
    exposed_raw_percentages = {
        _normalize_number(match.group("value"))
        for match in _PERCENT_RE.finditer(text)
        if _normalize_number(match.group("value")) in raw_percentages
    }
    trace["surface_mart_percentage"] = {
        "blocked": bool(exposed_raw_percentages),
        "tokens": sorted(exposed_raw_percentages),
    }

    subset_scope_blocked = _generic_subset_unresolved(question, results)
    if subset_scope_blocked:
        text = (
            "공식 허가 근거에서 요청한 제네릭 제품 목록을 확인하지 못해 제품별 매출 1위를 "
            "판정할 수 없습니다. 확인된 본품 매출을 제네릭 매출로 대체하지 않습니다."
        )
    trace["subset_scope_guard"] = {
        "blocked": subset_scope_blocked,
        "reason": "generic_product_set_unresolved" if subset_scope_blocked else None,
    }

    hira_before = inspect_requested_hira_surface(question, text, results)
    invalid_patient_sentences, invalid_patient_values = _invalid_hira_patient_sentences(
        text,
        hira_before["expected"],
    )
    if invalid_patient_sentences:
        text = _remove_markdown_sentences(text, invalid_patient_sentences)

    text, claim_trace = _enforce_claim_eligibility(question, text, results)
    trace["claim_eligibility_guard"] = claim_trace

    hira_after_claims = inspect_requested_hira_surface(question, text, results)
    hira_repaired = bool(hira_after_claims["missing"])
    if hira_repaired:
        text = _merge_unique_blocks(
            _render_hira_surface_facts(hira_after_claims["expected"]),
            text,
        )
    hira_final = inspect_requested_hira_surface(question, text, results)
    trace["requested_hira_surface"] = {
        "repaired": hira_repaired,
        "expected": [_public_hira_fact(fact) for fact in hira_final["expected"]],
        "missing_before_repair": [
            _public_hira_fact(fact) for fact in hira_after_claims["missing"]
        ],
        "missing_after_repair": [
            _public_hira_fact(fact) for fact in hira_final["missing"]
        ],
        "misbound_patient_values": sorted(invalid_patient_values),
    }
    hira_derived = build_hira_derived_outcome(results)
    trace["hira_derived_metrics"] = [
        proof.model_dump(mode="json") for proof in hira_derived.proofs
    ]
    if hira_derived.text and all(proof.matched for proof in hira_derived.proofs):
        text = _replace_markdown_section(text, "종합 인사이트", hira_derived.text)
    if hira_derived.scope_notice:
        text = _replace_hira_scope_notice(text, hira_derived.scope_notice)

    requested_dimensions = _requested_dimension_levels(question)
    missing_dimension_blocks: list[str] = []
    repaired_dimensions: list[str] = []
    replaced_headings: set[str] = set()
    for level, block in _rendered_mart_dimension_blocks(mart_results):
        if level not in requested_dimensions:
            continue
        table_header = next(
            (
                line
                for line in block.splitlines()
                if line.startswith("| ") and not line.startswith("| ---")
            ),
            "",
        )
        if table_header and table_header not in text:
            missing_dimension_blocks.append(block)
            repaired_dimensions.append(level)
            heading = next(
                (line for line in block.splitlines() if line.startswith("## ")),
                "",
            )
            if heading:
                replaced_headings.add(heading)
    if missing_dimension_blocks:
        text = "\n".join(
            line for line in text.splitlines() if line.strip() not in replaced_headings
        ).strip()
        text = _merge_unique_blocks(*missing_dimension_blocks, text)
    trace["requested_dimension_surface"] = {
        "requested": list(requested_dimensions),
        "repaired": list(dict.fromkeys(repaired_dimensions)),
    }

    text = _merge_unique_blocks(text)

    text, surface_trace = normalize_answer_surface(text)
    text, late_internal_count = scrub_internal_release_tokens(text)
    trace["reason_code_enforcement"]["INTERNAL_TOKEN_LEAK"] += late_internal_count
    trace["final_surface"] = surface_trace

    text = _append_sources(text, results)
    text, final_source_rewrites = normalize_public_source_surface(text)
    trace["public_source_surface"]["rewritten"] += final_source_rewrites
    trace["sources_block"] = {"present": "## 출처" in text}
    return GatedAnswer(text=text, trace=trace)


def _unsigned_comparison_decreases(
    text: str,
    comparison_tokens: set[str],
) -> set[str]:
    negative_magnitudes = {
        token[1:] for token in comparison_tokens if token.startswith("-")
    }
    return {
        value
        for match in _UNSIGNED_DECREASE_RE.finditer(text)
        if (value := _normalize_number(match.group("value"))) in negative_magnitudes
    }


def _requested_source(question: str) -> str | None:
    lowered = question.casefold()
    if "iqvia" in lowered:
        return "IQVIA"
    if "ubist" in lowered:
        return "UBIST"
    return None


def _mart_source_labels(results: tuple[SourceResult, ...]) -> set[str]:
    labels: set[str] = set()
    for result in results:
        serialized = json.dumps(result.payload, ensure_ascii=False).upper()
        if "UBIST" in serialized:
            labels.add("UBIST")
        if "IQVIA" in serialized:
            labels.add("IQVIA")
    return labels


def _asks_cross_source_sum(question: str) -> bool:
    lowered = question.casefold()
    return "ubist" in lowered and "iqvia" in lowered and any(
        token in lowered for token in ("합쳐", "합산", "총매출", "더해")
    )


def _requested_metric_fields(question: str) -> tuple[str, ...]:
    lowered = question.casefold()
    fields = {
        field
        for term, field_names in _METRIC_FIELDS.items()
        if term in lowered
        for field in field_names
    }
    return tuple(sorted(fields | set(_CONTEXT_FIELDS)))


def _answer_mart_metric_numbers(text: str, question: str) -> set[str]:
    lowered = question.casefold()
    numbers: set[str] = set()
    for metric, pattern in _MART_VALUE_PATTERNS.items():
        if metric not in lowered:
            continue
        for match in pattern.finditer(text):
            value = match.groupdict().get("value")
            value = value or match.groupdict().get("after") or match.groupdict().get("before")
            if value:
                numbers.add(value)
    return numbers


def _redact_invented_metric_values(
    text: str,
    invented: list[str],
    *,
    replacement: str | None,
) -> str:
    blocked = {_normalize_number(token) for token in invented}

    def replace(match: re.Match[str]) -> str:
        if _normalize_number(match.group(0)) not in blocked:
            return match.group(0)
        return replacement or ""

    if replacement:
        return _NUMBER_RE.sub(replace, text)
    return "\n".join(
        line
        for raw_line in text.splitlines()
        if (line := _without_blocked_numeric_clauses(raw_line, blocked)).strip()
    ).strip()


_NUMERIC_CLAUSE_SEPARATOR_RE = re.compile(
    r"(\s*(?:,|;|·)\s*|\s*(?:이며|이고|이지만|하지만|다만)\s*)"
)


def _without_blocked_numeric_clauses(line: str, blocked: set[str]) -> str:
    if not any(
        _normalize_number(match.group(0)) in blocked for match in _NUMBER_RE.finditer(line)
    ):
        return line
    parts = _NUMERIC_CLAUSE_SEPARATOR_RE.split(line)
    clauses = parts[::2]
    separators = parts[1::2]
    keep = [
        not any(
            _normalize_number(match.group(0)) in blocked
            for match in _NUMBER_RE.finditer(clause)
        )
        for clause in clauses
    ]
    kept_indexes = [index for index, include in enumerate(keep) if include and clauses[index].strip()]
    if not kept_indexes:
        return ""
    output = clauses[kept_indexes[0]].rstrip()
    previous = kept_indexes[0]
    for index in kept_indexes[1:]:
        separator = separators[previous] if index == previous + 1 else ". "
        output += separator + clauses[index].lstrip()
        previous = index
    original_end = line.rstrip()[-1:] if line.rstrip() else ""
    if original_end in ".!?" and not output.rstrip().endswith((".", "!", "?")):
        output = output.rstrip() + original_end
    return output


def _payload_numbers(
    results: tuple[SourceResult, ...],
    *,
    allowed_fields: tuple[str, ...] = (),
) -> set[str]:
    tokens: set[str] = set()
    for result in results:
        tokens.update(
            _session_state_numeric_tokens(
                result.payload,
                allowed_fields=allowed_fields,
            )
        )
        if allowed_fields:
            values = (
                value
                for path, value in _walk_scalars(result.payload)
                if any(field in path.casefold() for field in allowed_fields)
            )
            serialized = json.dumps(tuple(values), ensure_ascii=False, default=str)
        else:
            serialized = json.dumps(result.payload, ensure_ascii=False, default=str)
        for token in _NUMBER_RE.findall(serialized):
            tokens.update(_numeric_copy_variants(token))
    tokens.update(
        _mart_dimension_payload_numbers(results, allowed_fields=allowed_fields)
    )
    return tokens


def _session_state_numeric_tokens(
    payload: Any,
    *,
    allowed_fields: tuple[str, ...],
) -> set[str]:
    if not isinstance(payload, Mapping):
        return set()
    facts = payload.get("last_numeric_facts")
    if not isinstance(facts, (list, tuple)):
        return set()
    tokens: set[str] = set()
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        path = str(fact.get("path") or "").casefold()
        value = fact.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if allowed_fields and not any(field in path for field in allowed_fields):
            continue
        tokens.add(_normalize_number(str(value)))
    return tokens


def _normalize_number(value: str) -> str:
    return value.replace(",", "").lstrip("+")


def _numeric_copy_variants(value: str) -> set[str]:
    normalized = _normalize_number(value)
    variants = {normalized}
    try:
        decimal_value = Decimal(normalized)
    except InvalidOperation:
        return variants
    variants.add(format(decimal_value.normalize(), "f"))
    try:
        rounded = decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return variants
    variants.add(format(rounded, "f"))
    return variants


def _mart_dimension_payload_numbers(
    results: tuple[SourceResult, ...],
    *,
    allowed_fields: tuple[str, ...] = (),
) -> set[str]:
    tokens: set[str] = set()
    for render_data in _mart_dimension_renders(results):
        public_fields = _mart_dimension_public_numeric_fields(render_data)
        for row_key in ("level_segments", "level_top5_trend_series"):
            rows = render_data.get(row_key)
            if not isinstance(rows, list):
                continue
            for path, value in _walk_scalars(rows):
                leaf = path.rsplit(".", 1)[-1].casefold()
                if not _is_mart_number(value) or not _is_public_dimension_field(
                    leaf,
                    public_fields,
                    allowed_fields,
                ):
                    continue
                tokens.add(_normalize_number(str(value)))
                tokens.add(_normalize_number(_format_mart_value(value)))
                if any(marker in leaf for marker in _PERCENTAGE_FIELD_MARKERS):
                    tokens.add(_two_decimal_display(value))
    return tokens


def _mart_dimension_public_numeric_fields(
    render_data: Mapping[str, Any],
) -> set[str]:
    fields = {"rank", "value", "value_recent"}
    query_spec = render_data.get("query_spec")
    metrics = query_spec.get("metrics") if isinstance(query_spec, Mapping) else None
    metric_items = metrics if isinstance(metrics, (list, tuple)) else (metrics,)
    fields.update(
        str(metric).strip().casefold()
        for metric in metric_items
        if isinstance(metric, str) and metric.strip()
    )
    measure = render_data.get("measure")
    if isinstance(measure, str) and measure.strip():
        fields.add(measure.strip().casefold())
    return fields


def _is_public_dimension_field(
    leaf: str,
    public_fields: set[str],
    allowed_fields: tuple[str, ...],
) -> bool:
    is_public = (
        leaf in public_fields
        or any(field in leaf for field in public_fields if field not in {"value"})
        or any(marker in leaf for marker in _PERCENTAGE_FIELD_MARKERS)
    )
    if not is_public or not allowed_fields:
        return is_public

    requested_fields = set(allowed_fields) - set(_CONTEXT_FIELDS) - {"value"}
    if leaf in {"value", "value_recent"}:
        declared_fields = public_fields - {"value", "value_recent", "rank"}
        return any(
            requested in declared or declared in requested
            for requested in requested_fields
            for declared in declared_fields
        )
    return any(requested in leaf for requested in requested_fields)


def _two_decimal_display(value: Any) -> str:
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return _normalize_number(str(value))
    return format(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def _mart_dimension_renders(
    results: tuple[SourceResult, ...],
) -> tuple[Mapping[str, Any], ...]:
    candidates: list[tuple[int, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for result in results:
        calls = result.payload.get("calls") if isinstance(result.payload, Mapping) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            render_data = call.get("render_data") if isinstance(call, Mapping) else None
            if not isinstance(render_data, Mapping):
                continue
            level = str(render_data.get("level") or "").strip().casefold()
            if not level or level not in _declared_dimensions(render_data):
                continue
            trend_rows = render_data.get("level_top5_trend_series")
            segment_rows = render_data.get("level_segments")
            score = (
                2
                if isinstance(trend_rows, list) and trend_rows
                else 1
                if isinstance(segment_rows, list) and segment_rows
                else 0
            )
            if score == 0:
                continue
            fingerprint = json.dumps(
                render_data,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            candidates.append((score, render_data))

    trends = tuple(render for score, render in candidates if score == 2)
    return tuple(
        render
        for score, render in candidates
        if score == 2
        or not any(
            _mart_dimension_scope_matches(render, trend) for trend in trends
        )
    )


def _mart_dimension_scope_matches(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    if _mart_dimension_level_measure(left) != _mart_dimension_level_measure(right):
        return False
    for key in ("market", "brand", "source"):
        left_value = _mart_dimension_scope_value(left, key)
        right_value = _mart_dimension_scope_value(right, key)
        if left_value and right_value and left_value != right_value:
            return False
    return True


def _mart_dimension_level_measure(
    render_data: Mapping[str, Any],
) -> tuple[str, str]:
    return (
        str(render_data.get("level") or "").strip().casefold(),
        str(
            render_data.get("measure") or render_data.get("value_label") or "value"
        ).strip().casefold(),
    )


def _mart_dimension_scope_value(
    render_data: Mapping[str, Any],
    key: str,
) -> str:
    query_spec = render_data.get("query_spec")
    value = query_spec.get(key) if isinstance(query_spec, Mapping) else None
    if value in (None, ""):
        value = render_data.get(key)
    return str(value or "").strip().casefold()


def _declared_dimensions(render_data: Mapping[str, Any]) -> set[str]:
    query_spec = render_data.get("query_spec")
    if not isinstance(query_spec, Mapping):
        return set()
    dimensions: set[str] = set()
    for key in ("group_by", "dimensions"):
        value = query_spec.get(key)
        items = value if isinstance(value, (list, tuple)) else (value,)
        for item in items:
            if not isinstance(item, str):
                continue
            dimensions.update(
                token.strip().casefold() for token in item.split(",") if token.strip()
            )
    return dimensions


def _requested_display_numbers(
    results: tuple[SourceResult, ...],
    question: str,
    allowed_fields: tuple[str, ...],
) -> set[str]:
    summary_numbers: set[str] = set()
    for result in results:
        calls = result.payload.get("calls") if isinstance(result.payload, dict) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            summary = str(call.get("summary_text") or "").strip()
            summary_numbers.update(
                _normalize_number(token)
                for token in _answer_mart_metric_numbers(summary, question)
            )
    if summary_numbers:
        return summary_numbers

    numbers: set[str] = set()
    for result in results:
        for path, value in _walk_scalars(result.payload):
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            leaf = path.rsplit(".", 1)[-1].casefold()
            if not any(field in leaf for field in allowed_fields):
                continue
            if leaf in {"value", "amount"} or leaf.endswith("_value"):
                continue
            numbers.add(_normalize_number(str(value)))
    return numbers


def _mart_display_percentages(results: tuple[SourceResult, ...]) -> set[str]:
    percentages: set[str] = set()
    for result in results:
        calls = result.payload.get("calls") if isinstance(result.payload, dict) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            summary = str(call.get("summary_text") or "")
            percentages.update(
                _normalize_number(match.group("value"))
                for match in _PERCENT_RE.finditer(summary)
            )
    return percentages


def _mart_raw_percentages(results: tuple[SourceResult, ...]) -> set[str]:
    percentages: set[str] = set()
    percentage_fields = _METRIC_FIELDS["점유율"] + _METRIC_FIELDS["성장률"]
    for result in results:
        for path, value in _walk_scalars(result.payload):
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            leaf = path.rsplit(".", 1)[-1].casefold()
            if any(field in leaf for field in percentage_fields):
                percentages.add(_normalize_number(str(value)))
    return percentages


def _generic_subset_unresolved(
    question: str,
    results: tuple[SourceResult, ...],
) -> bool:
    lowered = question.casefold()
    generic_scope = "제네릭" in lowered or any(
        "제네릭" in result.query.casefold()
        for result in results
        if result.source == "nedrug" and result.status == "ok"
    )
    if not generic_scope or not any(
        term in lowered for term in ("매출", "순위", "가장 큰", "1위")
    ):
        return False
    root_match = re.match(r"\s*([^\s(]+)", question)
    if root_match is None:
        return False
    root = re.sub(r"[^0-9a-z가-힣]", "", root_match.group(1).casefold())
    item_names = [
        re.sub(r"[^0-9a-z가-힣]", "", str(value).casefold())
        for result in results
        if result.source == "nedrug" and result.status == "ok"
        for path, value in _walk_scalars(result.payload)
        if path.rsplit(".", 1)[-1].casefold() in {"item_name", "product_name"}
        and str(value).strip()
    ]
    return not item_names or all(name.startswith(root) for name in item_names)


def _without_numbers(text: str) -> str:
    lines = [line for line in text.splitlines() if not _NUMBER_RE.search(line)]
    return "\n".join(lines).strip()


def _render_mart_facts(
    results: tuple[SourceResult, ...],
    *,
    question: str,
    allowed_fields: tuple[str, ...],
) -> str:
    dimensions = render_mart_dimension_facts(results, question=question)
    history = _render_mart_history(results, question)
    if dimensions or history:
        return _merge_unique_blocks(dimensions, history)

    summaries: list[str] = []
    for result in results:
        calls = result.payload.get("calls") if isinstance(result.payload, dict) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            summary = str(call.get("summary_text") or "").strip()
            if summary and not re.search(
                r"MCP\s+returned|\btotalCount\b|\b(?:sickCd|ptntCnt)\b|"
                r"\b\d{7,}(?:\.\d+)?\s*KRW\b",
                summary,
                re.IGNORECASE,
            ):
                summaries.append(summary)
    if summaries:
        return "\n".join(dict.fromkeys(summaries))

    facts: list[str] = []
    for result in results:
        for key, value in _walk_scalars(result.payload):
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            lowered = key.casefold()
            if not any(field in lowered for field in allowed_fields):
                continue
            leaf = key.rsplit(".", 1)[-1]
            if leaf.endswith("_억원") or leaf.endswith("_eok"):
                label = "매출" if "sales" in lowered or "value" in lowered else "금액"
                facts.append(f"{label} {value}억원")
    if not facts:
        return "mart 근거는 확인했지만 복사 가능한 수치 필드를 찾지 못했습니다."
    return "확인된 내부 데이터마트 지표는 " + ", ".join(dict.fromkeys(facts[:20])) + "입니다."


def _requested_dimension_levels(question: str) -> tuple[str, ...]:
    lowered = question.casefold()
    levels: list[str] = []
    if "진료과" in lowered:
        levels.append("specialty")
    if re.search(r"(?:유통\s*채널|채널\s*별)", lowered):
        levels.append("channel")
    return tuple(levels)


def _rendered_mart_dimension_blocks(
    results: tuple[SourceResult, ...],
) -> tuple[tuple[str, str], ...]:
    blocks: list[tuple[str, str]] = []
    for render_data in _mart_dimension_renders(results):
        level = str(render_data.get("level") or "").strip()
        level_label = _DIMENSION_LABELS.get(level.casefold(), level)
        value_label = str(
            render_data.get("value_label") or render_data.get("measure") or "값"
        ).strip()
        unit = str(render_data.get("unit_label") or "").strip()
        trend_rows = render_data.get("level_top5_trend_series")
        rendered_trends = _render_mart_dimension_trends(
            trend_rows,
            render_data=render_data,
            level_label=level_label,
            value_label=value_label,
            unit=unit,
        )
        if rendered_trends:
            blocks.append((level.casefold(), rendered_trends))
            continue
        segment_rows = render_data.get("level_segments")
        rendered_segments = _render_mart_dimension_segments(
            segment_rows,
            render_data=render_data,
            level_label=level_label,
            value_label=value_label,
            unit=unit,
        )
        if rendered_segments:
            blocks.append((level.casefold(), rendered_segments))
    return tuple(blocks)


def render_mart_dimension_facts(
    results: tuple[SourceResult, ...],
    *,
    question: str = "",
) -> str:
    requested_levels = set(_requested_dimension_levels(question))
    return _merge_unique_blocks(
        *(
            block
            for level, block in _rendered_mart_dimension_blocks(results)
            if not question or level in requested_levels
        )
    )


def _render_mart_dimension_trends(
    rows: Any,
    *,
    render_data: Mapping[str, Any],
    level_label: str,
    value_label: str,
    unit: str,
) -> str:
    if not isinstance(rows, list):
        return ""
    rendered_rows: list[tuple[str, str, Any, str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "").strip()
        series = row.get("series")
        if not name or not isinstance(series, list):
            continue
        points: list[tuple[str, Any]] = []
        for point in series:
            if not isinstance(point, Mapping):
                continue
            period = str(point.get("period") or "").strip()
            value = _mart_dimension_point_value(point, render_data)
            if period and value is not None:
                points.append((period, value))
        if not points:
            continue
        first_period, first_value = points[0]
        last_period, last_value = points[-1]
        rendered_rows.append((name, first_period, first_value, last_period, last_value))
    if not rendered_rows:
        return ""

    first = rendered_rows[0]
    unit_suffix = f" {unit}" if unit else ""
    prose = (
        f"{level_label} 분해에서는 {first[0]}의 {value_label}이 "
        f"{first[1]} {_format_mart_value(first[2])}{unit_suffix}에서 "
        f"{first[3]} {_format_mart_value(first[4])}{unit_suffix}로 조회되었습니다. "
        f"전체 조회 항목은 아래 표에 정리했습니다. [출처: 내부 데이터마트]"
    )
    lines = [
        f"## {level_label}별 {value_label} 추이",
        f"| {level_label} | 시작 기간 | 시작 {value_label} | 최근 기간 | 최근 {value_label} |",
        "| --- | --- | ---: | --- | ---: |",
    ]
    for name, first_period, first_value, last_period, last_value in rendered_rows:
        lines.append(
            f"| {_escape_mart_cell(name)} | {first_period} | "
            f"{_format_mart_value(first_value)}{unit_suffix} | {last_period} | "
            f"{_format_mart_value(last_value)}{unit_suffix} |"
        )
    return prose + "\n\n" + "\n".join(lines)


def _render_mart_dimension_segments(
    rows: Any,
    *,
    render_data: Mapping[str, Any],
    level_label: str,
    value_label: str,
    unit: str,
) -> str:
    if not isinstance(rows, list):
        return ""
    rendered_rows: list[tuple[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "").strip()
        value = _mart_dimension_point_value(row, render_data)
        if name and value is not None:
            rendered_rows.append((name, value))
    if not rendered_rows:
        return ""
    unit_suffix = f" {unit}" if unit else ""
    period = str(render_data.get("period") or "현재").strip()
    prose = (
        f"{period} {level_label} 분해에서 {rendered_rows[0][0]}의 {value_label}은 "
        f"{_format_mart_value(rendered_rows[0][1])}{unit_suffix}로 조회되었습니다. "
        f"전체 조회 항목은 아래 표에 정리했습니다. [출처: 내부 데이터마트]"
    )
    lines = [
        f"## {level_label}별 {value_label}",
        f"| {level_label} | {value_label} |",
        "| --- | ---: |",
        *(
            f"| {_escape_mart_cell(name)} | {_format_mart_value(value)}{unit_suffix} |"
            for name, value in rendered_rows
        ),
    ]
    return prose + "\n\n" + "\n".join(lines)


def _mart_dimension_point_value(
    point: Mapping[str, Any],
    render_data: Mapping[str, Any],
) -> Any | None:
    query_spec = render_data.get("query_spec")
    metrics = query_spec.get("metrics") if isinstance(query_spec, Mapping) else None
    metric_items = metrics if isinstance(metrics, (list, tuple)) else (metrics,)
    candidates = [item for item in metric_items if isinstance(item, str)]
    measure = render_data.get("measure")
    if isinstance(measure, str):
        candidates.append(measure)
    candidates.extend(("value", "value_recent"))
    for key in dict.fromkeys(candidates):
        value = point.get(key)
        if _is_mart_number(value):
            return value
    excluded = {"rank", "year", "month", "yyyymm"}
    for key, value in point.items():
        lowered = str(key).casefold()
        if lowered in excluded or any(
            marker in lowered for marker in _PERCENTAGE_FIELD_MARKERS
        ):
            continue
        if _is_mart_number(value):
            return value
    return None


def _is_mart_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return False
    return value not in (None, "")


def _format_mart_value(value: Any) -> str:
    try:
        return _format_decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return str(value)


def _escape_mart_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _render_mart_history(results: tuple[SourceResult, ...], question: str) -> str:
    trend_requested = any(
        token in question.casefold()
        for token in ("추이", "시계열", "최근 5년", "최근5년", "변해", "변화", "변동")
    )
    market_requested = "시장 규모" in question.casefold() or "시장규모" in question.casefold()
    for result in results:
        if not trend_requested and not any(
            token in result.query.casefold()
            for token in ("추이", "시계열", "최근 5년", "최근5년", "변해", "변화", "변동")
        ):
            continue
        calls = result.payload.get("calls") if isinstance(result.payload, dict) else None
        if not isinstance(calls, list):
            continue
        for call in calls:
            render_data = call.get("render_data") if isinstance(call, dict) else None
            if not isinstance(render_data, dict):
                continue
            brand_series = render_data.get("brand_value_series_10pt")
            market_series = render_data.get("market_size_series")
            if not isinstance(brand_series, list):
                brand_series = []
            if not isinstance(market_series, list):
                market_series = []
            main_series = market_series if market_requested else brand_series
            if len(main_series) < 2:
                continue
            brand_by_period = {
                str(item.get("period")): item.get("value_억원")
                for item in brand_series
                if isinstance(item, dict)
                and item.get("period")
                and item.get("value_억원") is not None
            }
            market_by_period = (
                {
                    str(item.get("period")): item.get("value_억원")
                    for item in market_series
                    if isinstance(item, dict)
                    and item.get("period")
                    and item.get("value_억원") is not None
                }
                if market_series
                else {}
            )
            selected = [
                item
                for index, item in enumerate(main_series)
                if isinstance(item, dict)
                and item.get("period")
                and item.get("value_억원") is not None
                and (
                    index == 0
                    or str(item.get("period")).endswith("-12")
                    or index == len(main_series) - 1
                )
            ]
            if len(selected) < 2:
                continue
            brand = str(render_data.get("brand") or "브랜드")
            metric_label = f"{brand} 전략 시장 규모" if market_requested else f"{brand} 매출"
            subject_particle = "는" if market_requested else "은"
            first = selected[0]
            last = selected[-1]
            first_period = str(first["period"])
            last_period = str(last["period"])
            first_value = first["value_억원"]
            last_value = last["value_억원"]
            duration = _history_year_span(first_period, last_period)
            direction = _history_direction(first_value, last_value)
            prose = (
                f"{metric_label}{subject_particle} {_display_history_period(first_period)} {first_value}억원에서 "
                f"{_display_history_period(last_period)} {last_value}억원으로 "
                f"{duration}년간 {direction}했습니다. [출처: 내부 데이터마트]"
            )
            yearly = "연도별: " + " · ".join(
                f"{_display_history_period(str(item['period']))} {item['value_억원']}억원"
                for item in selected
            )
            lines = [f"| 기간 | {brand} 매출 | 시장 규모 |", "| --- | ---: | ---: |"]
            for item in selected:
                period = str(item["period"])
                brand_value = brand_by_period.get(period)
                market_value = market_by_period.get(period)
                brand_display = f"{brand_value}억원" if brand_value is not None else "확인되지 않음"
                market_display = f"{market_value}억원" if market_value is not None else "확인되지 않음"
                lines.append(f"| {period} | {brand_display} | {market_display} |")
            return prose + "\n\n" + yearly + "\n\n" + "\n".join(lines)
    return ""


def is_typed_absence_confirmation(result: SourceResult) -> bool:
    record = typed_absence_record(result)
    if record is None or result.evidence is None:
        return False
    claims = set(result.evidence.eligible_claims)
    return bool(
        record.status == "confirmed_non_reimbursed"
        and {
            record.doc_type,
            "absence_confirmation",
            f"absence_confirmation:{record.doc_type}",
        }.issubset(claims)
    )


def is_typed_absence_record(result: SourceResult) -> bool:
    return typed_absence_record(result) is not None and result.evidence is not None


def _enforce_claim_eligibility(
    question: str,
    text: str,
    results: tuple[SourceResult, ...],
) -> tuple[str, dict[str, Any]]:
    source_claims: list[tuple[tuple[str, ...], set[str]]] = []
    for result in results:
        if result.evidence is None:
            continue
        record = typed_absence_record(result)
        if result.status != "ok" and record is None:
            continue
        claims = set(result.evidence.eligible_claims)
        if record is not None and record.status != "confirmed_non_reimbursed":
            claims.discard("absence_confirmation")
            claims.discard(f"absence_confirmation:{record.doc_type}")
        if result.source == "hira" and "reimbursement" in claims:
            claims.add("eligibility")
        source_claims.append(
            (
                tuple(alias.casefold() for alias in _SOURCE_TAG_ALIASES[result.source]),
                claims,
            )
        )
    available_claims = set().union(*(claims for _, claims in source_claims))

    unsupported: set[str] = set()
    blocked_blocks = 0
    blocked_sentences = 0
    labeled_blocks = 0
    output: list[str] = []
    for block in re.split(r"(\n\s*\n)", text):
        if not block.strip() or re.fullmatch(r"\n\s*\n", block):
            output.append(block)
            continue
        block_tags = tuple(
            match.group(1).casefold()
            for match in re.finditer(r"\[출처:\s*([^\]]+)\]", block)
        )
        notice_lines = tuple(line.strip() for line in block.splitlines() if line.strip())
        if notice_lines and all(
            line.startswith("- ") and line[2:].strip() in _AUTOMATIC_SAFETY_NOTICES
            for line in notice_lines
        ):
            output.append(block)
            continue

        headings = [line for line in block.splitlines() if line.strip().startswith("## ")]
        kept: list[str] = []
        removed_claims: set[str] = set()
        kept_claims: set[str] = set()
        for sentence in _markdown_sentences(block):
            if sentence.strip().startswith("## "):
                continue
            if (
                sentence.strip() == _ACTIVE_KR_EMPTY_NOTICE
                and _confirmed_active_kr_empty(question, results)
            ):
                kept.append(sentence.strip())
                kept_claims.add("recruitment_status")
                continue
            required = {
                claim
                for claim, pattern in _CLAIM_PATTERNS.items()
                if pattern.search(sentence)
            }
            tags = tuple(
                match.group(1).casefold()
                for match in re.finditer(r"\[출처:\s*([^\]]+)\]", sentence)
            ) or block_tags
            supported: set[str] = set()
            if tags:
                for aliases, claims in source_claims:
                    if any(alias in tag for alias in aliases for tag in tags):
                        supported.update(claims)
            else:
                supported.update(available_claims)
            missing = required - supported
            if missing:
                unsupported.update(missing)
                removed_claims.update(required)
                blocked_sentences += 1
                continue
            kept.append(sentence.strip())
            kept_claims.update(required)

        if not removed_claims:
            output.append(block)
            continue
        blocked_blocks += 1
        requested_claims = {
            claim
            for claim, pattern in _CLAIM_PATTERNS.items()
            if pattern.search(question)
        }
        lost_requested = requested_claims & removed_claims - kept_claims
        if not kept or lost_requested:
            kept.append("해당 주장은 현재 근거 자격으로 확인되지 않았습니다.")
            labeled_blocks += 1
        output.append("\n".join((*headings, *kept)))

    return "".join(output).strip(), {
        "blocked": blocked_blocks > 0,
        "blocked_blocks": blocked_blocks,
        "blocked_sentences": blocked_sentences,
        "labeled_blocks": labeled_blocks,
        "unsupported_claims": sorted(unsupported),
    }


def _confirmed_active_kr_empty(
    question: str,
    results: tuple[SourceResult, ...],
) -> bool:
    normalized = " ".join(question.split()).casefold()
    if not (
        "임상" in normalized
        and any(marker in normalized for marker in ("진행 중", "진행중", "모집 중", "모집중"))
        and any(marker in normalized for marker in ("국내", "한국", "대한민국"))
    ):
        return False
    saw_empty = False
    saw_explicit_nonmatch = False
    for result in results:
        if result.source != "clinicaltrials":
            continue
        if result.status == "empty":
            saw_empty = True
            continue
        if result.status != "ok":
            continue
        for record in _nested_payload_mappings(result.payload):
            status_values = [
                str(value).upper().replace(" ", "_")
                for key, value in record.items()
                if "status" in str(key).casefold() and value not in (None, "")
            ]
            country_values = [
                str(value).casefold()
                for key, value in record.items()
                if "country" in str(key).casefold() and value not in (None, "")
            ]
            if not status_values or not country_values:
                continue
            active = any(value in _ACTIVE_TRIAL_STATUSES for value in status_values)
            kr = any(
                marker in value
                for value in country_values
                for marker in ("korea", "대한민국", "한국")
            )
            if active and kr:
                return False
            saw_explicit_nonmatch = True
    return saw_empty or saw_explicit_nonmatch


def _nested_payload_mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, Mapping):
        return (
            value,
            *(
                nested
                for item in value.values()
                for nested in _nested_payload_mappings(item)
            ),
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            nested
            for item in value
            for nested in _nested_payload_mappings(item)
        )
    return ()


def inspect_requested_hira_surface(
    question: str,
    text: str,
    results: tuple[SourceResult, ...] | list[SourceResult],
) -> dict[str, list[_HiraSurfaceFact]]:
    expected = _requested_hira_facts(question, tuple(results))
    missing = [fact for fact in expected if not _hira_fact_is_rendered(text, fact)]
    return {"expected": expected, "missing": missing}


def _requested_hira_facts(
    question: str,
    results: tuple[SourceResult, ...],
) -> list[_HiraSurfaceFact]:
    requested_fields = {
        field
        for pattern, fields in _HIRA_REQUEST_PATTERNS
        if pattern.search(question)
        for field in fields
    }
    if not requested_fields:
        return []
    requested_years = _requested_hira_years(question)
    requested_care_types = {
        care_type for care_type in ("입원", "외래") if care_type in question
    }

    facts: list[_HiraSurfaceFact] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for result in results:
        if result.source != "hira" or result.status != "ok" or not isinstance(result.payload, dict):
            continue
        calls = result.payload.get("calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            render = call.get("render_data")
            if not isinstance(render, dict):
                continue
            request = render.get("request") if isinstance(render.get("request"), dict) else {}
            subject = str(request.get("sickCd") or "").strip().upper()
            if not subject:
                match = re.search(r"\b[A-Z]\d{2,3}(?:\.\d+)?\b", result.query.upper())
                subject = match.group(0) if match else result.query.strip()
            request_year = str(request.get("year") or "").strip()
            items = render.get("items")
            if not isinstance(items, list):
                continue
            for row in items:
                if not isinstance(row, dict):
                    continue
                year = request_year or str(row.get("year") or "").strip()
                care_type = str(row.get("inpatOpat") or "환자").strip()
                if not year:
                    continue
                if requested_years and year not in requested_years:
                    continue
                if requested_care_types and care_type not in requested_care_types:
                    continue
                for field in requested_fields:
                    if field not in row or row[field] in (None, ""):
                        continue
                    value = _normalized_hira_value(field, row[field], row.get("units"))
                    if value is None:
                        continue
                    key = (subject, year, care_type, field, value)
                    if key in seen:
                        continue
                    seen.add(key)
                    label, unit = _HIRA_PUBLIC_FIELDS[field]
                    display_number = _format_decimal(value)
                    facts.append(
                        _HiraSurfaceFact(
                            subject=subject,
                            year=year,
                            care_type=care_type,
                            field=field,
                            label=label,
                            value=value,
                            display=f"{display_number}{unit}",
                        )
                    )
    return sorted(
        facts,
        key=lambda fact: (fact.subject, fact.year, fact.care_type, fact.field),
    )


def _requested_hira_years(question: str) -> set[str]:
    years = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", question))
    for start, end in re.findall(
        r"(?<!\d)(20\d{2})\s*(?:~|[-–]|부터)\s*(20\d{2})(?!\d)",
        question,
    ):
        first, last = sorted((int(start), int(end)))
        years.update(str(year) for year in range(first, last + 1))
    return years


def _normalized_hira_value(field: str, value: Any, units: Any) -> str | None:
    try:
        number = Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None
    source_unit = str(units.get(field) or "") if isinstance(units, dict) else ""
    if field in _HIRA_COST_FIELDS and source_unit != "원":
        number *= Decimal(1000)
    return format(number.normalize(), "f")


def _format_decimal(value: str) -> str:
    number = Decimal(value)
    if number == number.to_integral_value():
        return f"{int(number):,}"
    return f"{number:,}".rstrip("0").rstrip(".")


def _hira_fact_is_rendered(text: str, fact: _HiraSurfaceFact) -> bool:
    compact = text.replace(",", "")
    label = re.escape(fact.label)
    unit = re.escape(_HIRA_PUBLIC_FIELDS[fact.field][1])
    value = re.escape(fact.value)
    year = re.escape(fact.year)
    care_type = re.escape(fact.care_type)
    subject = re.escape(fact.subject)
    patterns = (
        rf"{subject}.{{0,160}}{year}년?.{{0,500}}{care_type}.{{0,160}}{label}.{{0,40}}{value}\s*{unit}",
        rf"{year}년?.{{0,160}}{subject}.{{0,500}}{care_type}.{{0,160}}{label}.{{0,40}}{value}\s*{unit}",
        rf"{subject}.{{0,500}}{care_type}.{{0,160}}{label}.{{0,40}}{value}\s*{unit}",
    )
    return any(re.search(pattern, compact, re.DOTALL) for pattern in patterns)


def _render_hira_surface_facts(facts: list[_HiraSurfaceFact]) -> str:
    grouped: dict[tuple[str, str, str], list[_HiraSurfaceFact]] = {}
    for fact in facts:
        grouped.setdefault((fact.subject, fact.year, fact.care_type), []).append(fact)
    lines = [
        f"{year}년 {subject} {care_type} "
        + ", ".join(f"{fact.label} {fact.display}" for fact in group)
        + "으로 확인되었습니다. [출처: HIRA]"
        for (subject, year, care_type), group in sorted(grouped.items())
    ]
    return "\n".join(lines)


def _public_hira_fact(fact: _HiraSurfaceFact) -> dict[str, str]:
    return {
        "subject": fact.subject,
        "year": fact.year,
        "care_type": fact.care_type,
        "metric": fact.label,
        "value": fact.display,
    }


def _invalid_hira_patient_sentences(
    text: str,
    facts: list[_HiraSurfaceFact],
) -> tuple[set[str], set[str]]:
    expected = [fact for fact in facts if fact.field == "ptntCnt"]
    if not expected:
        return set(), set()

    invalid_sentences: set[str] = set()
    invalid_values: set[str] = set()
    for sentence in _markdown_sentences(text):
        for match in _HIRA_PATIENT_SURFACE_RE.finditer(sentence):
            prefix = sentence[max(0, match.start() - 160) : match.start()]
            years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", prefix)
            care_types = re.findall(r"입원|외래", prefix)
            candidates = expected
            if years:
                candidates = [fact for fact in candidates if fact.year == years[-1]]
            if care_types:
                candidates = [
                    fact for fact in candidates if fact.care_type == care_types[-1]
                ]
            rendered_value = _normalize_number(match.group("value"))
            if rendered_value not in {fact.value for fact in candidates}:
                invalid_sentences.add(sentence.strip())
                invalid_values.add(rendered_value)
    return invalid_sentences, invalid_values


def _remove_markdown_sentences(text: str, denied: set[str]) -> str:
    return "\n".join(
        sentence.strip()
        for sentence in _markdown_sentences(text)
        if sentence.strip() not in denied
    )


def _markdown_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## ") or stripped.startswith("|"):
            sentences.append(stripped)
            continue
        matches = [match.group(0).strip() for match in _MARKDOWN_SENTENCE_RE.finditer(stripped)]
        sentences.extend(matches or [stripped])
    return sentences


def _merge_unique_blocks(*texts: str) -> str:
    blocks: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for block in re.split(r"\n\s*\n", text):
            cleaned = block.strip()
            if not cleaned:
                continue
            key = re.sub(r"\s+", " ", cleaned)
            if key in seen:
                continue
            seen.add(key)
            blocks.append(cleaned)
    return "\n\n".join(blocks)


def _replace_markdown_section(text: str, heading: str, body: str) -> str:
    pattern = re.compile(
        rf"(?ms)^##\s+{re.escape(heading)}\s*$.*?(?=^##\s+|\Z)"
    )
    replacement = f"## {heading}\n{body.strip()}\n\n"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1).strip()
    return _merge_unique_blocks(text, replacement.strip())


def _replace_hira_scope_notice(text: str, notice: str) -> str:
    lines = tuple(
        line
        for line in text.splitlines()
        if not (
            ("5세" in line or "연령 5세" in line)
            and any(token in line for token in ("확인되지", "제공되지", "지원되지"))
        )
    )
    return _merge_unique_blocks("\n".join(lines).strip(), f"## 미확인 요소\n{notice}")


def _display_history_period(period: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})", period)
    if match is None:
        return period
    return f"{match.group(1)}년 {int(match.group(2))}월"


def _history_year_span(first_period: str, last_period: str) -> int:
    first_year = int(first_period[:4]) if first_period[:4].isdigit() else 0
    last_year = int(last_period[:4]) if last_period[:4].isdigit() else first_year
    return max(0, last_year - first_year)


def _history_direction(first_value: Any, last_value: Any) -> str:
    try:
        first = float(str(first_value).replace(",", ""))
        last = float(str(last_value).replace(",", ""))
    except ValueError:
        return "변화"
    if last > first:
        return "증가"
    if last < first:
        return "감소"
    return "유지"


def _walk_scalars(value: Any, prefix: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_scalars(item, path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_scalars(item, f"{prefix}[{index}]")
    else:
        yield prefix, value


def _append_sentence(text: str, sentence: str) -> str:
    return f"{text}\n\n{sentence}".strip()


def _append_sources(text: str, results: tuple[SourceResult, ...]) -> str:
    if "## 출처" in text:
        text = text.split("## 출처", 1)[0].rstrip()
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for result in results:
        if result.status != "ok" and not is_typed_absence_record(result):
            continue
        source = _public_source_name(result)
        key = (source, result.query)
        if key in seen:
            continue
        seen.add(key)
        if result.source == "patent" and _append_patent_lane_sources(lines, result):
            continue
        detail = _source_reference_detail(result)
        reuse = " · 이전 조회 재사용" if result.cache_hit else ""
        line = f'- {source} — "{result.query}" {_source_reference_type(result)}{detail}{reuse}'
        lines.append(line)
        references = _public_source_references(result)
        lines.extend(
            "  - "
            + (f"{published_at} " if published_at else "")
            + f"[{_public_reference_label(url, title)}]({url})"
            for url, published_at, title in references
        )
    if not lines:
        lines.append("- 사용 가능한 출처를 확보하지 못했습니다.")
    return f"{text}\n\n## 출처\n" + "\n".join(lines)


def _append_patent_lane_sources(lines: list[str], result: SourceResult) -> bool:
    lanes = (
        result.payload.get("patent_lanes")
        if isinstance(result.payload, Mapping)
        else None
    )
    if not isinstance(lanes, Mapping):
        return False
    specs = tuple((lane, patent_lane_label(lane)) for lane in PATENT_LANES)
    added = False
    reuse = " · 이전 조회 재사용" if result.cache_hit else ""
    for lane_name, label in specs:
        raw_lane = lanes.get(lane_name)
        lane = raw_lane if isinstance(raw_lane, Mapping) else {}
        records = lane.get("records")
        if not isinstance(records, list) or not records:
            continue
        lines.append(f'- {label} — 조회 "{result.query}"{reuse}')
        lines.extend(
            "  - "
            + (f"{published_at} " if published_at else "")
            + f"[{_public_reference_label(url, title)}]({url})"
            for url, published_at, title in _public_references_from_value(lane)
        )
        added = True
    return added


def _source_reference_detail(result: SourceResult) -> str:
    if result.source != "hira" or result.evidence is None:
        return ""
    if "reimbursement" not in result.evidence.eligible_claims:
        return ""
    notice_numbers = tuple(
        dict.fromkeys(
            str(value).strip()
            for path, value in _walk_scalars(result.payload)
            if path.casefold().endswith(".notice_number")
            and re.fullmatch(r"고시\s+제\d{4}-\d+호", str(value).strip())
        )
    )
    return "" if not notice_numbers else " · " + " · ".join(notice_numbers)


def _public_source_name(result: SourceResult) -> str:
    return public_source_label(result.source)


def _source_reference_type(result: SourceResult) -> str:
    record = typed_absence_record(result)
    if record is not None:
        return (
            "고시 무결과 확인"
            if record.doc_type == "reimbursement"
            else "허가 문서 무결과 확인"
        )
    if result.source == "hira" and result.evidence is not None:
        if "reimbursement" in result.evidence.eligible_claims:
            return "고시 검색"
    return {
        "mart": "내부 지표 조회",
        "nedrug": "허가 검색",
        "hira": "통계 조회",
        "openfda": "안전성 검색",
        "clinicaltrials": "임상 검색",
        "web": "웹 검색",
        "patent": "특허 검색",
    }[result.source]


def _public_source_references(
    result: SourceResult,
) -> tuple[tuple[str, str | None, str | None], ...]:
    references: list[tuple[str, str | None, str | None]] = []
    for citation in result.citations:
        if is_public_source_url(citation.url):
            references.append((str(citation.url), None, None))

    references.extend(_public_references_from_value(result.payload))
    by_url: dict[str, tuple[str, str | None, str | None]] = {}
    for url, published_at, title in references:
        existing = by_url.get(url)
        if existing is None:
            by_url[url] = (url, published_at, title)
            continue
        by_url[url] = (
            url,
            existing[1] or published_at,
            existing[2] or title,
        )
    return tuple(by_url.values())


def _public_references_from_value(
    value: Any,
) -> tuple[tuple[str, str | None, str | None], ...]:
    references: list[tuple[str, str | None, str | None]] = []

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            url = current.get("url") or current.get("source_url")
            if is_public_source_url(url):
                published_at = current.get("published_at") or current.get("published_date")
                title = current.get("title") or current.get("name")
                references.append(
                    (
                        str(url),
                        str(published_at).strip() if published_at else None,
                        str(title).strip() if title else None,
                    )
                )
            for item in current.values():
                visit(item)
        elif isinstance(current, (list, tuple)):
            for item in current:
                visit(item)

    visit(value)
    by_url: dict[str, tuple[str, str | None, str | None]] = {}
    for url, published_at, title in references:
        existing = by_url.get(url)
        if existing is None:
            by_url[url] = (url, published_at, title)
            continue
        by_url[url] = (
            url,
            existing[1] or published_at,
            existing[2] or title,
        )
    return tuple(by_url.values())


def _public_reference_label(url: str, title: str | None) -> str:
    decoded = unquote(url)
    parsed = urlparse(decoded)
    host = (parsed.hostname or "출처").removeprefix("www.")
    visible_title = _one_line_source_title(title)
    if not visible_title:
        path_title = unquote(parsed.path).rstrip("/").rsplit("/", 1)[-1]
        visible_title = _one_line_source_title(path_title) or "원문"
    return f"{host} · {visible_title}"


def _one_line_source_title(value: str | None, limit: int = 72) -> str:
    text = unquote(" ".join(str(value or "").split()))
    text = text.replace("[", "(").replace("]", ")")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_public_source_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(unquote(value.strip()))
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    path_segments = tuple(
        segment.casefold() for segment in parsed.path.split("/") if segment
    )
    if (
        "." not in host
        or host == "localhost"
        or host.endswith(".svc")
        or ".svc." in host
        or host.endswith(".local")
        or host.startswith(("mcp-", "code-serving-", "read-only"))
        or any(token in host for token in ("mcp-", "code-serving-", "read-only"))
        or host.split(".", 1)[0] == "api"
        or any(segment in {"api", "mcp", "serving"} for segment in path_segments)
    ):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return address.is_global


def contains_internal_source_reference(value: str) -> bool:
    if _INTERNAL_ENDPOINT_SURFACE_RE.search(value):
        return True
    for match in _IPV4_SURFACE_RE.finditer(value):
        try:
            address = ipaddress.ip_address(match.group(0))
        except ValueError:
            continue
        if not address.is_global:
            return True
    return False
