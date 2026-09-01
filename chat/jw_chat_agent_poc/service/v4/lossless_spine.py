from __future__ import annotations

import os
import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from threading import Lock
from typing import Any, Literal

from jw_chat_agent_poc.hira_surface import requested_hira_axes
from jw_chat_agent_poc.service.context_scope import (
    has_explicit_file_source_comparison,
    has_file_axis_reference,
)
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.deterministic_render import render_deterministic_facts
from jw_chat_agent_poc.service.v4.evidence_sets import build_evidence_sets
from jw_chat_agent_poc.service.v4.fact_digest import FactDigest
from jw_chat_agent_poc.service.v4.gates import is_public_source_url
from jw_chat_agent_poc.service.v4.insight_contract import sanitize_s17_insight
from jw_chat_agent_poc.service.v4.lane_execution import (
    LaneExecutionRecord,
    build_lane_execution_records,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CompositionResult,
    DeterministicRender,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.semantic_realization import (
    ensure_core_answer_surface,
    strip_s17_body_metadata,
)
from jw_chat_agent_poc.service.v4.source_labels import normalize_public_source_surface
from jw_chat_agent_poc.service.v4.surface_notices import append_automatic_fact_notices
from jw_chat_agent_poc.service.v4.synthesis_policy import limit_evidence_sets_for_render

LosslessMode = Literal["shadow", "inject"]
RequestedFieldsMode = Literal["shadow", "inject"]
RequestSatisfactionMode = Literal["shadow", "inject"]

_SECTION_RE = re.compile(r"(?m)^#{1,6}\s+([^\n]+?)\s*$")
_CORE_HEADINGS = {"핵심 답", "핵심 요약"}
_CONTEXT_HEADINGS = {"근거와 맥락", "근거"}
_INSIGHT_HEADINGS = {"종합 인사이트", "인사이트"}
_INSIGHT_BLOCK_RE = re.compile(
    r"^##[ \t]*(?:\n[ \t]*)?(?P<heading>종합 인사이트|인사이트)[ \t]*\n"
)
_INSIGHT_SECTION_RE = re.compile(
    r"(?ms)^#{1,6}\s+(?:종합 인사이트|인사이트)\s*\n.*?(?=^#{1,6}\s+|\Z)"
)
_LIMIT_HEADINGS = {"해석 상한", "해석상 주의점", "미확인 요소", "한계"}
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_UNPROVIDED_CELL = "원천 미제공"
_INSIGHT_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?")
_INSIGHT_CITATION_RE = re.compile(r"\[출처:\s*[^\]]+\]")
_INSIGHT_LABEL_ONLY_RE = re.compile(r"^\s*(?:[-*]\s*)?\*\*[^*\n]+\*\*\s*[:：]?\s*$")
_INSIGHT_IDENTIFIER_RE = re.compile(
    r"(?:\b[A-Za-z]{1,8}[-_]?[A-Za-z0-9]{1,16}\b|"
    r"20\d{2}(?:[-./년]\d{1,2})?|\d{1,3}\s*~\s*\d{1,3}세|"
    r"(?:남|여|남성|여성)(?:\s|$))"
)
_INSIGHT_DERIVED_MARKERS = ("배", "비율", "합계", "총합", "증가율", "감소율")
_INSIGHT_ABSENCE_RE = re.compile(
    r"(?:확인되지\s*않|확인하지\s*못|확인할\s*수\s*없|찾지\s*못|연결하지\s*못|"
    r"포함되어\s*있지\s*않|존재하지\s*않|없는\s*것으로\s*확인|"
    r"제공되지\s*않|전달되지\s*않|미제공|조회되지\s*않|요약(?:이|은)?\s*불가|근거(?:가)?\s*없|"
    r"(?:데이터|자료|값|결과|레코드|record|정보|통계|총액|수치|특허|매출|환자\s*수)"
    r"(?:은|는|이|가|도|를|을)?\s*(?:없습니다|없음)|^\s*(?:없습니다|없음))",
    re.IGNORECASE,
)
_ABSENCE_RELEVANCE_GROUPS = (
    ("sellout", "sell out"),
    ("총액", "total_value", "합계"),
    ("전망치", "전망"),
    ("유병률",),
    ("환자수", "환자 수"),
    ("매출",),
    ("점유율",),
    ("특허",),
    ("임상",),
    ("시트",),
    ("셀",),
)
_YEAR_MONTH_RE = re.compile(
    r"(?P<year>20\d{2})(?:\s*년\s*|[-./])(?P<month>\d{1,2})(?:\s*월)?"
)
_YEAR_RE = re.compile(r"20\d{2}")
_MONTH_RE = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*월")
_PATENT_RETAIN_WHEN_UNPROVIDED_HEADERS = frozenset(
    {
        "특허구분",
        "구분",
        "재심사 시작일",
        "재심사 종료일",
        "존속기간 만료일",
        "특허권자",
    }
)
_COVERAGE_RE = re.compile(
    r"원천 검색 (?P<total>[^·]+?)건\s*·\s*수신 (?P<received>[\d,]+)건\s*·\s*"
    r"중복 제거 후 (?P<unique>[\d,]+)건\s*·\s*상세 표시 (?P<shown>[\d,]+)건"
)
_AXIS_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...] = (
    ("sales", ("매출", "실적", "순위"), ("market:",), "mart"),
    ("market_share", ("점유율", "시장점유"), ("market:",), "mart"),
    (
        "patient_statistics",
        ("환자수", "환자 수", "통계", "요양급여비용총액", "보험자부담금"),
        ("hira-statistics:",),
        "hira",
    ),
    (
        "clinical",
        ("임상", "nct", "clinical", "trial"),
        ("clinical:",),
        "clinicaltrials",
    ),
    ("patent", ("특허",), ("patent:",), "patent"),
    ("reimbursement", ("급여기준", "급여 기준", "고시"), ("policy:",), "hira"),
    ("approval", ("허가", "품목"), ("nedrug:", "openfda:"), "nedrug"),
    (
        "document",
        ("문서", "파일", "첨부", "업로드", "pdf"),
        ("document:",),
        "document",
    ),
)

_S14A_PROSE_BLACKLIST = (
    "본문 표에 표시된 항목과 범위를 기준으로 읽어야 합니다",
    "대표 항목만으로 전체를 일반화하기보다",
    "표에 남은 구분과 기간을 함께 확인하면",
    "질문 축의 구성과 차이를 더 분명하게 파악할 수 있습니다",
    "같은 질문을 서로 다른 자료 범위에서 보완하므로",
    "한쪽 결과만으로 결론을 닫지 않는 것이 적절합니다",
    "요청 축과 인접 축을 함께 보면",
    "실제 검토 우선순위를 정하는 데 어떤 맥락을 주는지",
    "이 구조는 세부 표의 차이를",
    "시장 검토, 경쟁 구도 확인 또는 후속 자료 탐색의 출발점",
    "본문에 표시되지 않은 원인이나 효과를 단정하기보다",
    "관찰된 구성과 범위가 추가 확인의 차례를 보여주는",
    "이번 응답에서 근거가 확인된 자료원은",
    "다른 자료원과의 교차 비교는 근거가 추가될 때 별도로",
    "현재는 한 자료원의 항목들을 같은 정의와 범위 안에서 비교",
    "본문에 표시된 사실은 항목별 식별자와 기간, 자료원 범위 안에서 해석",
    "살펴보면 다음과 같습니다",
    "이어서 다른 주요 항목을 확인해 볼 수 있습니다",
    "결론을 닫지 않는",
    "해석하는 편이 타당",
    "판단하지 않습니다",
    "판단하지 않았습니다",
    "단정하기보다",
    "단정하지 않습니다",
    "읽어야 합니다",
    "일반화하기보다",
    "해석해야 합니다",
    "유의해야 합니다",
    "주의가 필요합니다",
    "[확인 한계]",
    "나란히 확인됩니다",
    "비교의 기준이 됩니다",
    "고정하면 구체화할 수 있습니다",
    "가늠해 볼 수 있습니다",
    "추론할 수 있습니다",
    "의미합니다",
    "확인됩니다",
)
_SOURCE_LABELS = {
    "mart": "내부 데이터마트",
    "hira": "건강보험심사평가원",
    "clinicaltrials": "ClinicalTrials.gov",
    "patent": "식품의약품안전처 의약품 특허목록",
    "nedrug": "식품의약품안전처 의약품 정보",
    "openfda": "미국 의약품 공개 정보",
    "web": "공개 웹 자료",
    "document": "업로드 문서",
    "document_rag": "업로드 문서(문서 검색)",
    "document_sql": "업로드 문서(표 집계)",
    "prior_turn": "이전 답변",
}
_SOURCE_BINDING_STOPWORDS = frozenset(
    {
        "관련",
        "결과",
        "구분",
        "근거",
        "내용",
        "자료",
        "자료원",
        "정보",
        "조회",
        "종합",
        "항목",
        "확인",
        "현황",
    }
)
_S14A_RECENT_SENTENCE_SKELETONS: deque[tuple[str, str]] = deque(maxlen=50)
_S14A_RECENT_SENTENCE_LOCK = Lock()
_SOURCE_BINDING_CUES: Mapping[str, tuple[str, ...]] = {
    "mart": ("데이터마트", "매출", "점유율", "sellout", "sell out"),
    "hira": (
        "건강보험심사평가원",
        "심평원",
        "hira",
        "환자수",
        "환자 수",
        "상병",
        "청구",
    ),
    "clinicaltrials": ("clinicaltrials.gov", "clinicaltrials", "임상시험", "nct"),
    "patent": ("의약품 특허목록", "특허", "존속기간", "특허권자"),
    "nedrug": ("의약품 정보", "허가", "품목"),
    "openfda": ("openfda", "미국 의약품 공개"),
    "web": ("공개 웹", "웹 자료"),
    "document": ("업로드 문서", "파일"),
    "document_rag": ("문서 검색", "페이지"),
    "document_sql": ("표 집계", "시트", "셀", "sell out  standard"),
    "prior_turn": ("이전 답변", "이전 대화", "턴 전"),
}
_SOURCE_BINDING_FREE_TEXT_LABELS = (
    "요약",
    "summary",
    "스니펫",
    "snippet",
    "선정·제외 기준",
    "평가변수",
)
_AXIS_LABELS = {
    "sales": "매출",
    "market_share": "점유율",
    "patient_statistics": "환자수",
    "clinical": "임상 현황",
    "patent": "특허 현황",
    "reimbursement": "급여기준",
    "approval": "허가 정보",
    "document": "문서 내용",
}
_DEFAULT_PRIMARY_TABLE_ROW_LIMIT = 15
_HOMOGENEOUS_NARRATIVE_TABLE_THRESHOLD = 3
_PATIENT_UNRELATED_TERMS = (
    "임상",
    "특허",
    "허가",
    "매출",
    "점유율",
    "시장 동향",
)
_AXIS_FACT_TERMS = {
    "sales": ("매출", "실적", "브랜드", "시장", "억원"),
    "market_share": ("점유율", "시장점유", "브랜드", "시장", "순위"),
    "patient_statistics": ("환자수", "환자 수", "상병코드", "입원", "외래"),
    "clinical": ("임상", "시험", "nct", "trial", "study", "clinicaltrials.gov"),
    "patent": ("특허", "권리", "만료", "소멸", "orange book"),
    "reimbursement": ("급여", "고시", "투여", "인정", "제외기준"),
    "approval": ("허가", "품목", "용법", "용량", "적응증"),
    "document": ("업로드", "파일", "문서", "sellout", "총액", "시트", "셀", "행 수"),
}
_AXIS_SOURCE_MARKERS = {
    "sales": ("[출처: 내부 데이터마트]", "[출처: 시장 데이터베이스]", "mart"),
    "market_share": (
        "[출처: 내부 데이터마트]",
        "[출처: 시장 데이터베이스]",
        "mart",
    ),
    "patient_statistics": ("[출처: 건강보험심사평가원]",),
    "clinical": ("[출처: clinicaltrials.gov]",),
    "patent": ("[출처: 식품의약품안전처 의약품 특허목록]", "[출처: orange book]"),
    "reimbursement": ("[출처: 건강보험심사평가원 급여기준]", "[출처: hira]"),
    "approval": ("[출처: 식품의약품안전처 의약품 정보]", "[출처: openfda]"),
    "document": (
        "[출처: 업로드 문서]",
        "[출처: 업로드 문서(문서 검색)]",
        "[출처: 업로드 문서(표 집계)]",
    ),
}


def configured_lossless_mode() -> LosslessMode:
    value = os.environ.get("CHAT_V4_LOSSLESS_SPINE_MODE", "shadow").strip().casefold()
    return "inject" if value == "inject" else "shadow"


def configured_primary_table_row_limit(block_id: str = "") -> int:
    raw_value = os.environ.get("TABLE_ROW_LIMIT", str(_DEFAULT_PRIMARY_TABLE_ROW_LIMIT)).strip()
    try:
        value = int(raw_value)
    except ValueError:
        value = _DEFAULT_PRIMARY_TABLE_ROW_LIMIT
    configured = value if value > 0 else _DEFAULT_PRIMARY_TABLE_ROW_LIMIT
    return max(configured, 160) if block_id == "hira-statistics:records" else configured


def configured_request_satisfaction_mode() -> RequestSatisfactionMode:
    value = os.environ.get(
        "CHAT_V4_REQUEST_SATISFACTION_MODE",
        "shadow",
    ).strip().casefold()
    return "inject" if value == "inject" else "shadow"


def configured_requested_fields_mode() -> RequestedFieldsMode:
    value = os.environ.get(
        "CHAT_V4_REQUESTED_FIELDS_MODE",
        "shadow",
    ).strip().casefold()
    return "inject" if value == "inject" else "shadow"


def deterministic_fact_text(
    rendered: DeterministicRender,
    requested_fields_mode: RequestedFieldsMode,
) -> str:
    if not rendered.nodes:
        return rendered.text
    return "\n\n".join(
        node.text
        for node in rendered.nodes
        if node.text.strip()
        and (
            requested_fields_mode == "inject"
            or node.block_id != "requested-fields:absence"
        )
    )


def build_lossless_render(
    plan: PlannerOutput,
    results: Sequence[SourceResult],
    *,
    observed_on: date,
    source_render_limit: int | None = None,
    lane_execution: Mapping[str, LaneExecutionRecord] | None = None,
) -> tuple[tuple[EvidenceSet, ...], DeterministicRender]:
    evidence_sets = build_evidence_sets(plan, results, observed_on=observed_on)
    if lane_execution is None:
        lane_execution = build_lane_execution_records(plan, results, evidence_sets)
    render_sets = evidence_sets
    limit_trace: dict[str, Any] = {"applied": False, "sources": {}}
    if source_render_limit is not None:
        render_sets, limit_trace = limit_evidence_sets_for_render(
            evidence_sets,
            per_source_limit=source_render_limit,
            question=plan.resolved_question,
            observed_on=observed_on,
        )
    rendered = render_deterministic_facts(
        plan,
        render_sets,
        observed_on=observed_on,
        lane_execution=lane_execution,
    )
    selection_by_source = {
        source: dict(selection)
        for source, selection in rendered.selection_by_source.items()
    }
    for source, selection in limit_trace.get("sources", {}).items():
        selection_by_source.setdefault(source, {}).update(selection)
    rendered = rendered.model_copy(
        update={
            "selection_rule": limit_trace.get(
                "selection_rule",
                "leading_records_in_upstream_order",
            ),
            "selection_is_ranked": limit_trace.get("selection_is_ranked", False),
            "selection_by_source": selection_by_source,
        }
    )
    if limit_trace["applied"]:
        basis_labels = {
            "question_axis_brand_period_metric_then_evidence_id": (
                "질문 축(브랜드·기간·지표) 우선, 안정 식별자 순"
            ),
            "question_axis_metric_desc_then_evidence_id": (
                "질문 지표 내림차순, 안정 식별자 순"
            ),
            "question_axis_brand_then_evidence_id": "질문 브랜드 우선, 안정 식별자 순",
            "stable_evidence_id_order": "안정 식별자 순",
        }
        notices = [
            f"{source}: {counts['shown']}/{counts['total']} 표시("
            f"{basis_labels.get(str(counts.get('selection_rule')), '결정론 선택')}), "
            "나머지는 조회 상세에 보존"
            for source, counts in limit_trace["sources"].items()
        ]
        existing = str(rendered.request_notice or "").strip()
        rendered = rendered.model_copy(
            update={
                "request_notice": " · ".join(filter(None, (existing, *notices)))
            }
        )
    return evidence_sets, rendered


def compose_lossless_answer(
    rendered: DeterministicRender,
    commentary: str,
    *,
    synthesis_trace: Mapping[str, Any],
    mode: LosslessMode,
    requested_fields_mode: RequestedFieldsMode = "shadow",
    request_satisfaction_mode: RequestSatisfactionMode = "inject",
    question: str = "",
    streamed_prefix: str = "",
    _deterministic_body_only: bool = False,
) -> CompositionResult:
    commentary = _normalize_known_bold_headings(commentary)
    commentary = _drop_empty_bold_headings(commentary)
    if mode == "inject":
        commentary, model_source_lines_ignored = _strip_model_source_sections(commentary)
    else:
        model_source_lines_ignored = 0
    fallback = bool(synthesis_trace.get("fallback_reason")) or synthesis_trace.get("status") in {
        "fallback",
        "no_usable_evidence",
    }
    s17_richness = synthesis_trace.get("insight_richness_retry")
    s17_after = (
        s17_richness.get("after")
        if isinstance(s17_richness, Mapping)
        else None
    )
    # sanitize_s17_insight already removes ungrounded numbers and other hard
    # failures. contract_met is a richness/shape score, so a safe soft-degraded
    # section must still survive final composition.
    s17_validated_insight = bool(
        isinstance(s17_after, Mapping) and not s17_after.get("omitted")
    )
    retention = rendered.record_surface_rate if fallback else 1.0
    facts = deterministic_fact_text(rendered, requested_fields_mode)
    requested_fields_observed = any(
        node.block_id == "requested-fields:absence" for node in rendered.nodes
    )
    trace = {
        "mode": mode,
        "requested_fields_mode": requested_fields_mode,
        "requested_fields_observed": requested_fields_observed,
        "requested_fields_injected": False,
        "request_satisfaction_mode": request_satisfaction_mode,
        "request_notice_observed": bool(rendered.request_notice),
        "request_notice_injected": False,
        "source_notices_observed": list(rendered.source_notices),
        "source_notice_bindings": list(rendered.source_notice_bindings),
        "source_tiers": dict(rendered.source_tiers),
        "source_notices_injected": False,
        "profile": rendered.profile,
        "answer_mutation": False,
        "model_source_lines_ignored": model_source_lines_ignored,
        "record_surface_rate": rendered.record_surface_rate,
        "required_field_surface_rate": rendered.required_field_surface_rate,
        "fallback_detail_retention_rate": retention,
        "records_received": rendered.coverage.records_received,
        "records_unique": rendered.coverage.records_unique,
        "records_rendered": rendered.coverage.records_rendered,
        "rendered_table_rows": rendered.coverage.records_rendered,
        "lossless_records_rendered": rendered.coverage.records_rendered,
        "narrated_record_count": len(rendered.narrated_record_ids),
        "narrated_record_ids": list(rendered.narrated_record_ids),
        "unnarrated_record_count": rendered.unnarrated_record_count,
        "unnarrated_records": list(rendered.unnarrated_records),
        "narrative_identifier_parity": (
            len(rendered.narrated_record_ids) == rendered.coverage.records_rendered
        ),
        "narrative_record_accounting_complete": (
            len(rendered.narrated_record_ids) + rendered.unnarrated_record_count
            == rendered.coverage.records_rendered
        ),
        "record_field_usage": list(rendered.record_field_usage),
        "average_narrated_field_count": rendered.average_narrated_field_count,
        "loaded_field_narrative_use_rate": rendered.loaded_field_narrative_use_rate,
        "identifier_only_sentence_count": rendered.identifier_only_sentence_count,
        "selection_rule": rendered.selection_rule,
        "selection_is_ranked": rendered.selection_is_ranked,
        "selection_by_source": dict(rendered.selection_by_source),
        "answer_axis": "unknown",
        "primary_source": None,
        "axis_fallback_preserved_order": True,
        "primary_axis_absence": None,
        "secondary_records_compacted": 0,
        "primary_table_row_limit": configured_primary_table_row_limit(),
        "primary_table_rows_hidden": 0,
        "primary_body_fact_count": 0,
        "comparison_observation_sections_removed": 0,
        "mechanical_narratives_compacted": 0,
        "homogeneous_table_promotion_threshold": _HOMOGENEOUS_NARRATIVE_TABLE_THRESHOLD,
        "homogeneous_patient_narratives_promoted": 0,
        "facts_injected_after_synthesis": False,
        "s17_validated_insight_preserved": s17_validated_insight,
        "synthesis_prompt_chars": synthesis_trace.get("prompt_chars"),
        "render_nodes": [
            {
                "block_id": node.block_id,
                "record_ids": list(node.record_ids),
                "surface_fields": list(node.surface_fields),
            }
            for node in rendered.nodes
        ],
    }
    inject_facts = bool(mode == "inject" and facts)
    inject_request_notice = bool(
        rendered.request_notice and request_satisfaction_mode == "inject"
    )
    inject_source_notices = bool(mode == "inject" and rendered.source_notices)
    if not inject_facts and not inject_request_notice and not inject_source_notices:
        if mode == "inject":
            text, numeric_separator_repairs = _repair_numeric_separators(commentary)
            text, public_source_rewrites = normalize_public_source_surface(text)
            text, duplicate_leading_sentences_removed = _deduplicate_sentences(text)
            source_block = _merged_source_block(rendered, ())
            if source_block:
                text = f"{text.rstrip()}\n\n{source_block}" if text.strip() else source_block
            mutated = text.strip() != commentary.strip()
            trace["answer_mutation"] = mutated
            trace["public_source_surface"] = {"rewritten": public_source_rewrites}
            trace["numeric_separator_repairs"] = numeric_separator_repairs
            trace["duplicate_leading_sentences_removed"] = (
                duplicate_leading_sentences_removed
            )
            return CompositionResult(
                text=text.strip(),
                answer_mutated=mutated,
                fallback_detail_retention_rate=retention,
                trace=trace,
            )
        return CompositionResult(
            text=commentary,
            answer_mutated=False,
            fallback_detail_retention_rate=retention,
            trace=trace,
        )

    public_limit_block = _retrieval_limit_block(
        request_notice=rendered.request_notice if inject_request_notice else None,
        source_notice_bindings=rendered.source_notice_bindings,
    )
    trace["body_limits_moved_to_inspection"] = {
        "request_notice": rendered.request_notice if inject_request_notice else None,
        "source_notices": list(rendered.source_notices),
        "omitted_columns": [],
        "commentary_sections": [],
        "fact_limits": [],
    }
    prefix = f"{public_limit_block}\n\n" if public_limit_block else ""
    if not inject_facts and not inject_source_notices:
        text = prefix + commentary
        source_block = _merged_source_block(rendered, ())
        if source_block:
            text = f"{text.rstrip()}\n\n{source_block}" if text.strip() else source_block
    else:
        text = _assemble_injected_answer(
            rendered,
            commentary,
            fallback=fallback,
            requested_fields_mode=requested_fields_mode,
            request_notice=rendered.request_notice if inject_request_notice else None,
            question=question,
            layout_trace=trace,
            streamed_prefix=streamed_prefix,
            deterministic_body_only=_deterministic_body_only,
            s17_validated_insight=s17_validated_insight,
        )
    text, numeric_separator_repairs = _repair_numeric_separators(text)
    notice_sources = _rendered_notice_sources(rendered)
    if trace.get("answer_axis") == "patient_statistics":
        notice_sources = tuple(source for source in notice_sources if source == "hira")
    if not streamed_prefix:
        text = append_automatic_fact_notices(text, notice_sources)
    text, public_source_rewrites = normalize_public_source_surface(text)
    text, duplicate_leading_sentences_removed = _deduplicate_sentences(text)
    trace["answer_mutation"] = True
    trace["public_source_surface"] = {"rewritten": public_source_rewrites}
    trace["numeric_separator_repairs"] = numeric_separator_repairs
    trace["duplicate_leading_sentences_removed"] = duplicate_leading_sentences_removed
    trace["requested_fields_injected"] = bool(
        inject_facts
        and requested_fields_mode == "inject"
        and requested_fields_observed
    )
    trace["request_notice_injected"] = bool(public_limit_block)
    trace["source_notices_injected"] = inject_source_notices
    trace["facts_injected_after_synthesis"] = bool(inject_facts and not streamed_prefix)
    trace["facts_streamed_before_synthesis"] = bool(streamed_prefix)
    return CompositionResult(
        text=text.strip(),
        answer_mutated=True,
        fallback_detail_retention_rate=retention,
        trace=trace,
    )


def compose_streaming_body(
    rendered: DeterministicRender,
    *,
    mode: LosslessMode,
    requested_fields_mode: RequestedFieldsMode = "shadow",
    request_satisfaction_mode: RequestSatisfactionMode = "inject",
    question: str = "",
    card_core: str | None = None,
) -> str:
    """Render the immutable record-owned body before model commentary finishes."""

    if mode != "inject":
        return ""
    body = compose_lossless_answer(
        rendered,
        "",
        synthesis_trace={"status": "streaming_body"},
        mode=mode,
        requested_fields_mode=requested_fields_mode,
        request_satisfaction_mode=request_satisfaction_mode,
        question=question,
        _deterministic_body_only=True,
    ).text
    core = ensure_core_answer_surface(
        body,
        question,
        fallback_fact_body=tuple(
            line for node in rendered.nodes for line in node.text.splitlines()
        ),
        available_axes=visible_surface_axes(rendered),
        card_core=card_core,
    ).text
    core, _removed_insight_sections = _strip_insight_sections(core)
    return strip_s17_body_metadata(core)[0]


def visible_surface_axes(rendered: DeterministicRender) -> tuple[str, ...]:
    axes: list[str] = []
    for node in rendered.nodes:
        axis = {
            "market:records": "market",
            "hira-statistics:records": "patient",
        }.get(node.block_id)
        if axis is None:
            continue
        table_lines = tuple(
            line.strip()
            for line in node.text.splitlines()
            if line.strip().startswith("|")
        )
        if len(table_lines) >= 3:
            axes.append(axis)
    return tuple(dict.fromkeys(axes))


def _rendered_notice_sources(rendered: DeterministicRender) -> tuple[str, ...]:
    source_by_prefix = {
        "hira-statistics": "hira",
        "openfda": "openfda",
        "clinical": "clinicaltrials",
        "patent": "patent",
    }
    return tuple(
        dict.fromkeys(
            source
            for node in rendered.nodes
            if node.record_ids
            for prefix, source in source_by_prefix.items()
            if node.block_id.startswith(f"{prefix}:")
        )
    )


def _assemble_injected_answer(
    rendered: DeterministicRender,
    commentary: str,
    *,
    fallback: bool,
    requested_fields_mode: RequestedFieldsMode,
    request_notice: str | None,
    question: str,
    layout_trace: dict[str, Any],
    streamed_prefix: str = "",
    deterministic_body_only: bool = False,
    s17_validated_insight: bool = False,
) -> str:
    preamble, commentary_sections = _markdown_sections(commentary)
    source_bodies = [
        body for heading, body in commentary_sections if heading == "출처" and body
    ]
    moved_commentary_limits = [
        {"heading": heading, "body": body}
        for heading, body in commentary_sections
        if heading in _LIMIT_HEADINGS and body
    ]
    commentary_sections = [
        (heading, body)
        for heading, body in commentary_sections
        if heading not in {"출처", "자동 해설"}
        and heading not in _LIMIT_HEADINGS
        and body
    ]
    layout_trace["legacy_source_summaries_removed"] = 0
    if s17_validated_insight:
        retained_sections = [
            (heading, body)
            for heading, body in commentary_sections
            if not _is_source_axis_heading(heading)
        ]
        layout_trace["legacy_source_summaries_removed"] = (
            len(commentary_sections) - len(retained_sections)
        )
        commentary_sections = retained_sections
    commentary_blocks = (
        []
        if deterministic_body_only
        else _question_driven_blocks(
            preamble,
            commentary_sections,
            fallback=fallback,
            fallback_text="## 핵심 답\n해설은 생성하지 못했고 조회 결과만 표시합니다.",
        )
    )

    axis, primary_prefixes, primary_source = _question_axis(question)
    requested_body_sources = _requested_body_sources(question)
    layout_trace["answer_axis"] = axis
    layout_trace["primary_source"] = primary_source
    layout_trace["axis_fallback_preserved_order"] = axis == "unknown"
    layout_trace["deterministic_insight_sections_removed"] = 0

    if s17_validated_insight and streamed_prefix:
        streamed_prefix, removed = _strip_insight_sections(streamed_prefix)
        layout_trace["deterministic_insight_sections_removed"] += removed

    coverage_nodes: list[RenderNode] = []
    fact_narratives: list[str] = []
    fact_nodes: list[tuple[RenderNode, str]] = []
    fact_limits: list[str] = []
    omitted_columns: list[str] = []
    nodes = rendered.nodes or (
        RenderNode(block_id=f"{rendered.profile}:facts", text=rendered.text),
    )
    for node in nodes:
        if (
            requested_fields_mode != "inject"
            and node.block_id == "requested-fields:absence"
        ):
            continue
        if not _has_visible_node_content(node):
            continue
        visible_text, node_omitted_columns = _omit_fully_unprovided_columns(
            node.text.strip()
        )
        if s17_validated_insight:
            visible_text, removed = _strip_insight_sections(visible_text)
            layout_trace["deterministic_insight_sections_removed"] += removed
        omitted_columns.extend(node_omitted_columns)
        if not visible_text:
            continue
        if node.block_id.endswith(":coverage"):
            coverage_nodes.append(node.model_copy(update={"text": visible_text}))
        elif node.block_id.startswith("narrative:"):
            fact_narratives.append(visible_text)
        elif node.block_id.endswith(":limits"):
            fact_limits.append(visible_text)
        else:
            fact_nodes.append((node, visible_text))

    unique_columns = tuple(dict.fromkeys(omitted_columns))
    layout_trace["body_limits_moved_to_inspection"] = {
        "request_notice": request_notice,
        "source_notices": list(rendered.source_notices),
        "omitted_columns": list(unique_columns),
        "commentary_sections": moved_commentary_limits,
        "fact_limits": list(fact_limits),
    }
    retrieval_limit_lines = _retrieval_limit_lines(
        request_notice=request_notice,
        source_notice_bindings=rendered.source_notice_bindings,
    )
    primary_nodes = [
        (node, text)
        for node, text in fact_nodes
        if _node_is_requested_body_fact(
            node,
            primary_prefixes=primary_prefixes,
            requested_sources=requested_body_sources,
            question=question,
        )
    ]
    layout_trace["coverage_nodes_moved_to_inspection"] = len(coverage_nodes)

    if streamed_prefix:
        if (
            not s17_validated_insight
            and axis != "unknown"
            and len(requested_body_sources) <= 1
        ):
            commentary_blocks = _align_commentary_to_axis(
                commentary_blocks,
                axis,
                primary_nodes=primary_nodes,
                question=question,
            )
        streaming_fact_nodes = [
            (node, text)
            for node, text in fact_nodes
            if _node_is_requested_body_fact(
                node,
                primary_prefixes=primary_prefixes,
                requested_sources=requested_body_sources,
                question=question,
            )
        ]
        moved_streaming_nodes = [
            (node, text)
            for node, text in fact_nodes
            if (node, text) not in streaming_fact_nodes
        ]
        moved_by_source = _record_counts_by_source(moved_streaming_nodes)
        layout_trace["adjacent_records_moved_to_inspection_by_source"] = moved_by_source
        layout_trace["adjacent_clinical_records_moved_to_inspection"] = moved_by_source.get(
            "clinicaltrials", 0
        )
        unemitted_facts = _streaming_unemitted_fact_blocks(
            streaming_fact_nodes,
            streamed_prefix,
        )
        commentary_tail = _streaming_commentary_blocks(
            commentary_blocks,
            preserve_validated_insight=s17_validated_insight,
        )
        blocks = [streamed_prefix, *unemitted_facts, *commentary_tail]
        layout_trace["streaming_prefix_chars"] = len(streamed_prefix)
        layout_trace["facts_in_streaming_prefix"] = True
        layout_trace["facts_appended_after_streaming"] = len(unemitted_facts)
        layout_trace["primary_body_fact_count"] = _markdown_table_data_row_count(
            streamed_prefix
        )
    elif axis == "unknown":
        blocks = [
            *fact_narratives,
            *commentary_blocks,
            *(text for _, text in fact_nodes),
            *(_render_sections((("조회 제한", "\n".join(retrieval_limit_lines)),))),
        ]
    else:
        comparison_prefixes = (
            ("file-source-comparison:",)
            if has_explicit_file_source_comparison(question)
            else ()
        )
        primary_nodes = [
            (node, text)
            for node, text in fact_nodes
            if _node_is_requested_body_fact(
                node,
                primary_prefixes=(*primary_prefixes, *comparison_prefixes),
                requested_sources=requested_body_sources,
                question=question,
            )
        ]
        if not s17_validated_insight and len(requested_body_sources) <= 1:
            commentary_blocks = _align_commentary_to_axis(
                commentary_blocks,
                axis,
                primary_nodes=primary_nodes,
                question=question,
            )
        comparison_blocks = [
            block for block in commentary_blocks if block.startswith("## 비교 관측\n")
        ]
        if comparison_blocks:
            commentary_blocks = [
                block for block in commentary_blocks if block not in comparison_blocks
            ]
            layout_trace["comparison_observation_sections_removed"] = len(comparison_blocks)
        secondary_nodes = [
            (node, text)
            for node, text in fact_nodes
            if (node, text) not in primary_nodes
        ]
        moved_by_source = _record_counts_by_source(secondary_nodes)
        layout_trace["adjacent_records_moved_to_inspection_by_source"] = moved_by_source
        layout_trace["adjacent_clinical_records_moved_to_inspection"] = moved_by_source.get(
            "clinicaltrials", 0
        )
        (
            commentary_blocks,
            dimension_notices,
            promoted_commentary,
        ) = _align_patient_dimension_commentary(
            commentary_blocks,
            question=question,
            axis=axis,
            primary_nodes=primary_nodes,
        )
        if dimension_notices:
            layout_trace["body_limits_moved_to_inspection"]["dimension_notices"] = list(
                dimension_notices
            )
            retrieval_limit_lines.extend(f"- {notice}" for notice in dimension_notices)
        lead_blocks, deferred_commentary = _partition_lead_commentary(commentary_blocks)
        absence_block, absence_reason = _primary_absence_block(
            axis,
            primary_source,
            primary_nodes,
            rendered.source_notice_bindings,
            question=question,
        )
        if absence_block:
            lead_blocks.insert(0, absence_block)
            layout_trace["primary_axis_absence"] = absence_reason
            absence_type = _absence_type(absence_reason)
            layout_trace["primary_axis_absence_type"] = absence_type
            layout_trace["primary_axis_absence_id"] = (
                f"absence:{axis}:{primary_source}:{absence_type.casefold()}"
            )
        moved_secondary_records = sum(_record_counts_by_source(secondary_nodes).values())
        compacted: list[str] = []
        layout_trace["secondary_records_compacted"] = 0
        layout_trace["secondary_records_moved_to_inspection"] = moved_secondary_records
        limited_primary: list[str] = []
        hidden_primary_rows = 0
        for node, text in primary_nodes:
            limited, hidden = _limit_markdown_table_rows(
                text,
                row_limit=configured_primary_table_row_limit(node.block_id),
                preferred_periods=(
                    _requested_years(question)
                    if node.block_id == "market:records"
                    else ()
                ),
            )
            limited_primary.append(limited)
            hidden_primary_rows += hidden
        layout_trace["primary_table_rows_hidden"] = hidden_primary_rows
        layout_trace["primary_body_fact_count"] = sum(
            _markdown_table_data_row_count(block) for block in limited_primary
        )
        homogeneous_patient_narratives = (
            promoted_commentary
            + _homogeneous_patient_narrative_count(
                fact_narratives,
                axis=axis,
                primary_nodes=primary_nodes,
            )
        )
        layout_trace["homogeneous_patient_narratives_promoted"] = (
            homogeneous_patient_narratives
            if homogeneous_patient_narratives >= _HOMOGENEOUS_NARRATIVE_TABLE_THRESHOLD
            else 0
        )
        layout_trace["mechanical_narratives_compacted"] = len(fact_narratives)
        core_blocks = _merge_primary_into_core(lead_blocks, limited_primary)
        blocks = [
            *core_blocks,
            *deferred_commentary,
            *compacted,
            *(_render_sections((("조회 제한", "\n".join(retrieval_limit_lines)),))),
        ]
    comparison_facts_present = any(
        node.block_id.startswith("file-source-comparison:")
        for node, _text in fact_nodes
    )
    document_facts_present = any(
        _node_source(node.block_id) == "document" for node, _text in fact_nodes
    )
    document_absence_guarded = _document_absence_guard_is_grounded(
        question,
        tuple(
            text
            for node, text in fact_nodes
            if _node_source(node.block_id) == "document"
        ),
    )
    core_absence_source_records = {
        source: int(selection.get("records_received", 0) or 0)
        for source, selection in rendered.selection_by_source.items()
    } if comparison_facts_present else (
        {"document": int(rendered.coverage.records_received)}
        if document_absence_guarded else {}
    )
    source_evidence: dict[str, list[str]] = {}
    for node, text in fact_nodes:
        structured_evidence = _structured_lane_evidence(text)
        if structured_evidence:
            source_evidence.setdefault(_node_source(node.block_id), []).append(
                structured_evidence
            )
    requested_evidence_sources = {
        _node_source(node.block_id)
        for node, _text in (primary_nodes if axis != "unknown" else fact_nodes)
    }
    requested_source_evidence = {
        source: "\n".join(evidence)
        for source, evidence in source_evidence.items()
        if source in requested_evidence_sources
    }
    if s17_validated_insight:
        insight_trace = {
            "s17_fact_digest_validated": True,
            "legacy_body_revalidation_skipped": True,
        }
    else:
        blocks, insight_trace = _bind_insight_blocks_to_body(
            blocks,
            axis=axis,
            question=question,
            source_evidence_by_lane={
                source: "\n".join(evidence)
                for source, evidence in source_evidence.items()
            },
            records_present=(
                rendered.coverage.records_received > 0
                if comparison_facts_present
                else bool(primary_nodes if axis != "unknown" else fact_nodes)
            ),
            body_records_present=bool(fact_nodes),
            guard_core_absence=comparison_facts_present or document_facts_present,
            core_absence_source_records=core_absence_source_records,
        )
    layout_trace["insight_body_binding"] = insight_trace
    if (
        not s17_validated_insight
        and not deterministic_body_only
        and fact_nodes
        and question.strip()
    ):
        blocks, richness_trace = _ensure_s9_insight_contract(
            blocks,
            axis=axis,
            question=question,
            source_evidence_by_lane=requested_source_evidence,
        )
        layout_trace["insight_richness"] = richness_trace
        if (
            richness_trace["fallback_applied"]
            and rendered.profile in {"clinical_portfolio", "patent_portfolio"}
        ):
            layout_trace["advisory_fallback_reason"] = (
                "missing_synthesized_advisory"
            )
    if (
        not deterministic_body_only
        and rendered.profile in {"clinical_portfolio", "patent_portfolio"}
        and fact_nodes
        and not any(block.startswith("## 종합 인사이트\n") for block in blocks)
    ):
        layout_trace["advisory_omitted_reason"] = "missing_grounded_insight"
    source_block = (
        ""
        if deterministic_body_only
        else _merged_source_block(
            rendered,
            source_bodies,
            allowed_sources=(
                frozenset({primary_source})
                if axis == "patient_statistics" and primary_source
                else None
            ),
        )
    )
    if source_block:
        blocks.append(source_block)
    return "\n\n".join(block for block in blocks if block.strip()).strip()


def _streaming_commentary_blocks(
    blocks: Sequence[str],
    *,
    preserve_validated_insight: bool = False,
) -> list[str]:
    """Move model prose behind the already emitted deterministic body."""

    output: list[str] = []
    for block in blocks:
        if block.startswith("## 핵심 답\n"):
            if preserve_validated_insight:
                continue
            body = block.removeprefix("## 핵심 답\n").strip()
            if body:
                output.append(f"## 종합 인사이트\n{body}")
        elif block.startswith("## "):
            if preserve_validated_insight and not _is_insight_block(block):
                continue
            output.append(block)
        elif block.strip():
            output.append(f"## 종합 인사이트\n{block.strip()}")
    return output


def _strip_insight_sections(text: str) -> tuple[str, int]:
    stripped, removed = _INSIGHT_SECTION_RE.subn("", text)
    return stripped.strip(), removed


def _streaming_unemitted_fact_blocks(
    fact_nodes: Sequence[tuple[RenderNode, str]],
    streamed_prefix: str,
) -> list[str]:
    output: list[str] = []
    for _node, text in fact_nodes:
        candidate = _remove_streamed_table_blocks(text, streamed_prefix)
        candidate, _cleanup_trace = strip_s17_body_metadata(candidate)
        content_lines = tuple(
            line.strip()
            for line in candidate.splitlines()
            if line.strip()
            and not line.lstrip().startswith("#")
            and not re.fullmatch(r"[| :\-]+", line.strip())
        )
        if content_lines and all(line in streamed_prefix for line in content_lines):
            continue
        if candidate:
            output.append(candidate)
    return output


def _remove_streamed_table_blocks(text: str, streamed_prefix: str) -> str:
    streamed_tables = _markdown_table_signatures(streamed_prefix)
    if not streamed_tables:
        return text
    lines = text.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            output.append(lines[index])
            index += 1
            continue
        table_lines: list[str] = []
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            table_lines.append(lines[index])
            index += 1
        signature = "\n".join(line.strip() for line in table_lines)
        if signature not in streamed_tables:
            output.extend(table_lines)
    return "\n".join(output)


def _markdown_table_signatures(text: str) -> set[str]:
    signatures: set[str] = set()
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        table_lines: list[str] = []
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            table_lines.append(lines[index])
            index += 1
        signatures.add("\n".join(line.strip() for line in table_lines))
    return signatures


def _bind_insight_blocks_to_body(
    blocks: Sequence[str],
    *,
    axis: str,
    question: str = "",
    source_evidence_by_lane: Mapping[str, str],
    records_present: bool,
    body_records_present: bool,
    guard_core_absence: bool = False,
    core_absence_source_records: Mapping[str, int] | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Keep synthesized insight claims inside the visible deterministic fact boundary."""

    body_text = "\n\n".join(
        block for block in blocks if not _is_insight_block(block)
    )
    body_numbers = {
        _normalize_insight_number(match.group(0))
        for match in _INSIGHT_NUMBER_RE.finditer(body_text)
    }
    body_identifiers = _body_identifier_tokens(body_text)
    trace = {
        "sections_checked": 0,
        "numeric_sentences_removed": 0,
        "identifier_sentences_removed": 0,
        "false_absence_sentences_removed": 0,
        "empty_sections_removed": 0,
        "grounded_fallback_sections": 0,
        "provenance_added": 0,
        "provenance_rebound": 0,
        "multi_source_sentences": 0,
        "unbound_sentences": 0,
        "cross_lane_numeric_sentences_removed": 0,
    }
    output: list[str] = []
    for block in blocks:
        guarded_core = guard_core_absence and block.startswith("## 핵심 답\n")
        if not _is_insight_block(block) and not guarded_core:
            output.append(block)
            continue
        trace["sections_checked"] += 1
        if guarded_core:
            heading = "## 핵심 답"
            body = block[len("## 핵심 답\n") :]
        else:
            insight_match = _INSIGHT_BLOCK_RE.match(block)
            if insight_match is None:
                output.append(block)
                continue
            heading = f"## {insight_match.group('heading')}"
            body = block[insight_match.end() :]
        retained: list[str] = []
        for paragraph in re.split(r"\n\s*\n", body):
            if not _substantive_insight_text(paragraph):
                continue
            kept_sentences: list[str] = []
            for sentence in re.split(r"(?<=[.!?])\s+", paragraph.strip()):
                sentence = sentence.strip()
                if not sentence or _INSIGHT_LABEL_ONLY_RE.fullmatch(sentence):
                    continue
                original_citations = tuple(_INSIGHT_CITATION_RE.findall(sentence))
                sentence = _INSIGHT_CITATION_RE.sub("", sentence).strip()
                sentence = re.sub(r"\s+([.!?])", r"\1", sentence)
                if not sentence:
                    continue
                absence_claim = _is_absence_claim(
                    sentence.replace(_UNPROVIDED_CELL, "")
                )
                source_absence_is_contradicted = (
                    absence_claim
                    and _comparison_absence_is_contradicted(
                        sentence,
                        core_absence_source_records or {},
                    )
                )
                false_absence = source_absence_is_contradicted or (
                    not guarded_core
                    and (
                        _is_false_axis_absence(sentence, axis)
                        or (
                            axis == "document"
                            and _is_document_summary_request(question)
                            and absence_claim
                        )
                    )
                )
                if records_present and false_absence:
                    trace["false_absence_sentences_removed"] += 1
                    continue
                numbers = {
                    _normalize_insight_number(match.group(0))
                    for match in _INSIGHT_NUMBER_RE.finditer(sentence)
                }
                if numbers and (
                    not numbers <= body_numbers
                    or _has_unbound_derived_value(sentence, body_text)
                ):
                    trace["numeric_sentences_removed"] += 1
                    continue
                if numbers and not _has_insight_identifier(
                    sentence,
                    body_identifiers=body_identifiers,
                ):
                    trace["identifier_sentences_removed"] += 1
                    continue
                if guarded_core:
                    kept_sentences.append(sentence)
                    continue
                matching_sources = _matching_insight_sources(
                    sentence,
                    source_evidence_by_lane=source_evidence_by_lane,
                    visible_body=body_text,
                )
                if numbers and not absence_claim and not _numbers_bound_to_sources(
                    numbers,
                    matching_sources,
                    source_evidence_by_lane=source_evidence_by_lane,
                ):
                    trace["cross_lane_numeric_sentences_removed"] += 1
                    continue
                if matching_sources:
                    source_labels = " · ".join(
                        _SOURCE_LABELS.get(source, source)
                        for source in matching_sources
                    )
                    sentence = f"{sentence} [출처: {source_labels}]"
                    if original_citations:
                        trace["provenance_rebound"] += 1
                    else:
                        trace["provenance_added"] += 1
                    if len(matching_sources) > 1:
                        trace["multi_source_sentences"] += 1
                else:
                    trace["unbound_sentences"] += 1
                    if absence_claim:
                        kept_sentences.append(sentence)
                    continue
                kept_sentences.append(sentence)
            if kept_sentences:
                retained.append(" ".join(kept_sentences))
        if retained:
            output.append(f"{heading}\n" + "\n\n".join(retained))
        else:
            trace["empty_sections_removed"] += 1
    return output, trace


def _ensure_s9_insight_contract(
    blocks: Sequence[str],
    *,
    axis: str,
    question: str,
    source_evidence_by_lane: Mapping[str, str],
) -> tuple[list[str], dict[str, Any]]:
    sanitized_blocks, conflicts_removed = _remove_axis_confirmation_conflicts(blocks)
    insight_indexes = tuple(
        index
        for index, block in enumerate(sanitized_blocks)
        if _is_insight_block(block)
    )
    current_body = "\n\n".join(
        _INSIGHT_BLOCK_RE.sub("", sanitized_blocks[index], count=1).strip()
        for index in insight_indexes
    ).strip()
    before = _insight_richness_metrics(
        current_body,
        source_evidence_by_lane,
        question=question,
    )
    if before["contract_met"]:
        output = list(sanitized_blocks)
        duplicate_blocks_collapsed = max(0, len(insight_indexes) - 1)
        if duplicate_blocks_collapsed:
            output = [
                block
                for index, block in enumerate(sanitized_blocks)
                if index not in insight_indexes
            ]
            insertion_index = insight_indexes[0]
            output.insert(
                min(insertion_index, len(output)),
                f"## 종합 인사이트\n{current_body}",
            )
        _record_s14a_cross_question_skeletons(question, current_body)
        return output, {
            "fallback_applied": False,
            "duplicate_blocks_collapsed": duplicate_blocks_collapsed,
            "axis_confirmation_conflicts_removed": conflicts_removed,
            **before,
        }

    output = [
        block
        for index, block in enumerate(sanitized_blocks)
        if index not in insight_indexes
    ]
    reason_code = (
        "MISSING_EVIDENCE"
        if before["question_independent_sentence_count"]
        or before["blacklist_hits"]
        else "AXIS_UNCLOSED"
        if conflicts_removed
        else "MISSING_REQUIRED_ROLE"
    )
    return output, {
        "fallback_applied": False,
        "omitted": True,
        "reason_code": reason_code,
        "duplicate_blocks_collapsed": max(0, len(insight_indexes) - 1),
        "axis_confirmation_conflicts_removed": conflicts_removed,
        "before": before,
        **before,
    }


def _remove_axis_confirmation_conflicts(
    blocks: Sequence[str],
) -> tuple[tuple[str, ...], int]:
    core = "\n".join(
        block
        for block in blocks
        if re.match(r"^##[ \t]+(?:핵심 답|핵심 요약)\b", block)
    )
    unavailable_terms = tuple(
        terms
        for label, terms in (
            ("환자수", ("환자수", "환자 수")),
            ("유병률", ("유병률",)),
            ("매출", ("매출", "총액", "판매액", "sellout", "sell out")),
            ("점유율", ("점유율", "시장점유율", "market share")),
            ("특허", ("특허", "만료일")),
            ("허가", ("허가", "품목")),
            ("임상", ("임상", "clinical", "trial", "nct")),
            ("문서", ("문서", "파일", "청크", "요약", "집계")),
        )
        if re.search(
            rf"(?:요청하신\s+)?{re.escape(label)}[^.!?]{{0,80}}"
            r"(?:확인되지\s*않|산출하지\s*않|미제공|없습니다)",
            core,
            flags=re.IGNORECASE,
        )
    )
    if not unavailable_terms:
        return tuple(blocks), 0

    confirmation_terms = (
        "확인한",
        "확인된",
        "확인했습니다",
        "확인할 수",
        "파악한",
        "파악된",
        "확보됐",
        "확보된",
        "집계됐",
        "집계된",
        "나타났",
        "나타난",
    )
    absence_terms = (
        "확인되지",
        "확인하지 못",
        "산출하지 않",
        "미제공",
        "없습니다",
        "시간 초과",
        "실패",
    )
    output: list[str] = []
    removed = 0
    for block in blocks:
        if not _is_insight_block(block):
            output.append(block)
            continue
        heading = _INSIGHT_BLOCK_RE.match(block)
        body = _INSIGHT_BLOCK_RE.sub("", block, count=1).strip()
        paragraphs: list[str] = []
        for paragraph in re.split(r"\n\s*\n", body):
            sentences = tuple(
                sentence.strip()
                for sentence in re.split(r"(?<=[.!?])\s+", paragraph)
                if sentence.strip()
            )
            retained = []
            for sentence in sentences:
                normalized = sentence.casefold()
                conflicts = any(
                    any(term.casefold() in normalized for term in axis_terms)
                    and (
                        any(term in sentence for term in confirmation_terms)
                        or not any(term in sentence for term in absence_terms)
                    )
                    for axis_terms in unavailable_terms
                )
                if conflicts:
                    removed += 1
                else:
                    retained.append(sentence)
            if retained:
                retained[0] = re.sub(
                    r"^(?:그러나|따라서)\s*[,，]?\s*",
                    "",
                    retained[0],
                )
                paragraphs.append(" ".join(retained))
        prefix = heading.group(0).strip() if heading is not None else "## 종합 인사이트"
        output.append(f"{prefix}\n" + "\n\n".join(paragraphs))
    return tuple(output), removed


def ensure_s9_insight_surface(
    answer: str,
    *,
    question: str,
    sources: Sequence[str],
    fact_digest: FactDigest | None = None,
) -> tuple[str, dict[str, Any]]:
    """Recheck the S9 insight contract after semantic surface sanitization."""
    if fact_digest is not None:
        return sanitize_s17_insight(answer, fact_digest)
    visible_sources = _prioritize_s9_sources(question, sources)
    if not answer.strip() or not visible_sources:
        return answer, {
            "fallback_applied": False,
            "contract_met": False,
            "reason": "no_visible_sources",
        }
    blocks = tuple(
        block.strip()
        for block in re.split(r"(?=^##[ \t]+)", answer, flags=re.MULTILINE)
        if block.strip()
    )
    axis, _prefixes, _source = _question_axis(question)
    repaired, trace = _ensure_s9_insight_contract(
        blocks,
        axis=axis,
        question=question,
        source_evidence_by_lane={source: "present" for source in visible_sources},
    )
    return "\n\n".join(repaired).strip(), trace


def _prioritize_s9_sources(question: str, sources: Sequence[str]) -> tuple[str, ...]:
    visible_sources = tuple(dict.fromkeys(source for source in sources if source))
    normalized = " ".join(question.casefold().split())
    requested = tuple(
        source
        for source, terms in (
            ("hira", ("환자", "유병률")),
            (
                "mart",
                (
                    "매출",
                    "총액",
                    "점유율",
                    "시장점유율",
                    "market share",
                    "sellout",
                    "sell out",
                ),
            ),
            ("patent", ("특허", "만료")),
            ("nedrug", ("허가", "품목")),
            ("clinicaltrials", ("임상", "clinical", "trial", "nct")),
            ("openfda", ("fda", "부작용", "이상사례")),
        )
        if source in visible_sources
        and any(_axis_token_matches(normalized, term) for term in terms)
    )
    return tuple(dict.fromkeys((*requested, *visible_sources)))


def visible_s9_sources(rendered: DeterministicRender) -> tuple[str, ...]:
    """Return only sources that have visible, record-backed rendered facts."""
    return tuple(
        dict.fromkeys(
            source
            for node in rendered.nodes
            if node.record_ids and _has_visible_node_content(node)
            if (source := _node_source(node.block_id)) in _SOURCE_LABELS
        )
    )


def _insight_richness_metrics(
    body: str,
    source_evidence_by_lane: Mapping[str, str],
    *,
    question: str = "",
) -> dict[str, Any]:
    paragraphs = tuple(
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", body)
        if paragraph.strip()
    )
    sentences = tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", body)
        if sentence.strip()
    )
    labels = tuple(
        _SOURCE_LABELS.get(source, source) for source in source_evidence_by_lane
    )
    fusion_sentences = sum(
        1
        for sentence in sentences
        if sum(label in sentence for label in labels) >= min(2, len(labels))
        and len(labels) >= 2
    )
    fusion_unavailable_count = body.count("FUSION_UNAVAILABLE")
    normalized_sentences = tuple(_s14a_sentence_skeleton(sentence) for sentence in sentences)
    duplicate_sentences = len(normalized_sentences) - len(set(normalized_sentences))
    max_skeleton_jaccard = _max_s14a_skeleton_jaccard(normalized_sentences)
    cross_question_skeleton_jaccard = _s14a_cross_question_skeleton_jaccard(
        question,
        normalized_sentences,
    )
    question_independent_sentences = sum(
        1
        for sentence in sentences
        if "FUSION_UNAVAILABLE" not in sentence
        and not _s14a_sentence_has_concrete_slot(sentence)
    )
    blacklist_hits = sum(body.count(term) for term in _S14A_PROSE_BLACKLIST)
    contract_met = (
        len(paragraphs) == 3
        and 4 <= len(sentences) <= 6
        and blacklist_hits == 0
        and duplicate_sentences == 0
        and max_skeleton_jaccard < 0.70
        and cross_question_skeleton_jaccard < 0.85
        and question_independent_sentences == 0
        and not any(paragraph.startswith("또한,") for paragraph in paragraphs)
        and (len(labels) < 2 or fusion_sentences >= 1 or fusion_unavailable_count == 1)
    )
    return {
        "paragraph_count": len(paragraphs),
        "sentence_count": len(sentences),
        "character_count": len(body),
        "fusion_sentence_count": fusion_sentences,
        "fusion_unavailable_count": fusion_unavailable_count,
        "limitation_count": body.count("[확인 한계]"),
        "blacklist_hits": blacklist_hits,
        "duplicate_sentence_count": duplicate_sentences,
        "max_skeleton_jaccard": max_skeleton_jaccard,
        "cross_question_skeleton_jaccard": cross_question_skeleton_jaccard,
        "question_independent_sentence_count": question_independent_sentences,
        "contract_met": contract_met,
    }


def _s14a_sentence_skeleton(sentence: str) -> str:
    normalized = _INSIGHT_CITATION_RE.sub("", sentence).casefold()
    normalized = re.sub(r"20\d{2}(?:[-./년]\d{1,2})?(?:[-./월]\d{1,2})?", "<date>", normalized)
    normalized = re.sub(r"\d+(?:,\d{3})*(?:\.\d+)?", "<value>", normalized)
    for label in sorted(_SOURCE_LABELS.values(), key=len, reverse=True):
        normalized = normalized.replace(label.casefold(), "<source>")
    return re.sub(r"\s+", " ", normalized).strip(" .!?")


def _max_s14a_skeleton_jaccard(sentences: Sequence[str]) -> float:
    maximum = 0.0
    grams = tuple(
        {sentence[index : index + 5] for index in range(max(0, len(sentence) - 4))}
        for sentence in sentences
    )
    for left_index, left in enumerate(grams):
        for right in grams[left_index + 1 :]:
            union = left | right
            if union:
                maximum = max(maximum, len(left & right) / len(union))
    return maximum


def _s14a_cross_question_skeleton_jaccard(
    question: str,
    sentences: Sequence[str],
) -> float:
    question_key = " ".join(question.casefold().split())
    if not question_key or not sentences:
        return 0.0
    with _S14A_RECENT_SENTENCE_LOCK:
        previous = tuple(_S14A_RECENT_SENTENCE_SKELETONS)
    return max(
        (
            _s14a_skeleton_pair_jaccard(sentence, previous_sentence)
            for sentence in sentences
            for previous_question, previous_sentence in previous
            if previous_question != question_key
        ),
        default=0.0,
    )


def _record_s14a_cross_question_skeletons(question: str, body: str) -> None:
    question_key = " ".join(question.casefold().split())
    if not question_key:
        return
    skeletons = tuple(
        _s14a_sentence_skeleton(sentence.strip())
        for sentence in re.split(r"(?<=[.!?])\s+", body)
        if sentence.strip()
    )
    with _S14A_RECENT_SENTENCE_LOCK:
        _S14A_RECENT_SENTENCE_SKELETONS.extend(
            (question_key, skeleton) for skeleton in skeletons if skeleton
        )


def _s14a_skeleton_pair_jaccard(left: str, right: str) -> float:
    left_grams = {left[index : index + 5] for index in range(max(0, len(left) - 4))}
    right_grams = {right[index : index + 5] for index in range(max(0, len(right) - 4))}
    union = left_grams | right_grams
    return len(left_grams & right_grams) / len(union) if union else 0.0


def _s14a_sentence_has_concrete_slot(sentence: str) -> bool:
    normalized = sentence.casefold()
    slot_terms = (
        *tuple(label.casefold() for label in _SOURCE_LABELS.values()),
        "환자수",
        "유병률",
        "매출",
        "총액",
        "점유율",
        "특허",
        "만료일",
        "허가",
        "임상",
        "문서",
        "기간",
        "성별",
        "연령",
        "상병",
        "시장",
        "실패",
        "시간 초과",
    )
    return bool(_INSIGHT_NUMBER_RE.search(sentence)) or any(
        term in normalized for term in slot_terms
    )


def _is_insight_block(block: str) -> bool:
    return _INSIGHT_BLOCK_RE.match(block) is not None


def _markdown_table_data_row_count(value: str) -> int:
    rows = [
        cells
        for line in value.splitlines()
        if (cells := _split_markdown_row(line)) is not None
    ]
    if len(rows) < 2:
        return 0
    separator_index = next(
        (
            index
            for index, cells in enumerate(rows)
            if cells and all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells)
        ),
        None,
    )
    if separator_index is None:
        return 0
    return sum(
        1
        for cells in rows[separator_index + 1 :]
        if cells and any(cell.strip() for cell in cells)
    )


def _substantive_insight_text(value: str) -> bool:
    lines = [
        line.strip()
        for line in value.splitlines()
        if line.strip() and not _INSIGHT_LABEL_ONLY_RE.fullmatch(line)
    ]
    return bool("".join(lines).strip("-*_:： `"))


def _normalize_insight_number(value: str) -> str:
    return value.replace(",", "").lstrip("0") or "0"


def _matching_insight_sources(
    sentence: str,
    *,
    source_evidence_by_lane: Mapping[str, str],
    visible_body: str,
) -> tuple[str, ...]:
    sentence_tokens = _source_binding_tokens(sentence)
    if not sentence_tokens:
        return ()
    visible_tokens = _source_binding_tokens(visible_body)
    normalized_sentence = sentence.casefold()
    explicit_positions = [
        (
            min(normalized_sentence.find(cue) for cue in cues if cue in normalized_sentence),
            source,
        )
        for source, cues in _SOURCE_BINDING_CUES.items()
        if source in source_evidence_by_lane
        and any(cue in normalized_sentence for cue in cues)
        and _source_binding_tokens(source_evidence_by_lane[source]) & visible_tokens
    ]
    explicit_sources = tuple(
        source for _position, source in sorted(explicit_positions)
    )
    if explicit_sources:
        return explicit_sources
    matches: list[str] = []
    for source, evidence in source_evidence_by_lane.items():
        evidence_tokens = _source_binding_tokens(evidence) & visible_tokens
        if any(
            _source_binding_token_matches(sentence_token, evidence_token)
            for sentence_token in sentence_tokens
            for evidence_token in evidence_tokens
        ):
            matches.append(source)
    return tuple(matches)


def _numbers_bound_to_sources(
    numbers: set[str],
    sources: Sequence[str],
    *,
    source_evidence_by_lane: Mapping[str, str],
) -> bool:
    source_numbers = {
        _normalize_insight_number(match.group(0))
        for source in sources
        for match in _INSIGHT_NUMBER_RE.finditer(
            source_evidence_by_lane.get(source, "")
        )
    }
    return numbers <= source_numbers


def _visible_evidence_sources(
    source_evidence_by_lane: Mapping[str, str],
    *,
    visible_body: str,
) -> tuple[str, ...]:
    visible_tokens = _source_binding_tokens(visible_body)
    return tuple(
        source
        for source, evidence in source_evidence_by_lane.items()
        if _source_binding_tokens(evidence) & visible_tokens
    )


def _source_binding_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(
            r"[A-Za-z][A-Za-z0-9._-]+|[가-힣]{2,}", value.casefold()
        )
        if token not in _SOURCE_BINDING_STOPWORDS
    }


def _source_binding_token_matches(left: str, right: str) -> bool:
    if left == right:
        return True
    if any(character.isdigit() for character in left + right):
        return False
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 2 and longer.startswith(shorter)


def _structured_lane_evidence(value: str) -> str:
    lines = value.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        header = _split_markdown_row(lines[index])
        separator = (
            _split_markdown_row(lines[index + 1])
            if index + 1 < len(lines)
            else None
        )
        if (
            header is None
            or separator is None
            or len(header) != len(separator)
            or not all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in separator)
        ):
            line = lines[index]
            normalized = line.casefold()
            if not (
                line.lstrip().startswith(("- ", "* "))
                and any(
                    label in normalized
                    for label in _SOURCE_BINDING_FREE_TEXT_LABELS
                )
            ):
                output.append(line)
            index += 1
            continue

        retained_indexes = tuple(
            column_index
            for column_index, column in enumerate(header)
            if not any(
                label in column.casefold()
                for label in _SOURCE_BINDING_FREE_TEXT_LABELS
            )
        )
        cursor = index + 2
        data_rows: list[list[str]] = []
        while cursor < len(lines):
            row = _split_markdown_row(lines[cursor])
            if row is None or len(row) != len(header):
                break
            data_rows.append(row)
            cursor += 1
        if retained_indexes:
            output.extend(
                _join_markdown_row(row, retained_indexes)
                for row in (header, separator, *data_rows)
            )
        index = cursor
    return "\n".join(output).strip()


def _body_identifier_tokens(body: str) -> set[str]:
    identifiers: set[str] = set()
    for line in body.splitlines():
        cells = _split_markdown_row(line)
        if cells is None or all(
            _TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells
        ):
            continue
        for cell in cells:
            token = cell.strip().casefold()
            if (
                len(token) >= 2
                and not any(character.isdigit() for character in token)
                and not _INSIGHT_NUMBER_RE.fullmatch(
                    token.replace("원", "").replace("명", "")
                )
                and token
                not in {"항목", "값", "기간", "매출", "환자수", "성별", "연령대"}
            ):
                identifiers.add(token)
    return identifiers


def _has_insight_identifier(
    sentence: str,
    *,
    body_identifiers: set[str],
) -> bool:
    if _INSIGHT_IDENTIFIER_RE.search(sentence):
        return True
    normalized = sentence.casefold()
    return any(identifier in normalized for identifier in body_identifiers)


def _has_unbound_derived_value(sentence: str, body: str) -> bool:
    for marker in _INSIGHT_DERIVED_MARKERS:
        if marker not in sentence:
            continue
        expressions = {
            match.group(0).replace(" ", "")
            for match in re.finditer(
                rf"\d+(?:,\d{{3}})*(?:\.\d+)?\s*{re.escape(marker)}",
                sentence,
            )
        }
        if expressions and not all(
            expression in body.replace(" ", "") for expression in expressions
        ):
            return True
    return False


def _is_false_axis_absence(sentence: str, axis: str) -> bool:
    if not _is_absence_claim(sentence):
        return False
    axis_terms = {
        "sales": ("매출", "실적"),
        "market_share": ("점유율",),
        "patient_statistics": ("환자수", "환자 수", "유병률"),
        "clinical": ("임상",),
        "patent": ("특허",),
        "reimbursement": ("급여", "고시"),
        "approval": ("허가", "품목"),
        "document": ("문서", "파일"),
    }.get(axis, ())
    return bool(axis_terms and any(term in sentence for term in axis_terms))


def _is_document_summary_request(question: str) -> bool:
    normalized = " ".join(question.casefold().split())
    return any(marker in normalized for marker in ("내용", "정리", "요약"))


def _document_absence_guard_is_grounded(
    question: str,
    document_fact_blocks: Sequence[str],
) -> bool:
    """Require the requested period and metric before rejecting document absence."""

    if not document_fact_blocks:
        return False
    if _is_document_summary_request(question):
        return True

    normalized_question = " ".join(question.casefold().split())
    normalized_facts = " ".join(
        " ".join(document_fact_blocks).casefold().replace("\\", "").split()
    )
    requested_groups = tuple(
        aliases
        for aliases in _ABSENCE_RELEVANCE_GROUPS
        if any(alias in normalized_question for alias in aliases)
    )
    requested_periods = _canonical_period_markers(normalized_question)
    fact_periods = _canonical_period_markers(normalized_facts)
    return bool(
        requested_groups
        and all(any(alias in normalized_facts for alias in aliases) for aliases in requested_groups)
        and requested_periods <= fact_periods
    )


def _canonical_period_markers(value: str) -> frozenset[str]:
    markers: set[str] = set(_YEAR_RE.findall(value))
    for match in _YEAR_MONTH_RE.finditer(value):
        month = int(match.group("month"))
        if 1 <= month <= 12:
            markers.add(f"{match.group('year')}-{month:02d}")
            markers.add(f"month:{month:02d}")
    for match in _MONTH_RE.finditer(value):
        month = int(match.group("month"))
        if 1 <= month <= 12:
            markers.add(f"month:{month:02d}")
    return frozenset(markers)


def _is_absence_claim(sentence: str) -> bool:
    return _INSIGHT_ABSENCE_RE.search(sentence) is not None


def _comparison_absence_is_contradicted(
    sentence: str,
    source_records: Mapping[str, int],
) -> bool:
    aliases = {
        "document": ("업로드 파일", "업로드 문서", "파일값", "파일 값"),
        "mart": ("내부 마트", "내부 데이터마트", "마트"),
        "hira": ("심평원", "건강보험심사평가원"),
    }
    mentioned = {
        source
        for source, labels in aliases.items()
        if any(label in sentence for label in labels)
    }
    if mentioned:
        if len(mentioned) > 1 and any(
            marker in sentence for marker in ("모두", "양쪽", "둘 다", "및")
        ):
            return any(source_records.get(source, 0) > 0 for source in mentioned)
        return all(source_records.get(source, 0) > 0 for source in mentioned)
    return bool(source_records) and all(count > 0 for count in source_records.values())


def _requested_body_sources(question: str) -> frozenset[str]:
    """Return only sources whose tabular axis the user explicitly requested."""

    normalized = " ".join(question.casefold().split())
    hira_cost_total = any(
        term in normalized
        for term in ("요양급여비용총액", "요양 급여 비용 총액")
    )
    source_terms = (
        ("hira", ("환자", "유병률", "상병", "요양급여", "급여기준", "급여 기준")),
        (
            "mart",
            ("매출", "총액", "점유율", "시장점유", "sellout", "sell out", "실적"),
        ),
        ("patent", ("특허", "만료")),
        ("clinicaltrials", ("임상", "nct", "clinical", "trial")),
        ("nedrug", ("허가", "품목")),
        ("openfda", ("fda", "부작용", "이상사례")),
        ("web", ("뉴스", "보도", "웹")),
    )
    requested: set[str] = set()
    for source, terms in source_terms:
        matched_terms = {
            term for term in terms if _axis_token_matches(normalized, term)
        }
        if source == "mart" and hira_cost_total and matched_terms <= {"총액"}:
            continue
        if matched_terms:
            requested.add(source)
    if has_file_axis_reference(question) or any(
        _axis_token_matches(normalized, term)
        for term in ("총액", "sellout", "sell out", "시트", "행 수")
    ) and not hira_cost_total:
        requested.add("document")
    return frozenset(requested)


def _node_is_requested_body_fact(
    node: RenderNode,
    *,
    primary_prefixes: Sequence[str],
    requested_sources: frozenset[str],
    question: str,
) -> bool:
    if node.block_id == "patent:news":
        normalized = " ".join(question.casefold().split())
        return any(term in normalized for term in ("뉴스", "보도", "웹"))
    return bool(
        node.block_id.startswith(tuple(primary_prefixes))
        or _node_source(node.block_id) in requested_sources
    )


def _record_counts_by_source(
    nodes: Sequence[tuple[RenderNode, str]],
) -> dict[str, int]:
    ids_by_source: dict[str, set[str]] = {}
    for node, _text in nodes:
        ids_by_source.setdefault(_node_source(node.block_id), set()).update(node.record_ids)
    return {
        source: len(record_ids)
        for source, record_ids in sorted(ids_by_source.items())
        if record_ids
    }


def _question_axis(question: str) -> tuple[str, tuple[str, ...], str | None]:
    normalized = " ".join(question.casefold().split())
    # _AXIS_RULES is first-match and the document rule sits last, so a question
    # that points at an uploaded file ("이 리포트에 나온 … 점유율") matched the
    # market rule first and the body was laid out on an axis the user never
    # asked for. Worse, this function receives the question *augmented* with the
    # planner's measure terms, so a planner-emitted "매출" could win the axis on
    # a question that contains no such word - the "요청하신 매출은 …" core answer
    # reported in R68 2-F.
    #
    # An explicit file reference is the user naming the axis themselves, so it
    # outranks the token table. A file/market comparison is excluded: that
    # request wants both legs, and the existing rules already order it.
    if has_file_axis_reference(question) and not has_explicit_file_source_comparison(question):
        return "document", ("document:",), "document"
    for axis, tokens, prefixes, source in _AXIS_RULES:
        if any(_axis_token_matches(normalized, token) for token in tokens):
            return axis, prefixes, source
    return "unknown", (), None


def _axis_token_matches(normalized: str, token: str) -> bool:
    if token.isascii() and any(character.isalnum() for character in token):
        escaped = re.escape(token).replace(r"\ ", r"\s+")
        if token == "trial":
            escaped = "trials?"
        return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", normalized) is not None
    return token in normalized


def _align_commentary_to_axis(
    blocks: Sequence[str],
    axis: str,
    *,
    primary_nodes: Sequence[tuple[RenderNode, str]],
    question: str = "",
) -> list[str]:
    if axis == "patient_statistics":
        aligned: list[str] = []
        topical_references: list[str] = []
        for block in blocks:
            if block.startswith("## 참고:"):
                if _reference_matches_question(block, question):
                    topical_references.append(block)
                continue
            preamble, sections = _markdown_sections(block)
            if not sections:
                if not any(term in preamble for term in _PATIENT_UNRELATED_TERMS):
                    aligned.append(block)
                continue
            retained_sections: list[tuple[str, str]] = []
            for heading, body in sections:
                paragraphs = [
                    paragraph.strip()
                    for paragraph in re.split(r"\n\s*\n", body)
                    if paragraph.strip()
                    and not any(term in paragraph for term in _PATIENT_UNRELATED_TERMS)
                ]
                if paragraphs:
                    retained_sections.append((heading, "\n\n".join(paragraphs)))
            aligned.extend(_render_sections(retained_sections))
        return [*_filter_commentary_blocks_for_axis(aligned, axis), *topical_references]
    if axis == "reimbursement":
        aligned = _align_reimbursement_commentary(blocks, primary_nodes=primary_nodes)
        return _filter_commentary_blocks_for_axis(aligned, axis)
    if axis == "document":
        return _align_document_commentary(blocks)
    return _filter_commentary_blocks_for_axis(list(blocks), axis)


def align_core_answer_to_question(
    answer: str,
    question: str,
) -> tuple[str, dict[str, object]]:
    axis, _prefixes, _source = _question_axis(question)
    trace: dict[str, object] = {
        "axis": axis,
        "applied": False,
        "removed_sentence_count": 0,
    }
    if axis == "unknown":
        return answer, trace

    lines = answer.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"##\s+(?:핵심 답|핵심 요약)\s*", line.strip())
        ),
        None,
    )
    if start is None:
        return answer, trace
    section_end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.match(r"^##\s+", lines[index].strip())
        ),
        len(lines),
    )
    prose_end = next(
        (
            index
            for index in range(start + 1, section_end)
            if re.match(r"^#{3,6}\s+", lines[index].strip())
        ),
        section_end,
    )
    prose = " ".join(line.strip() for line in lines[start + 1 : prose_end] if line.strip())
    filtered = _filter_axis_body(prose, axis)
    if not prose or not filtered:
        return answer, trace

    before = tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", prose)
        if sentence.strip()
    )
    after = tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", filtered)
        if sentence.strip()
    )
    updated = [
        *lines[: start + 1],
        filtered,
        "",
        *lines[prose_end:],
    ]
    trace.update(
        {
            "applied": filtered != prose,
            "before_sentence_count": len(before),
            "after_sentence_count": len(after),
            "removed_sentence_count": max(0, len(before) - len(after)),
        }
    )
    return "\n".join(updated).strip(), trace


def _align_document_commentary(blocks: Sequence[str]) -> list[str]:
    aligned: list[str] = []
    deferred: list[str] = []
    for block in blocks:
        if not block.startswith("## 핵심 답\n"):
            aligned.append(block)
            continue
        body = block.removeprefix("## 핵심 답\n")
        kept: list[str] = []
        for paragraph in re.split(r"\n\s*\n", body):
            sentences = [
                sentence.strip()
                for sentence in re.split(r"(?<=[.!?])\s+", paragraph.strip())
                if sentence.strip()
            ]
            kept.extend(sentence for sentence in sentences if _axis_text_allowed(sentence, "document"))
            deferred.extend(
                sentence for sentence in sentences if not _axis_text_allowed(sentence, "document")
            )
        if kept:
            aligned.append("## 핵심 답\n" + " ".join(kept))
    if deferred:
        aligned.append("## 종합 인사이트\n" + " ".join(deferred))
    return aligned


_REFERENCE_TOPIC_STOPWORDS = frozenset(
    {
        "알려줘",
        "알려주세요",
        "환자수",
        "환자",
        "통계",
        "결과",
        "관련",
        "참고",
        "시장",
        "동향",
        "치료제",
    }
)


def _reference_matches_question(block: str, question: str) -> bool:
    question_tokens = {
        token.casefold()
        for token in re.findall(r"[0-9A-Za-z가-힣]+", question)
        if len(token) >= 2 and token.casefold() not in _REFERENCE_TOPIC_STOPWORDS
    }
    if not question_tokens:
        return False
    normalized_block = block.casefold()
    return any(token in normalized_block for token in question_tokens)


def _align_reimbursement_commentary(
    blocks: Sequence[str],
    *,
    primary_nodes: Sequence[tuple[RenderNode, str]],
) -> list[str]:
    aligned: list[str] = []
    deferred_market: list[str] = []
    deferred_notices: list[str] = []
    allowed_notices = _notice_numbers(text for _node, text in primary_nodes)
    for block in blocks:
        if block.startswith("## 핵심 답\n"):
            body = block.removeprefix("## 핵심 답\n")
            retained: list[str] = []
            for paragraph in re.split(r"\n\s*\n", body):
                kept_sentences: list[str] = []
                for sentence in re.split(r"(?<=[.!?])\s+", paragraph.strip()):
                    if any(
                        marker in sentence
                        for marker in (
                            "[출처: 내부 데이터마트]",
                            "[출처: 시장 데이터베이스]",
                        )
                    ):
                        deferred_market.append(sentence.strip())
                        continue
                    sentence_notices = _notice_numbers((sentence,))
                    if sentence_notices and not sentence_notices <= allowed_notices:
                        deferred_notices.append(sentence.strip())
                    elif sentence.strip():
                        kept_sentences.append(sentence.strip())
                if kept_sentences:
                    retained.append(" ".join(kept_sentences))
            if retained:
                aligned.append("## 핵심 답\n" + "\n\n".join(retained))
            continue
        if not block.startswith("## 종합 인사이트\n"):
            aligned.append(block)
            continue
        body = block.removeprefix("## 종합 인사이트\n")
        if any(
            token in body
            for token in ("급여", "고시", "투여", "인정", "일반원칙", "제외기준")
        ):
            aligned.append(block)
        else:
            aligned.append("## 참고: 인접 연구\n" + body)
    if deferred_market:
        aligned.append("## 참고: 인접 연구\n" + "\n\n".join(deferred_market))
    if deferred_notices:
        aligned.append("## 참고: 관련 고시\n" + "\n\n".join(deferred_notices))
    return aligned


def _filter_commentary_blocks_for_axis(blocks: Sequence[str], axis: str) -> list[str]:
    if axis not in _AXIS_FACT_TERMS:
        return list(blocks)
    filtered: list[str] = []
    for block in blocks:
        preamble, sections = _markdown_sections(block)
        if not sections:
            body = _filter_axis_body(preamble, axis)
            if body:
                filtered.append(body)
            continue
        retained_sections = [
            (heading, body)
            for heading, raw_body in sections
            if (body := _filter_axis_body(raw_body, axis))
        ]
        filtered.extend(_render_sections(retained_sections))
    return filtered


def _filter_axis_body(body: str, axis: str) -> str:
    retained: list[str] = []
    for paragraph in re.split(r"\n\s*\n", body):
        stripped = paragraph.strip()
        if not stripped:
            continue
        if stripped.startswith("|"):
            header = stripped.splitlines()[0].casefold()
            if _axis_text_allowed(header, axis):
                retained.append(stripped)
            continue
        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", stripped)
            if sentence.strip()
        ]
        kept = [sentence for sentence in sentences if _axis_text_allowed(sentence, axis)]
        if kept:
            retained.append(" ".join(kept))
    return "\n\n".join(retained)


def _axis_text_allowed(value: str, axis: str) -> bool:
    normalized = value.casefold()
    equivalent_axes = (
        {"sales", "market_share"}
        if axis in {"sales", "market_share"}
        else {axis}
    )
    source_axes = {
        candidate
        for candidate, markers in _AXIS_SOURCE_MARKERS.items()
        if any(marker.casefold() in normalized for marker in markers)
    }
    if source_axes and source_axes.isdisjoint(equivalent_axes):
        return False
    matched_axes = {
        candidate
        for candidate, terms in _AXIS_FACT_TERMS.items()
        if any(term.casefold() in normalized for term in terms)
    }
    return not matched_axes or not matched_axes.isdisjoint(equivalent_axes)


def _notice_numbers(values: Iterable[str]) -> set[str]:
    return {
        match.group(1)
        for value in values
        for match in re.finditer(r"(?:고시\s*)?제?(\d{4}-\d+)호", value)
    }


def _node_source(block_id: str) -> str:
    if block_id == "patent:news":
        return "web"
    prefix = block_id.split(":", 1)[0]
    return {
        "market": "mart",
        "hira-statistics": "hira",
        "policy": "hira",
        "clinical": "clinicaltrials",
        "patent": "patent",
        "nedrug": "nedrug",
        "openfda": "openfda",
        "web": "web",
        "document": "document",
    }.get(prefix, prefix)


def _consolidated_coverage(nodes: Sequence[RenderNode]) -> list[str]:
    rows: list[str] = []
    for node in nodes:
        match = _COVERAGE_RE.search(node.text)
        if match is None:
            body = _markdown_sections(node.text)[1]
            detail = body[0][1] if body else node.text
            detail = "<br>".join(line.strip() for line in detail.splitlines() if line.strip())
            escaped_detail = detail.replace("|", "\\|")
            rows.append(
                f"| {_SOURCE_LABELS.get(_node_source(node.block_id), _node_source(node.block_id))} "
                f"| {escaped_detail} | - | - | - |"
            )
            continue
        rows.append(
            "| "
            + " | ".join(
                (
                    _SOURCE_LABELS.get(_node_source(node.block_id), _node_source(node.block_id)),
                    match.group("total").strip(),
                    match.group("received"),
                    match.group("unique"),
                    match.group("shown"),
                )
            )
            + " |"
        )
    if not rows:
        return []
    return [
        "## 조사 범위와 완전성\n"
        "| 자료원 | 원천 | 수신 | 중복 제거 후 | 표시 |\n"
        "| --- | ---: | ---: | ---: | ---: |\n"
        + "\n".join(rows)
    ]


def _partition_lead_commentary(blocks: Sequence[str]) -> tuple[list[str], list[str]]:
    lead: list[str] = []
    deferred: list[str] = []
    for block in blocks:
        if block.startswith("## 핵심 답\n"):
            lead.append(block)
        elif match := re.match(r"^## 핵심 답\s*:[^\n]*\n", block):
            lead.append("## 핵심 답\n" + block[match.end() :])
        elif not lead and not block.startswith("## "):
            lead.append(f"## 핵심 답\n{block}")
        else:
            deferred.append(block)
    return lead, deferred


def _merge_primary_into_core(
    lead_blocks: Sequence[str],
    primary_blocks: Sequence[str],
) -> list[str]:
    bodies: list[str] = []
    for block in lead_blocks:
        body = (
            block.removeprefix("## 핵심 답\n").strip()
            if block.startswith("## 핵심 답\n")
            else block.strip()
        )
        if body:
            bodies.append(body)
    primary = [
        _demote_section_headings(block.strip())
        for block in primary_blocks
        if block.strip()
    ]
    merged = [*bodies, *primary]
    return ["## 핵심 답\n" + "\n\n".join(merged)] if merged else []


def _demote_section_headings(value: str) -> str:
    return re.sub(r"(?m)^##\s+", "### ", value)


def _primary_absence_block(
    axis: str,
    primary_source: str | None,
    primary_nodes: Sequence[tuple[RenderNode, str]],
    bindings: Sequence[Mapping[str, Any]],
    *,
    question: str,
) -> tuple[str | None, str | None]:
    if primary_nodes or primary_source is None:
        return None, None
    matching = next(
        (
            binding
            for binding in bindings
            if str(binding.get("tool") or "") == primary_source
        ),
        None,
    )
    reason_code = str(matching.get("reason_code") or "") if matching else ""
    reason = _public_absence_reason(reason_code)
    label = _AXIS_LABELS.get(axis, "요청하신 정보")
    source_label = _SOURCE_LABELS.get(primary_source, primary_source)
    normalized_reason = reason_code.casefold()
    if "timeout" in normalized_reason:
        sentence = (
            f"요청하신 {label}{_topic_particle(label)} 이번 조회에서 {source_label}이 "
            "응답 시간 초과로 종료되어 확인하지 못했습니다."
        )
    elif normalized_reason == "empty_result":
        sentence = (
            f"요청하신 {label}{_topic_particle(label)} {source_label} 조회는 완료됐지만 "
            f"'{' '.join(question.split())}' 조건 범위에서 레코드가 반환되지 않았습니다"
            "(조회했으나 결과 0건)."
        )
    elif normalized_reason == "missing_denominator":
        sentence = (
            f"{source_label}의 분자 지표는 확인됐지만 필요한 분모가 없어 "
            f"요청하신 {label}{_topic_particle(label)} 산출하지 않았습니다."
        )
    elif normalized_reason == "period_not_covered":
        sentence = (
            f"요청 기간의 {label}{_topic_particle(label)} {source_label} 표면에 없어 "
            "인접 기간 값으로 대체하지 않았습니다."
        )
    elif normalized_reason == "disease_code_unresolved":
        sentence = (
            f"질환 코드를 확인하지 못해 요청하신 {label}{_topic_particle(label)} "
            "통계 조회를 실행하지 않았습니다."
        )
    elif normalized_reason == "disease_code_lookup_failed":
        sentence = (
            f"질환 코드 조회가 실패해 요청하신 {label}{_topic_particle(label)} "
            "통계 조회를 실행하지 않았습니다."
        )
    elif normalized_reason == "entity_unresolved":
        sentence = (
            f"질문의 대상을 {source_label} 식별자와 확정 연결하지 못해 "
            f"요청하신 {label}{_topic_particle(label)} 확정하지 않았습니다."
        )
    elif normalized_reason in {"partial_axis", "partial_fields"}:
        sentence = (
            f"{source_label}에서 일부 필드는 확인됐지만 요청 필드가 모두 확보되지 않아 "
            f"{label} 결론까지는 확정하지 않았습니다."
        )
    else:
        suffix = f"({reason})" if reason else ""
        sentence = (
            f"요청하신 {label}{_topic_particle(label)} 이번 조회에서 "
            f"확인하지 못했습니다{suffix}."
        )
    return (
        f"## 핵심 답\n{sentence}",
        reason_code or None,
    )


def _absence_type(reason_code: str | None) -> str:
    normalized = (reason_code or "").casefold()
    if "timeout" in normalized:
        return "TIMEOUT"
    return {
        "empty_result": "SUCCESS_EMPTY",
        "missing_denominator": "MISSING_DENOMINATOR",
        "period_not_covered": "PERIOD_NOT_COVERED",
        "entity_unresolved": "ENTITY_UNRESOLVED",
        "disease_code_unresolved": "ENTITY_UNRESOLVED",
        "disease_code_lookup_failed": "ENTITY_UNRESOLVED",
        "partial_axis": "PARTIAL_AXIS",
        "partial_fields": "PARTIAL_FIELDS",
    }.get(normalized, "UNKNOWN")


def _topic_particle(value: str) -> str:
    if not value:
        return "는"
    last = value[-1]
    if last.isdigit():
        return "은"
    code = ord(last)
    if 0xAC00 <= code <= 0xD7A3:
        return "은" if (code - 0xAC00) % 28 else "는"
    return "는"


def _align_patient_dimension_commentary(
    blocks: Sequence[str],
    *,
    question: str,
    axis: str,
    primary_nodes: Sequence[tuple[RenderNode, str]],
) -> tuple[list[str], tuple[str, ...], int]:
    if (
        axis != "patient_statistics"
        or not primary_nodes
        or not requested_hira_axes(question)
    ):
        return list(blocks), (), 0
    primary_text = "\n".join(text for _node, text in primary_nodes)
    has_age_dimension = any(token in primary_text for token in ("연령", "0~9세", "10~19세"))
    if not has_age_dimension:
        return list(blocks), (), 0

    retained: list[str] = []
    promoted = 0
    for block in blocks:
        if block.startswith(("## 근거와 맥락\n", "## 근거\n", "## 종합 인사이트\n")):
            continue
        without_table, removed_tables = _remove_overlapping_patient_tables(
            block,
            primary_text,
        )
        cleaned, count = _remove_patient_restatements(without_table)
        promoted += count + removed_tables
        if cleaned:
            retained.append(cleaned)
    retained.append(
        "## 종합 인사이트\n"
        "'핵심 답'의 상병코드별 성별·연령대 수치를 종합하면 환자수는 "
        "상병코드와 연령대에 따라 다릅니다. 수신되지 않은 성별·연령대 조합은 "
        "전체 경향으로 일반화하지 않았습니다."
    )
    normalized_question = " ".join(question.casefold().split())
    has_male = "| 남 |" in primary_text
    has_female = "| 여 |" in primary_text
    notices: list[str] = []
    if "성별" in normalized_question and has_male and not has_female:
        notices.append("여성 연령대별 자료는 이번 조회에서 확인하지 못했습니다.")
    return retained, tuple(notices), promoted


_PATIENT_RESTATEMENT_RE = re.compile(
    r"(?:^|\n)\s*\d{4}년\s+\S+\s+\S+\s*·\s*\S+세\s+환자수\s+"
    r"[\d,]+명으로\s+확인되었습니다\.\s*"
    r"(?:\[출처:\s*건강보험심사평가원\])?\s*(?=\n|$)"
)


def _remove_patient_restatements(block: str) -> tuple[str, int]:
    matches = tuple(_PATIENT_RESTATEMENT_RE.finditer(block))
    if len(matches) < _HOMOGENEOUS_NARRATIVE_TABLE_THRESHOLD:
        return block, 0
    cleaned = _PATIENT_RESTATEMENT_RE.sub("\n", block)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if cleaned.startswith("## ") and "\n" not in cleaned:
        cleaned = ""
    return cleaned, len(matches)


def _remove_overlapping_patient_tables(block: str, primary_text: str) -> tuple[str, int]:
    if "환자수" not in primary_text or "보험자부담금" not in primary_text:
        return block, 0
    lines = block.splitlines()
    output: list[str] = []
    removed = 0
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            output.append(lines[index])
            index += 1
            continue
        end = index
        while end < len(lines) and lines[end].lstrip().startswith("|"):
            end += 1
        header = lines[index]
        if "환자수" in header and "보험자부담금" not in header:
            removed += 1
            index = end
            notice_index = index
            while notice_index < len(lines) and not lines[notice_index].strip():
                notice_index += 1
            if notice_index < len(lines) and re.match(
                r"^전체\s+[\d,]+건.*표시",
                lines[notice_index].strip(),
            ):
                index = notice_index + 1
            continue
        output.extend(lines[index:end])
        index = end
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()
    return cleaned, removed


def _homogeneous_patient_narrative_count(
    blocks: Sequence[str],
    *,
    axis: str,
    primary_nodes: Sequence[tuple[RenderNode, str]],
) -> int:
    if axis != "patient_statistics" or not any(
        "| 연령대 |" in text or "| 성별 | 연령대 |" in text
        for _node, text in primary_nodes
    ):
        return 0
    return sum(len(_PATIENT_RESTATEMENT_RE.findall(block)) for block in blocks)


def _public_absence_reason(reason_code: str) -> str | None:
    normalized = reason_code.casefold()
    # An unmapped code returns None and the caller drops the line, so a lane that
    # reports "not_planned" would vanish rather than say so. R71-D S2 lists 미계획
    # as a state the surface must be able to speak; give it a word.
    if normalized == "not_planned":
        return "미계획"
    if normalized in {"query_not_generated", "source_not_selected", "not_executed"}:
        return "계획됨·미실행"
    if normalized == "partial_not_executed":
        return "일부 계획 질의 미실행"
    if "timeout" in normalized:
        return "응답 시간 초과"
    if normalized == "empty_result":
        return "조회했으나 결과 0건"
    if any(token in normalized for token in ("quota", "limit", "rate")):
        return "쿼터·한도 소진"
    if normalized == "query_failed":
        return "실행 실패"
    if normalized == "unknown":
        return "확인되지 않음"
    return None


def _retrieval_limit_block(
    *,
    request_notice: str | None,
    source_notice_bindings: Sequence[Mapping[str, Any]],
) -> str:
    lines = _retrieval_limit_lines(
        request_notice=request_notice,
        source_notice_bindings=source_notice_bindings,
    )
    if not lines:
        return ""
    return "## 조회 제한\n" + "\n".join(lines)


def _retrieval_limit_lines(
    *,
    request_notice: str | None,
    source_notice_bindings: Sequence[Mapping[str, Any]],
) -> list[str]:
    lines: list[str] = []
    source_reasons: dict[str, list[str]] = {}
    for binding in source_notice_bindings:
        reason_code = str(binding.get("reason_code") or "")
        reason = _public_absence_reason(reason_code)
        if not reason:
            continue
        if reason not in {"응답 시간 초과", "쿼터·한도 소진", "실행 실패"}:
            continue
        source = str(binding.get("tool") or "").strip().casefold()
        source_reasons.setdefault(source, []).append(reason)

    reason_priority = {
        "응답 시간 초과": 4,
        "쿼터·한도 소진": 3,
        "실행 실패": 3,
        "조회했으나 결과 0건": 2,
        "계획됨·미실행": 1,
        "일부 계획 질의 미실행": 1,
        "확인되지 않음": 1,
        "미계획": 0,
    }
    for source, reasons in source_reasons.items():
        label = _SOURCE_LABELS.get(source, "자료원명 미확인")
        reason = max(
            dict.fromkeys(reasons),
            key=lambda item: (
                5 if item.startswith("조회·성공·") else reason_priority[item]
            ),
        )
        lines.append(f"- {label}: {reason}.")

    lines.extend(_substantive_request_notice_lines(request_notice))
    return lines


def _substantive_request_notice_lines(request_notice: str | None) -> list[str]:
    if not request_notice:
        return []
    excluded_markers = (
        "계획 상한",
        "계획됨",
        "미실행",
        "미계획",
        "생략",
        "축소된 범위",
        "질문 해석",
    )
    substantive_markers = (
        "시간 초과",
        "쿼터",
        "조회 한도",
        "한도 소진",
        "조회에 실패",
        "응답을 해석하지 못",
    )
    selected: list[str] = []
    pending_context: str | None = None
    for raw_line in request_notice.splitlines():
        clean = raw_line.strip().removeprefix("- ").rstrip(".")
        if not clean:
            continue
        normalized = clean.casefold()
        if "자료를 확보했습니다" in normalized:
            pending_context = clean
            continue
        if any(marker in normalized for marker in excluded_markers):
            continue
        if not any(marker in normalized for marker in substantive_markers):
            continue
        if pending_context is not None:
            selected.append(f"- {pending_context}.")
            pending_context = None
        selected.append(f"- {clean}.")
    return list(dict.fromkeys(selected))


def _compact_secondary_nodes(
    nodes: Sequence[tuple[RenderNode, str]],
) -> tuple[list[str], int]:
    grouped: dict[str, list[tuple[RenderNode, str]]] = {}
    for node, visible_text in nodes:
        grouped.setdefault(_secondary_group_key(node.block_id), []).append((node, visible_text))

    lines: list[str] = []
    compacted_records = 0
    for grouped_nodes in grouped.values():
        record_ids = tuple(
            dict.fromkeys(
                record_id
                for node, _visible_text in grouped_nodes
                for record_id in node.record_ids
            )
        )
        count = len(record_ids)
        if count == 0:
            continue
        first_node, first_text = grouped_nodes[0]
        heading, representative = _summary_parts(first_text)
        label = heading or _SOURCE_LABELS.get(_node_source(first_node.block_id), "참고 자료")
        representative_text = f" · 대표: {representative}" if representative else ""
        lines.append(
            f"- {label} {count}건{representative_text} · 상세 항목은 조회 상세에서 확인할 수 있습니다."
        )
        compacted_records += count
    return (["## 참고 자료\n" + "\n".join(lines)] if lines else []), compacted_records


def _secondary_group_key(block_id: str) -> str:
    parts = block_id.split(":")
    if parts[0] == "patent" and len(parts) > 1:
        return ":".join(parts[:2])
    return parts[0]


def _summary_parts(text: str) -> tuple[str, str | None]:
    _preamble, sections = _markdown_sections(text)
    heading = sections[0][0] if sections else ""
    body = sections[0][1] if sections else text
    table_rows = [
        row
        for line in body.splitlines()
        if (row := _split_markdown_row(line)) is not None
    ]
    if len(table_rows) >= 3 and table_rows[2]:
        return heading, table_rows[2][0]
    first_line = next((line.strip("- ") for line in body.splitlines() if line.strip()), "")
    return heading, first_line or None


def _limit_markdown_table_rows(
    text: str,
    *,
    row_limit: int,
    preferred_periods: Sequence[str] = (),
) -> tuple[str, int]:
    lines = text.splitlines()
    table_indexes = [
        index for index, line in enumerate(lines) if _split_markdown_row(line) is not None
    ]
    if len(table_indexes) <= row_limit + 2:
        return text, 0
    first = table_indexes[0]
    contiguous: list[int] = []
    for index in table_indexes:
        if not contiguous or index == contiguous[-1] + 1:
            contiguous.append(index)
        elif index > first:
            break
    data_indexes = contiguous[2:]
    if len(data_indexes) <= row_limit:
        return text, 0
    hidden = len(data_indexes) - row_limit
    header = _split_markdown_row(lines[contiguous[0]]) or ()
    period_column = next(
        (index for index, value in enumerate(header) if value == "기간"),
        None,
    )

    def matches_preferred_period(index: int) -> bool:
        row = _split_markdown_row(lines[index]) or ()
        return bool(
            period_column is not None
            and period_column < len(row)
            and any(row[period_column].startswith(period) for period in preferred_periods)
        )

    preferred = [index for index in data_indexes if matches_preferred_period(index)]
    preferred_set = set(preferred)
    remaining = [index for index in data_indexes if index not in preferred_set]
    selected = set((*preferred, *remaining)[:row_limit])
    table_lines = [
        *lines[first : first + 2],
        *(lines[index] for index in data_indexes if index in selected),
        f"전체 {len(data_indexes)}건 중 {row_limit}건 표시 · 나머지는 조회 상세에서 확인",
    ]
    output = [*lines[:first], *table_lines, *lines[contiguous[-1] + 1 :]]
    return "\n".join(output), hidden


def _requested_years(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"(?<!\d)(20\d{2})\s*년?", question)))


def _question_driven_blocks(
    preamble: str,
    sections: Sequence[tuple[str, str]],
    *,
    fallback: bool,
    fallback_text: str,
) -> list[str]:
    if fallback:
        return [fallback_text]
    core: list[tuple[str, str]] = []
    headed: list[tuple[str, str]] = []
    unheaded: list[str] = []
    for heading, body in sections:
        if _is_source_axis_heading(heading):
            if body.strip() and body.strip() not in unheaded:
                unheaded.append(body.strip())
            continue
        if heading in _CORE_HEADINGS:
            core.append(("핵심 답", body))
        else:
            headed.append((heading, body))
    blocks: list[str] = []
    if preamble.strip():
        blocks.append(preamble.strip())
    blocks.extend(_render_sections(core))
    blocks.extend(_render_sections(headed))
    blocks.extend(unheaded)
    if not blocks and fallback_text.strip():
        blocks.append(fallback_text.strip())
    return blocks


def _is_source_axis_heading(heading: str) -> bool:
    normalized = " ".join(heading.split()).casefold()
    if not normalized.endswith((" 요약", " 보조 자료")):
        return False
    return any(
        token in normalized
        for token in (
            "fda",
            "웹 뉴스",
            "임상시험",
            "clinicaltrials",
            "건강보험심사평가원",
            "hira",
            "국내 특허",
            "특허·분쟁",
            "식품의약품안전처",
        )
    )


def _omit_fully_unprovided_columns(text: str) -> tuple[str, tuple[str, ...]]:
    lines = text.splitlines()
    output: list[str] = []
    omitted: list[str] = []
    index = 0
    while index < len(lines):
        header = _split_markdown_row(lines[index])
        separator = (
            _split_markdown_row(lines[index + 1])
            if index + 1 < len(lines)
            else None
        )
        if (
            header is None
            or separator is None
            or len(header) != len(separator)
            or not all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in separator)
        ):
            output.append(lines[index])
            index += 1
            continue

        data_rows: list[list[str]] = []
        cursor = index + 2
        while cursor < len(lines):
            row = _split_markdown_row(lines[cursor])
            if row is None or len(row) != len(header):
                break
            data_rows.append(row)
            cursor += 1

        retained_unprovided_headers = (
            _PATENT_RETAIN_WHEN_UNPROVIDED_HEADERS
            if "특허번호" in {cell.strip() for cell in header}
            else frozenset()
        )

        omitted_indexes = tuple(
            column_index
            for column_index in range(len(header))
            if data_rows
            and header[column_index].strip() not in retained_unprovided_headers
            and all(
                row[column_index].strip() == _UNPROVIDED_CELL
                for row in data_rows
            )
        )
        if omitted_indexes:
            omitted.extend(
                header[column_index].strip()
                for column_index in omitted_indexes
            )
            retained_indexes = [
                column_index
                for column_index in range(len(header))
                if column_index not in omitted_indexes
            ]
            if retained_indexes:
                output.extend(
                    _join_markdown_row(row, retained_indexes)
                    for row in (header, separator, *data_rows)
                )
        else:
            output.extend(lines[index:cursor])
        index = cursor

    visible = "\n".join(output).strip()
    _, sections = _markdown_sections(visible)
    if sections and all(not body for _, body in sections):
        visible = ""
    return visible, tuple(omitted)


def _split_markdown_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]


def _join_markdown_row(row: Sequence[str], indexes: Sequence[int]) -> str:
    return "| " + " | ".join(row[index] for index in indexes) + " |"


def _markdown_sections(value: str) -> tuple[str, list[tuple[str, str]]]:
    text = value.strip()
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return text, []
    preamble = text[: matches[0].start()].strip()
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[match.end() : end].strip()))
    return preamble, sections


def _strip_model_source_sections(value: str) -> tuple[str, int]:
    preamble, sections = _markdown_sections(value)
    ignored = sum(
        1
        for heading, body in sections
        if heading == "출처"
        for line in body.splitlines()
        if line.strip()
    )
    retained = tuple((heading, body) for heading, body in sections if heading != "출처")
    blocks = ([preamble] if preamble else []) + _render_sections(retained)
    return "\n\n".join(block for block in blocks if block.strip()), ignored


_BOLD_HEADING_RE = re.compile(r"^\s*\*\*[^*\n]+\*\*\s*$")
_KNOWN_BOLD_HEADINGS = frozenset(
    {
        "핵심 답",
        "핵심 요약",
        "근거와 맥락",
        "근거",
        "종합 인사이트",
        "인사이트",
        "참고: 인접 연구",
    }
)


def _normalize_known_bold_headings(value: str) -> str:
    output: list[str] = []
    for line in value.splitlines():
        match = re.fullmatch(r"\s*\*\*([^*\n]+)\*\*\s*", line)
        heading = match.group(1).strip() if match else ""
        output.append(f"## {heading}" if heading in _KNOWN_BOLD_HEADINGS else line)
    return "\n".join(output)


def _drop_empty_bold_headings(value: str) -> str:
    lines = value.splitlines()
    output: list[str] = []
    for index, line in enumerate(lines):
        if not _BOLD_HEADING_RE.fullmatch(line):
            output.append(line)
            continue
        next_visible = next(
            (candidate for candidate in lines[index + 1 :] if candidate.strip()),
            "",
        )
        if not next_visible or _BOLD_HEADING_RE.fullmatch(next_visible) or next_visible.lstrip().startswith("#"):
            continue
        output.append(line)
    return "\n".join(output)


def _render_sections(sections: Sequence[tuple[str, str]]) -> list[str]:
    merged: list[tuple[str, list[str]]] = []
    by_heading: dict[str, list[str]] = {}
    for heading, body in sections:
        if not body.strip():
            continue
        bodies = by_heading.get(heading)
        if bodies is None:
            bodies = []
            by_heading[heading] = bodies
            merged.append((heading, bodies))
        if body.strip() not in bodies:
            bodies.append(body.strip())
    return [f"## {heading}\n" + "\n\n".join(bodies) for heading, bodies in merged]


_CITATION_ONLY_RE = re.compile(r"(?:\[[^\n\]]+\]\s*)+")


def _deduplicate_sentences(text: str) -> tuple[str, int]:
    seen: set[str] = set()
    removed = 0
    output: list[str] = []
    sentence_re = re.compile(
        r".+?[.!?](?:\s*\[[^\n]+\])?(?=\s|$)",
    )
    for line in text.splitlines():
        if line.lstrip().startswith(("#", "|")):
            output.append(line)
            continue
        cursor = 0
        parts: list[str] = []
        for match in sentence_re.finditer(line):
            parts.append(line[cursor : match.start()])
            sentence = match.group(0).strip()
            sentence_without_source = re.sub(
                r"\s*\[[^\n]+\]\s*$",
                "",
                sentence,
            )
            key = re.sub(r"\s+", " ", sentence_without_source).casefold()
            if key in seen:
                removed += 1
            else:
                seen.add(key)
                parts.append(match.group(0))
            cursor = match.end()
        parts.append(line[cursor:])
        collapsed = "".join(parts).strip()
        # Dropping a duplicate sentence can leave its citation stranded on a line
        # of its own. A bare citation carries no fact, so it is noise rather than
        # evidence; the record it pointed at is still cited by the surviving copy.
        if collapsed and _CITATION_ONLY_RE.fullmatch(collapsed):
            continue
        output.append(collapsed)
    return "\n".join(output).strip(), removed


def _repair_numeric_separators(text: str) -> tuple[str, int]:
    repaired, thousands = re.subn(
        r"(?<=\d)\.\s+(?=\d{3}\s*(?:만|천|백)?\s*(?:Rx|건|명|원|억원))",
        ",",
        text,
    )
    repaired, decimals = re.subn(r"(?<=\d)\.\s+(?=\d{3}\s*%)", ".", repaired)
    return repaired, thousands + decimals


def _has_visible_node_content(node: RenderNode) -> bool:
    text = node.text.strip()
    if not text:
        return False
    _, sections = _markdown_sections(text)
    if sections and all(not body for _, body in sections):
        return False
    return not (
        not node.record_ids
        and re.search(r"(?m)^\|\s*조회 결과 없음\s*\|", text) is not None
    )


def _merged_source_block(
    rendered: DeterministicRender,
    source_bodies: Sequence[str],
    *,
    allowed_sources: frozenset[str] | None = None,
) -> str:
    del source_bodies
    lines: list[str] = []
    seen_urls: set[str] = set()
    refs_by_source: dict[str, list[Any]] = {}
    for ref in rendered.source_refs:
        if allowed_sources is not None and ref.source not in allowed_sources:
            continue
        if not is_public_source_url(ref.url) or ref.url in seen_urls:
            continue
        seen_urls.add(ref.url)
        source = ref.source or ref.url
        refs_by_source.setdefault(source, []).append(ref)
    for source, refs in refs_by_source.items():
        first = refs[0]
        label = _SOURCE_LABELS.get(source, first.title or "원문")
        extra = f" · 외 {len(refs) - 1}건" if len(refs) > 1 else ""
        lines.append(f"- [{label}]({first.url}){extra}")
    return "" if not lines else "## 출처\n" + "\n".join(lines)


__all__ = [
    "build_evidence_sets",
    "build_lossless_render",
    "compose_lossless_answer",
    "compose_streaming_body",
    "configured_lossless_mode",
    "configured_request_satisfaction_mode",
    "configured_requested_fields_mode",
    "deterministic_fact_text",
    "ensure_s9_insight_surface",
    "render_deterministic_facts",
    "visible_s9_sources",
]
