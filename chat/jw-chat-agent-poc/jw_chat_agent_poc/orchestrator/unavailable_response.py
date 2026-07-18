from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from jw_chat_agent_poc.orchestrator.provenance_labels import sanitize_internal_provenance_labels
from jw_chat_agent_poc.orchestrator.tool_use_contract import (
    missing_tool_use_requirements,
    tool_call_status,
    tool_use_evidence_complete,
)

_RETIRED_CAUSE_CACHE = "cache" + "_cause"
_INTERNAL_DIAGNOSTIC_RE = re.compile(
    rf"(?:{re.escape(_RETIRED_CAUSE_CACHE)}|CausePayloadKey|market_id\s*=|response_json|Traceback|LookupError|TypeError|KeyError|SELECT\s+|FROM\s+)",
    re.IGNORECASE,
)
_UNAVAILABLE_SIGNAL_RE = re.compile(
    r"(?:데이터\s*미보유|미보유|미지원|지원\s*범위\s*밖|확인\s*불가|확인되지|확정\s*경로를\s*찾지\s*못|"
    r"표시할\s*검증\s*fact가\s*제한|현재\s*데이터로\s*답변\s*불가|데이터\s*없음|데이터\s*가\s*없)",
)
_QUESTION_UNAVAILABLE_RE = re.compile(
    r"(?:datamonitor|cortellis|kol|nccn|가이드라인|치료\s*지침|전문가|자문|글로벌\s*시장\s*전망|"
    r"파이프라인|임상\s*파이프라인)",
    re.IGNORECASE,
)
_DENOMINATOR_NOTE_RE = re.compile(
    r"참고:\s*(?:strategy|ml)_\d+\s+기준\s+순위는\s+\d+(?:/\d+)?/\d+으로\s+표시될\s+수\s+있음",
    re.IGNORECASE,
)
_INTENTIONAL_MARKET_CONTEXT_RE = re.compile(
    r"(?:데이터\s*상세|시장:)\s*.*\b(?:strategy|ml|competitive)_\d+\b.*"
    r"(?:market_landscape|competitive_dynamics|분모|Class\s*구분\s*존재)",
    re.IGNORECASE,
)
_FIVE_STEP_MARKERS = (
    "1. 미보유 데이터",
    "2. 현재 가능한 proxy",
    "3. 해석 가능한 상한선",
    "4. 확인 필요 데이터",
    "5. 확보 시 수행할 분석",
)
_SOURCE_HEADING_RE = re.compile(r"\n##\s*(?:출처|처리\s*시간)\b")
_GENERIC_UNAVAILABLE = "요청한 일부 지표는 현재 운영 데이터에서 확정 경로를 찾지 못했습니다."
_FORECAST_TOKENS = ("전망", "forecast", "예측", "향후")
_ERROR_STATUSES = frozenset({"error", "query_failed", "mapping_failed", "timeout", "failed"})
_ABSENT_STATUSES = frozenset({"no_data", "unsupported", "missing", "not_found", "incomplete_split"})


def file_absence_answer(status: str, *, subject: str = "", period: str = "") -> str:
    """Render file-query absence with the shared public absence status vocabulary."""

    if status not in _ABSENT_STATUSES:
        raise ValueError(f"unsupported absence status: {status}")
    if status == "unsupported" and subject:
        return f"이 파일에는 {subject} 관련 열이 없습니다. 파일의 열 이름을 확인해 주세요."
    if status in {"missing", "no_data", "not_found"} and period:
        return f"요청하신 기간({period})의 데이터가 없습니다. 파일에 있는 기간을 지정해 주세요."
    return "요청하신 항목은 이 파일에서 확인되지 않습니다. 파일의 열과 기간을 확인해 주세요."


@dataclass(frozen=True, slots=True)
class UnavailablePlan:
    missing: str
    proxy: str
    ceiling: str
    needed: str
    analysis: str


_PLANS: tuple[tuple[tuple[str, ...], UnavailablePlan], ...] = (
    (
        ("datamonitor", "글로벌"),
        UnavailablePlan(
            missing="글로벌 시장 전망, 외부 리서치 forecast, 장기 성장률 원천 데이터입니다.",
            proxy="보유된 UBIST/IQVIA 매출·시장점유율·순위 fact가 있으면 국내 관찰 기간의 방향성만 proxy로 사용합니다.",
            ceiling="국내 처방/매출 관찰 범위의 최근 흐름만 말할 수 있고 글로벌 시장 전망이나 성장률을 추정하지 않습니다.",
            needed="Datamonitor 등 글로벌 시장 전망, 국가별 매출, forecast 기간, 가정·방법론 필드가 필요합니다.",
            analysis="확보 시 국가·기간별 CAGR과 국내 UBIST/IQVIA 추이를 나란히 비교해 전망과 실제 처방 흐름의 차이를 분석합니다.",
        ),
    ),
    (
        _FORECAST_TOKENS,
        UnavailablePlan(
            missing="예측 모델, forecast 시계열, 미래 기간에 대한 확정 전망 데이터입니다.",
            proxy="보유된 UBIST/IQVIA 과거 실적 추세가 있으면 참고용 proxy로만 사용합니다. 과거 실적 추세는 예측이 아닙니다.",
            ceiling="과거 추세는 미래 매출·시장 규모를 보장하지 않으며 forecast 값처럼 제시하지 않습니다.",
            needed="forecast 시계열, 예측 기간, 모델 가정, 약가·경쟁 출시·정책 변화 변수 필드가 필요합니다.",
            analysis="확보 시 과거 실적과 외생 변수를 분리하고 시계열 예측 모델을 적용해 예측값·신뢰구간·가정 민감도를 제시합니다.",
        ),
    ),
    (
        ("kol", "전문가", "자문", "의견"),
        UnavailablePlan(
            missing="KOL 자문, 전문가 인터뷰, 처방 의사 의견 원천 데이터입니다.",
            proxy="보유된 UBIST/IQVIA 매출·MS·채널 fact와 실제 뉴스 fact가 있으면 외부 관찰 proxy로만 사용합니다.",
            ceiling="proxy는 시장 반응이나 보도 맥락의 관찰치일 뿐 전문가 판단이나 처방 의도를 대체하지 않습니다.",
            needed="KOL 발언, 인터뷰 일자, 전문 분야, 병원/채널, 주요 메시지와 근거 수준 필드가 필요합니다.",
            analysis="확보 시 의견 주제별로 기회·위협을 분류하고 동기간 매출·MS 변화와 함께 검토합니다.",
        ),
    ),
    (
        ("nccn", "가이드라인", "치료 지침", "guideline"),
        UnavailablePlan(
            missing="NCCN/학회 가이드라인, 치료 지침 개정 내용과 권고 수준 원천 데이터입니다.",
            proxy="보유된 HIRA 환자수 또는 UBIST/IQVIA 처방·매출 fact가 있으면 질환 규모와 처방 성과를 보조 proxy로 사용합니다.",
            ceiling="proxy만으로 가이드라인 권고 변화, 치료 표준 변경, 처방 적합성을 단정하지 않습니다.",
            needed="가이드라인 명칭, 개정일, 권고 등급, 대상 환자군, 비교 치료 옵션 필드가 필요합니다.",
            analysis="확보 시 권고 변화 전후 환자수·처방·매출 흐름을 대조해 시장 영향 가능성을 검토합니다.",
        ),
    ),
    (
        ("cortellis", "파이프라인", "임상", "pipeline"),
        UnavailablePlan(
            missing="Cortellis/파이프라인, 경쟁 임상 단계, 예상 출시일과 적응증 원천 데이터입니다.",
            proxy="보유된 특허/독점권 fact, 뉴스 fact, UBIST/IQVIA 경쟁 브랜드 지표가 있으면 경쟁 환경 proxy로만 사용합니다.",
            ceiling="proxy는 현재 확인된 특허·뉴스·처방 성과만 보여주며 임상 성공률이나 출시 가능성을 추정하지 않습니다.",
            needed="성분·제품명, 임상 단계, 적응증, 스폰서, 주요 결과, 예상 허가·출시일 필드가 필요합니다.",
            analysis="확보 시 임상 단계별 경쟁 위협도를 나누고 특허·매출·MS 변화와 함께 출시 전후 시나리오를 분석합니다.",
        ),
    ),
    (
        ("hira", "환자수", "환자 수", "patient"),
        UnavailablePlan(
            missing="HIRA 환자수, 진료 형태별 환자 규모, 질환 코드 기준 원천 데이터입니다.",
            proxy="보유된 UBIST/IQVIA 매출·MS·순위 fact가 있으면 처방 성과의 방향성만 proxy로 사용합니다.",
            ceiling="매출·MS proxy로 환자수, 진단 규모, 치료율을 역산하거나 단정하지 않습니다.",
            needed="질환 코드, 연도·월, 외래/입원, 성·연령, 환자수와 청구 건수 필드가 필요합니다.",
            analysis="확보 시 환자수 변화와 브랜드 매출·MS를 같은 기간에 맞춰 침투율과 성장 여지를 비교합니다.",
        ),
    ),
    (
        ("channel", "채널", "segment", "세그먼트", "class", "molecule", "용량", "제형", "weekly", "monthly"),
        UnavailablePlan(
            missing="요청 축의 세그먼트 원천 행 또는 필요한 grain(주간/월간/축별) 데이터입니다.",
            proxy="반환된 UBIST/IQVIA 월별·축별 fact가 있으면 지원되는 축의 방향성만 proxy로 사용합니다.",
            ceiling="지원된 축 밖의 Class/Molecule/브랜드/용량/제형·주간 변화를 추정하지 않습니다.",
            needed="기간 grain, Class, Molecule, 브랜드, 용량, 제형, 채널별 매출·MS 원천 행이 필요합니다.",
            analysis="확보 시 축별 상위 세그먼트의 시작·최신값, 변화폭, 브랜드와 시장의 동행/괴리를 비교합니다.",
        ),
    ),
    (
        ("영업", "활동", "impact", "콜", "csd", "디테일링"),
        UnavailablePlan(
            missing="CSD 영업활동, 콜 수, impact level, 기관·의사별 활동 원천 데이터입니다.",
            proxy="보유된 UBIST/IQVIA 매출·MS·순위 시계열이 있으면 동기간 성과 변화만 proxy로 사용합니다.",
            ceiling="proxy는 매출·MS 움직임만 보여주며 영업활동 impact나 처방 인과를 증명하지 않습니다.",
            needed="콜 수, impact level, 기관·의사, 활동일, 제품·메시지, 처방 lag, 비활동 대조군 필드가 필요합니다.",
            analysis="확보 시 활동 전후 1~3개월 매출·MS를 비활동군과 비교해 uplift와 lag를 추정합니다.",
        ),
    ),
    (
        ("출처", "교차", "iqvia", "ubist"),
        UnavailablePlan(
            missing="출처 간 교차 검증에 필요한 동일 시장·동일 기간의 원천 지표입니다.",
            proxy="보유된 단일 출처 UBIST/IQVIA fact가 있으면 해당 출처 안에서만 방향성과 수준을 관찰합니다.",
            ceiling="단일 출처 proxy만으로 출처 간 일치·불일치, 원천 정의 차이, 전체 시장 보정값을 확정하지 않습니다.",
            needed="동일 기간의 UBIST/IQVIA 매출·MS, 시장 정의, 브랜드 매핑, 집계 기준 필드가 필요합니다.",
            analysis="확보 시 출처별 시장규모·브랜드 매출·MS를 같은 기준으로 정렬해 차이와 원인을 분해합니다.",
        ),
    ),
)

_DEFAULT_PLAN = UnavailablePlan(
    missing="요청 분석에 필요한 일부 원천 데이터 또는 확정 fact입니다.",
    proxy="보유된 UBIST/IQVIA 매출·시장점유율·순위, HIRA 통계, 특허, 뉴스 fact가 있으면 해당 범위만 proxy로 사용합니다.",
    ceiling="proxy는 보유 데이터 범위의 관찰값만 보여주며 미보유 원천의 값이나 원인을 생성하지 않습니다.",
    needed="요청 축의 기간, 시장 정의, 브랜드·성분 매핑, 원천 지표, 이벤트 일자와 비교군 필드가 필요합니다.",
    analysis="확보 시 동일 기간·동일 시장 기준으로 proxy와 신규 원천을 정렬해 변화폭, 원인 후보, 확인 필요 항목을 분리합니다.",
)


def apply_common_unavailable_response(
    question: str,
    answer: str,
    markdown_response: Mapping[str, object] | None,
    *,
    tool_calls: Sequence[Mapping[str, Any]] | None = None,
    source_scope: str = "MARKET",
    connected_source_mode: bool = False,
) -> str:
    """Sanitize internal diagnostics and append the common 5-step unavailable block when needed."""

    fact_md = _fact_markdown(markdown_response)
    sanitized_answer = sanitize_internal_diagnostics(answer)
    if source_scope == "FILE":
        return _cleanup(sanitized_answer)
    combined = "\n\n".join(part for part in (question, sanitized_answer, sanitize_internal_diagnostics(fact_md)) if part)
    question_has_unavailable_signal = bool(_QUESTION_UNAVAILABLE_RE.search(question))
    if connected_source_mode and _is_generic_guideline_request(question):
        question_has_unavailable_signal = False
    if not _UNAVAILABLE_SIGNAL_RE.search(combined) and not question_has_unavailable_signal:
        return _cleanup(sanitized_answer)
    gated = _four_stage_unavailable_gate(
        question,
        sanitized_answer,
        fact_md,
        tool_calls,
        question_has_unavailable_signal=question_has_unavailable_signal,
        connected_source_mode=connected_source_mode,
    )
    if gated is not None:
        return gated
    if _has_five_step_block(sanitized_answer):
        return _cleanup(sanitized_answer)
    if not question_has_unavailable_signal and _is_positioning_question(question):
        return _cleanup(sanitized_answer)
    if not question_has_unavailable_signal and not _UNAVAILABLE_SIGNAL_RE.search(sanitize_internal_diagnostics(fact_md)):
        return _cleanup(sanitized_answer)
    block = _five_step_block(_plan_for(question, sanitized_answer, fact_md))
    return _cleanup(_insert_before_source(sanitized_answer, block))


def _is_generic_guideline_request(question: str) -> bool:
    lowered = question.casefold()
    return "nccn" not in lowered and any(
        token in lowered for token in ("가이드라인", "치료 지침", "guideline")
    )


def _completed_answer_contract(question: str, answer: str, fact_md: str) -> bool:
    """Preserve deterministic contract output before unavailable-state handling."""

    from jw_chat_agent_poc.orchestrator.answer_contract import evaluate_answer_contract

    status = evaluate_answer_contract(question, answer, {"fact_md": fact_md})
    contract = status.get("intent") or status.get("structural_contract")
    return isinstance(contract, str) and bool(contract) and status.get("status") == "pass"


def _four_stage_unavailable_gate(
    question: str,
    answer: str,
    fact_md: str,
    tool_calls: Sequence[Mapping[str, Any]] | None,
    *,
    question_has_unavailable_signal: bool,
    connected_source_mode: bool,
) -> str | None:
    """Separate owned facts, source absence, and failed verification.

    Slot reuse runs before final answer generation. This final gate therefore starts
    at the current fact set and only evaluates tool evidence when the caller supplies
    the executions from this turn. Omitting tool_calls preserves legacy pure-format
    call sites.
    """

    if tool_calls is None:
        return None
    calls = tuple(tool_calls)
    if connected_source_mode:
        if _has_positive_fact(fact_md) and _has_successful_fact_call(calls):
            return _cleanup(answer)
        failed = tuple(
            _public_tool_name(call)
            for call in calls
            if _tool_status(call) in _ERROR_STATUSES
        )
        if failed:
            return _unverified_answer(f"도구 조회({', '.join(dict.fromkeys(failed))})가 실패했습니다")
        if tool_use_evidence_complete(question, calls):
            return _cleanup(answer)
        missing = missing_tool_use_requirements(question, calls)
        if missing:
            return _unverified_answer(f"필요 근거({', '.join(missing)})가 이번 턴에 완성되지 않았습니다")
    if not question_has_unavailable_signal and _has_positive_fact(fact_md) and _has_successful_fact_call(calls):
        if _completed_answer_contract(question, answer, fact_md):
            return _cleanup(answer)
        from jw_chat_agent_poc.service.answer_safety import (
            finalized_fallback_fact_answer,
            replace_internal_fact_dump,
        )

        question_fold = question.casefold()
        if "CSD aggregate 콜수" in fact_md and any(
            token in question_fold for token in ("영업", "활동", "콜", "impact", "csd")
        ):
            activity_answer = replace_internal_fact_dump(question, fact_md, {"fact_md": fact_md})
            if activity_answer != fact_md:
                return _cleanup(activity_answer)
        public_answer = replace_internal_fact_dump(question, answer, {"fact_md": fact_md})
        if public_answer != answer:
            return _cleanup(public_answer)
        return _cleanup(finalized_fallback_fact_answer(question, {"fact_md": fact_md}))

    required = _required_tools(question)
    attempted = {_public_tool_name(call) for call in calls}
    missing = tuple(tool for tool in required if tool not in attempted)
    if missing:
        return _unverified_answer(f"필요 도구({', '.join(missing)})가 이번 턴에 실행되지 않았습니다")

    failed = tuple(
        _public_tool_name(call)
        for call in calls
        if _tool_status(call) in _ERROR_STATUSES
    )
    if failed:
        return _unverified_answer(f"도구 조회({', '.join(dict.fromkeys(failed))})가 실패했습니다")

    absent = tuple(call for call in calls if _tool_status(call) in _ABSENT_STATUSES)
    if absent or question_has_unavailable_signal:
        plan = _plan_for(question, answer, fact_md)
        source_absence = f"원천에 없음: {plan.missing}"
        if _has_five_step_block(answer):
            return _cleanup("\n\n".join((source_absence, answer)))
        return _cleanup(_insert_before_source("\n\n".join((source_absence, answer)), _five_step_block(plan)))
    return None


def _required_tools(question: str) -> tuple[str, ...]:
    from jw_chat_agent_poc.orchestrator.answer_contract import CONTRACT_REQUIRED_TOOLS, evaluate_answer_contract

    status = evaluate_answer_contract(question, "", None)
    structural = status.get("structural_contract")
    if isinstance(structural, str) and structural:
        return CONTRACT_REQUIRED_TOOLS.get(structural, ())
    intent = status.get("intent")
    if isinstance(intent, str) and intent:
        return CONTRACT_REQUIRED_TOOLS.get(intent, ())
    return ()


def _public_tool_name(call: Mapping[str, Any]) -> str:
    tool = str(call.get("tool") or "")
    aliases = {
        "get_metric": "get_brand_metric",
        "get_market_scope": "market_scope",
        "get_market_landscape": "market_scope",
    }
    return aliases.get(tool, tool)


def _tool_status(call: Mapping[str, Any]) -> str:
    return tool_call_status(call)


def _has_successful_fact_call(calls: Sequence[Mapping[str, Any]]) -> bool:
    for call in calls:
        if _tool_status(call) != "ok":
            continue
        data = call.get("render_data")
        if isinstance(data, Mapping) and any(value not in (None, "", [], ()) for value in data.values()):
            return True
    return False


def _has_positive_fact(fact_md: str) -> bool:
    if not fact_md.strip():
        return False
    compact = re.sub(r"\s+", " ", fact_md).strip()
    if compact in {"데이터 미보유", "미보유", "데이터 없음"}:
        return False
    return bool(re.search(r"(?:\d[\d,.]*\s*(?:억원|%|건|명)|확정 fact|시계열 fact|지표 fact)", compact))


def _unverified_answer(reason: str) -> str:
    return f"현재 확인 불가: {reason}. 이는 원천 데이터가 없다는 뜻은 아닙니다. 조회 경로가 정상화된 뒤 다시 확인해 주세요."


def sanitize_internal_diagnostics(text: str) -> str:
    """Replace internal cache/SQL/debug details with a stable user-facing unavailable sentence."""

    if not text:
        return ""
    lines: list[str] = []
    previous_generic = False
    for raw_line in text.splitlines():
        if _INTERNAL_DIAGNOSTIC_RE.search(raw_line):
            table_line = _sanitized_table_line(raw_line)
            if table_line:
                lines.append(table_line)
                previous_generic = False
                continue
            if not previous_generic:
                prefix = _line_prefix(raw_line)
                lines.append(f"{prefix}{_GENERIC_UNAVAILABLE}" if prefix else _GENERIC_UNAVAILABLE)
                previous_generic = True
            continue
        lines.append(raw_line)
        previous_generic = False
    sanitized = "\n".join(lines)
    sanitized, protected_market_contexts = _protect_intentional_market_contexts(sanitized)
    sanitized = re.sub(r"CausePayloadKey\([^)]*\)", _GENERIC_UNAVAILABLE, sanitized)
    sanitized = re.sub(r"\bmarket_id\s*=\s*['\"]?[\w.-]+['\"]?", "시장 식별자", sanitized)
    sanitized = _restore_intentional_market_contexts(sanitized, protected_market_contexts)
    sanitized = sanitized.replace(_RETIRED_CAUSE_CACHE, "운영 데이터")
    return _cleanup(sanitize_internal_provenance_labels(sanitized))


def _protect_intentional_market_contexts(text: str) -> tuple[str, tuple[str, ...]]:
    protected: list[str] = []
    lines: list[str] = []
    for line in text.splitlines():
        if _is_intentional_market_context(line):
            protected.append(line)
            lines.append(f"__INTENTIONAL_MARKET_CONTEXT_{len(protected) - 1}__")
            continue
        lines.append(line)
    contextualized = "\n".join(lines)

    def replace(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"__INTENTIONAL_MARKET_CONTEXT_{len(protected) - 1}__"

    return _DENOMINATOR_NOTE_RE.sub(replace, contextualized), tuple(protected)


def _is_intentional_market_context(line: str) -> bool:
    return bool(_INTENTIONAL_MARKET_CONTEXT_RE.search(line))


def _restore_intentional_market_contexts(text: str, protected: tuple[str, ...]) -> str:
    restored = text
    for index, value in enumerate(protected):
        restored = restored.replace(f"__INTENTIONAL_MARKET_CONTEXT_{index}__", value)
    return restored


def _fact_markdown(markdown_response: Mapping[str, object] | None) -> str:
    if not isinstance(markdown_response, Mapping):
        return ""
    value = markdown_response.get("fact_md") or markdown_response.get("data_md") or ""
    return value if isinstance(value, str) else ""


def _has_five_step_block(answer: str) -> bool:
    return all(marker in answer for marker in _FIVE_STEP_MARKERS)


def _plan_for(question: str, answer: str, fact_md: str) -> UnavailablePlan:
    question_text = question.lower()
    for tokens, plan in _PLANS:
        if any(token.lower() in question_text for token in tokens):
            return plan
    text = " ".join((answer, fact_md)).lower()
    for tokens, plan in _PLANS:
        if tokens == _FORECAST_TOKENS:
            continue
        if _is_source_trap_plan(tokens):
            continue
        if any(token.lower() in text for token in tokens):
            return plan
    return _DEFAULT_PLAN


def _is_source_trap_plan(tokens: tuple[str, ...]) -> bool:
    return any(token in tokens for token in ("datamonitor", "cortellis", "kol", "nccn"))


def _is_positioning_question(question: str) -> bool:
    return "포지셔닝" in question


def _five_step_block(plan: UnavailablePlan) -> str:
    return "\n".join(
        (
            "### 미보유 데이터 처리",
            "| 단계 | 내용 |",
            "| --- | --- |",
            f"| 1. 미보유 데이터 | {plan.missing} |",
            f"| 2. 현재 가능한 proxy | {plan.proxy} |",
            f"| 3. 해석 가능한 상한선 | {plan.ceiling} |",
            f"| 4. 확인 필요 데이터 | {plan.needed} |",
            f"| 5. 확보 시 수행할 분석 | {plan.analysis} |",
        )
    )


def _insert_before_source(answer: str, block: str) -> str:
    match = _SOURCE_HEADING_RE.search(answer)
    if match is None:
        return "\n\n".join(part for part in (answer, block) if part.strip())
    return "\n\n".join((answer[: match.start()].strip(), block, answer[match.start() :].strip()))


def _line_prefix(line: str) -> str:
    match = re.match(r"^(\s*(?:[-*]\s+|\|\s*[^|]+\s*\|\s*)?)", line)
    if match is None:
        return ""
    prefix = match.group(1)
    return prefix if prefix.strip() else ""


def _sanitized_table_line(line: str) -> str:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return ""
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if len(cells) < 2:
        return ""
    replaced = tuple(_GENERIC_UNAVAILABLE if _INTERNAL_DIAGNOSTIC_RE.search(cell) else cell for cell in cells)
    return "| " + " | ".join(replaced) + " |"


def _cleanup(markdown: str) -> str:
    text = re.sub(r"(?m)^(#{1,6})([^\s#])", r"\1 \2", markdown.strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
