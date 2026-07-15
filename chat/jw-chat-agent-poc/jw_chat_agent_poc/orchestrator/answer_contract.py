from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Final, Mapping

from jw_chat_agent_poc.agent_loop.models import ToolCallPlan
from jw_chat_agent_poc.orchestrator.answer_completeness import (
    COMPLETENESS_INTENTS,
    completeness_intent,
    completeness_status,
    repair_completeness,
)
from jw_chat_agent_poc.orchestrator.dosage_notes import DOSAGE_COMBINATION_NOTE_PREFIX, dosage_combination_note
from jw_chat_agent_poc.orchestrator.general_view_contract import enforce_general_view_contract
from jw_chat_agent_poc.orchestrator.provenance_labels import provenance_source_block_from_facts


@dataclass(frozen=True, slots=True)
class ContractRule:
    required_facts: tuple[str, ...] = ()
    required_tables: tuple[str, ...] = ()
    required_claims: tuple[str, ...] = ()
    forbidden_outputs: tuple[str, ...] = ()
    min_rows: int = 0


ANSWER_CONTRACT: Final[dict[str, ContractRule]] = {
    "ranking": ContractRule(
        required_facts=("rank", "rank_denominator", "brand_sales", "brand_ms", "period", "source"),
        required_claims=("rank_direct_answer",),
        forbidden_outputs=("UBIST: -", "표시할 확정 fact가 없습니다"),
    ),
    "trend": ContractRule(
        required_facts=("time_series", "start_value", "end_value", "period", "source"),
        required_tables=("time_series_table",),
        required_claims=("start_end_summary", "trend_state"),
        min_rows=4,
    ),
    **{intent: ContractRule(required_facts=("deterministic_completion_fact",)) for intent in COMPLETENESS_INTENTS},
}

_MI_IMPLICATION_CONTRACTS: Final[frozenset[str]] = frozenset(
    {"segment_compare", "source_crosscheck", "positioning", "threat_detection", "news_ei"}
)

CONTRACT_REQUIRED_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    "patent_exclusivity": ("search_patent", "mfds_patent"),
    "clinical_evidence": (
        "clinicaltrials_v2_search",
        "mfds_clinical_trial_kr",
        "mfds_permission_search",
        "openfda_label_search",
    ),
    "news_ei": ("search_news",),
    "change_drivers": ("search_news", "get_brand_metric", "market_scope"),
    "sales_activity_link": ("get_brand_metric", "csd_activity_trend"),
    "source_crosscheck": ("get_brand_metric", "market_scope"),
    "segment_compare": ("get_brand_metric", "market_scope"),
    "quarter_metric": ("get_brand_metric",),
    "specialty_breakdown": ("get_brand_metric", "market_scope"),
    "positioning": ("get_brand_metric", "market_scope"),
    "threat_detection": ("search_news", "get_brand_metric", "market_scope"),
    "trend_support_matrix": ("get_brand_metric", "market_scope"),
    "ranking": ("get_brand_metric", "market_scope"),
    "trend": ("get_brand_metric",),
    "brand_compare": ("get_brand_metric",),
    "share_delta_compare": ("get_brand_metric", "market_scope"),
    "top_n_share_sum": ("get_brand_metric", "market_scope"),
    "concentration": ("get_brand_metric", "market_scope"),
    "target_share_gap": ("get_brand_metric", "market_scope"),
    "channel_provenance": ("get_brand_metric",),
}


def answer_contract_backfill_tool_calls(question: str, brand: str, calls: list[dict[str, Any]]) -> tuple[ToolCallPlan, ...]:
    """Return deterministic tool calls needed before final answer generation."""

    structural = _structural_contract_type(question)
    if structural == "sales_activity_link":
        plans: list[ToolCallPlan] = []
        if not _has_tool_attempt(calls, "get_brand_metric"):
            plans.append(
                ToolCallPlan(
                    name="get_metric",
                    arguments={"brand": brand, "measure": "sales", "period": "latest"},
                    reason="AnswerContract structural proxy backfill",
                )
            )
        if not _has_tool_attempt(calls, "csd_activity_trend"):
            plans.append(
                ToolCallPlan(
                    name="csd_activity_trend",
                    arguments={"brand": brand},
                    reason="AnswerContract CSD aggregate activity backfill",
                )
            )
        return tuple(plans)
    if structural == "change_drivers":
        if not _is_news_sales_impact_question(question):
            if _has_brand_metric_fact(calls, brand):
                return ()
            return (
                ToolCallPlan(
                    name="get_metric",
                    arguments={"brand": brand, "measure": "sales", "period": "latest"},
                    reason="AnswerContract structural proxy backfill",
                ),
            )
        plans: list[ToolCallPlan] = []
        existing = _contract_tool_names(calls)
        if "search_news" not in existing:
            plans.append(
                ToolCallPlan(
                    name="search_news",
                    arguments={"brand": brand, "query": question},
                    reason="AnswerContract change-driver news backfill",
                )
            )
        if not _has_tool_attempt(calls, "get_brand_metric"):
            plans.append(
                ToolCallPlan(
                    name="get_metric",
                    arguments={"brand": brand, "measure": "sales", "period": "latest"},
                    reason="AnswerContract structural proxy backfill",
                )
            )
        if "market_scope" not in existing:
            plans.append(
                ToolCallPlan(
                    name="get_market_scope",
                    arguments={"brand": brand, "view": "market_landscape"},
                    reason="AnswerContract change-driver market-scope backfill",
                )
            )
        return tuple(plans)
    intent = _intent(question)
    if intent == "concentration" and not _has_market_scope_fact(calls):
        return (
            ToolCallPlan(
                name="get_market_scope",
                arguments={"brand": brand, "view": "market_landscape"},
                reason="AnswerContract concentration fact backfill",
            ),
        )
    if intent in {"share_delta_compare", "top_n_share_sum"} and not _has_tool_attempt(calls, "get_brand_metric"):
        return (
            ToolCallPlan(
                name="get_metric",
                arguments={"brand": brand, "measure": "market_share", "period": "latest"},
                reason="AnswerContract share completeness fact backfill",
            ),
        )
    if intent != "ranking":
        return ()
    if _has_tool_attempt(calls, "get_brand_metric"):
        return ()
    return (
        ToolCallPlan(
            name="get_metric",
            arguments={"brand": brand, "measure": "market_share", "period": "latest"},
            reason="AnswerContract ranking fact backfill",
        ),
    )


def enforce_answer_contract(
    question: str,
    answer: str,
    markdown_response: Mapping[str, Any] | None,
    general_view_contract: Mapping[str, Any] | None = None,
) -> str:
    """Repair final-model omissions when required facts already exist."""

    fact_md = _fact_markdown(markdown_response)
    intent = _intent(question, fact_md)
    repaired = answer
    if intent is not None and fact_md:
        rule = ANSWER_CONTRACT[intent]
        if intent == "ranking":
            fact = _ranking_fact(fact_md)
            if fact is not None and not _ranking_surface_ok(answer, fact, rule):
                repaired = _join_blocks(_ranking_answer(fact), _source_block(fact_md))
        elif intent == "trend":
            fact = _trend_fact(fact_md)
            if fact is not None and len(fact.rows) >= rule.min_rows and not _trend_surface_ok(answer, fact, rule):
                repaired = _join_blocks(_trend_answer(fact), _source_block(fact_md))
        elif intent in COMPLETENESS_INTENTS:
            repaired = repair_completeness(intent, question, answer, fact_md)
    repaired = _append_general_dosage_combination_note(_enforce_structural_contract(question, repaired, fact_md))
    return enforce_general_view_contract(repaired, dict(general_view_contract) if general_view_contract else None)


def evaluate_answer_contract(question: str, answer: str, markdown_response: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the contract status for trace metadata without mutating the answer."""

    fact_md = _fact_markdown(markdown_response)
    intent = _intent(question, fact_md)
    structural = _structural_contract_type(question)
    if intent is None:
        return {
            "intent": None,
            "structural_contract": structural,
            "status": "pass" if structural and _structural_contract_present(answer, structural) else "not_applicable",
        }
    rule = ANSWER_CONTRACT[intent]
    if not fact_md:
        return {"intent": intent, "status": "missing_fact_set", "required_facts": rule.required_facts}
    if intent == "ranking":
        fact = _ranking_fact(fact_md)
        if fact is None:
            return {"intent": intent, "status": "missing_required_fact", "required_facts": rule.required_facts}
        return {
            "intent": intent,
            "structural_contract": structural,
            "status": "pass" if _ranking_surface_ok(answer, fact, rule) else "surface_missing",
            "required_facts": rule.required_facts,
            "required_claims": rule.required_claims,
            "forbidden_outputs": tuple(item for item in rule.forbidden_outputs if item in answer),
        }
    if intent == "trend":
        fact = _trend_fact(fact_md)
        if fact is None:
            return {"intent": intent, "status": "missing_required_fact", "required_facts": rule.required_facts}
        if len(fact.rows) < rule.min_rows:
            return {"intent": intent, "status": "insufficient_rows", "min_rows": rule.min_rows, "row_count": len(fact.rows)}
        return {
            "intent": intent,
            "structural_contract": structural,
            "status": "pass" if _trend_surface_ok(answer, fact, rule) else "surface_missing",
            "required_facts": rule.required_facts,
            "required_tables": rule.required_tables,
            "required_claims": rule.required_claims,
            "row_count": len(fact.rows),
        }
    if intent in COMPLETENESS_INTENTS:
        return {
            "intent": intent,
            "structural_contract": structural,
            "status": completeness_status(intent, question, answer, fact_md),
            "required_facts": rule.required_facts,
            "derived": intent != "channel_provenance",
        }
    return {"intent": intent, "structural_contract": structural, "status": "not_evaluated"}


@dataclass(frozen=True, slots=True)
class RankingFact:
    brand: str
    period: str
    sales: str
    share: str
    rank: str


@dataclass(frozen=True, slots=True)
class TrendRow:
    period: str
    sales: str
    share: str


@dataclass(frozen=True, slots=True)
class TrendFact:
    brand: str
    rows: tuple[TrendRow, ...]


@dataclass(frozen=True, slots=True)
class NewsFactor:
    category: str
    factor: str
    title: str
    source: str
    url: str
    date: str
    summary: str
    direction: str


@dataclass(frozen=True, slots=True)
class NewsGrade:
    row: NewsFactor
    grade: str
    handling: str


@dataclass(frozen=True, slots=True)
class MiImplicationRow:
    observation: str
    implication: str
    next_data: str


@dataclass(frozen=True, slots=True)
class PatentFactRow:
    source: str
    product: str
    patent_no: str
    status: str
    expiry: str
    owner: str


@dataclass(frozen=True, slots=True)
class ClinicalEvidenceRow:
    category: str
    content: str
    source: str


def _intent(question: str, fact_md: str = "") -> str | None:
    completeness = completeness_intent(question, fact_md)
    if completeness is not None:
        return completeness
    if _ranking_question(question):
        return "ranking"
    if _trend_question(question):
        return "trend"
    return None


def _ranking_question(question: str) -> bool:
    if any(token in question for token in ("채널", "경쟁", "구도", "상위", "비교", "아토젯")):
        return False
    return "순위" in question or ("점유율" in question and any(token in question for token in ("몇 위", "몇위", "위야", "위?", "랭킹")))


def _trend_question(question: str) -> bool:
    if any(token in question for token in ("경쟁", "구도", "상위", "비교", "아토젯")):
        return False
    return "매출" in question and any(token in question for token in ("추이", "변화", "증감", "하락", "감소", "줄"))


def _fact_markdown(markdown_response: Mapping[str, Any] | None) -> str:
    if not isinstance(markdown_response, Mapping):
        return ""
    parts: list[str] = []
    for key in ("fact_md", "data_md"):
        value = markdown_response.get(key)
        if isinstance(value, str) and value.strip() and value not in parts:
            parts.append(value)
    return "\n\n".join(parts)


def _has_ranking_fact(calls: list[dict[str, Any]], brand: str) -> bool:
    return _has_brand_metric_fact(calls, brand)


def _has_tool_attempt(calls: list[dict[str, Any]], tool: str) -> bool:
    return tool in _contract_tool_names(calls)


def _has_brand_metric_fact(calls: list[dict[str, Any]], brand: str) -> bool:
    for call in calls:
        if call.get("tool") != "get_brand_metric":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        if data.get("brand") != brand:
            continue
        if data.get("status") in {"error", "query_failed", "mapping_failed", "missing", "incomplete_split"}:
            continue
        if data.get("rank") is not None and (data.get("sales_krw") is not None or data.get("sales_억원") is not None):
            return True
        if data.get("sales_krw") is not None or data.get("sales_억원") is not None:
            return True
        if data.get("brand_value_series_10pt"):
            return True
    return False


def _has_csd_activity_fact(calls: list[dict[str, Any]], brand: str) -> bool:
    for call in calls:
        if call.get("tool") != "csd_activity_trend":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        if data.get("brand") != brand:
            continue
        if data.get("status") == "ok" and data.get("series"):
            return True
    return False


def _has_market_scope_fact(calls: list[dict[str, Any]]) -> bool:
    for call in calls:
        if call.get("tool") != "get_market_landscape":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        if data.get("hhi_recent") is not None or data.get("hhi") is not None:
            return True
    return False


def _ranking_fact(fact_md: str) -> RankingFact | None:
    line = _mandatory_row_payload(fact_md, "브랜드 핵심 지표")
    if line:
        parsed = _ranking_from_text(line)
        if parsed is not None:
            return parsed
    rows = _key_value_section(fact_md, "지표 fact")
    rank = rows.get("순위", "")
    if not rank:
        return None
    brand = rows.get("브랜드/시장", "")
    period = rows.get("기간", "")
    sales = rows.get("매출", "")
    share = rows.get("시장점유율", "")
    if not all((brand, period, sales, share, rank)):
        return None
    return RankingFact(brand=brand, period=period, sales=sales, share=share, rank=rank)


def _ranking_from_text(text: str) -> RankingFact | None:
    match = re.search(
        r"(?P<brand>\S+)\s+(?P<period>20\d{2}-\d{2})\s+매출\s+"
        r"(?P<sales>-?\d+(?:\.\d+)?억원)\s+시장점유율\s+"
        r"(?P<share>-?\d+(?:\.\d+)?%)\s+순위\s+(?P<rank>\d+(?:/\d+)?)",
        text,
    )
    if match is None:
        return None
    return RankingFact(**match.groupdict())


def _trend_fact(fact_md: str) -> TrendFact | None:
    lines = fact_md.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("### ") and "매출 시계열 fact" in stripped):
            continue
        brand = stripped.removeprefix("### ").split(" 매출 시계열 fact", 1)[0].strip()
        rows: list[TrendRow] = []
        for raw in lines[index + 1 :]:
            current = raw.strip()
            if current.startswith("### "):
                break
            if not current.startswith("|") or "---" in current or "기간" in current:
                continue
            cells = _table_cells(current)
            if len(cells) >= 3 and cells[0] and cells[1] and cells[2]:
                rows.append(TrendRow(period=cells[0], sales=cells[1], share=cells[2]))
        if brand and rows:
            return TrendFact(brand=brand, rows=tuple(rows))
    return None


def _mandatory_row_payload(fact_md: str, label: str) -> str:
    in_section = False
    for line in fact_md.splitlines():
        stripped = line.strip()
        if stripped == "### 필수 답변 fact":
            in_section = True
            continue
        if in_section and stripped.startswith("### "):
            return ""
        if not in_section or not stripped.startswith("|") or "---" in stripped or "구분" in stripped:
            continue
        cells = _table_cells(stripped)
        if len(cells) >= 2 and cells[0] == label:
            return cells[1]
    return ""


def _mandatory_rows(fact_md: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    in_section = False
    for line in fact_md.splitlines():
        stripped = line.strip()
        if stripped == "### 필수 답변 fact":
            in_section = True
            continue
        if in_section and stripped.startswith("### "):
            break
        if not in_section or not stripped.startswith("|") or "---" in stripped or "구분" in stripped:
            continue
        cells = _table_cells(stripped)
        if len(cells) >= 2 and cells[0]:
            rows[cells[0]] = cells[1]
    return rows


def _mandatory_row_items(fact_md: str) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    in_section = False
    for line in fact_md.splitlines():
        stripped = line.strip()
        if stripped == "### 필수 답변 fact":
            in_section = True
            continue
        if in_section and stripped.startswith("### "):
            break
        if not in_section or not stripped.startswith("|") or "---" in stripped or "구분" in stripped:
            continue
        cells = _table_cells(stripped)
        if len(cells) >= 2 and cells[0]:
            rows.append((cells[0], cells[1]))
    return tuple(rows)


def _key_value_section(fact_md: str, title_fragment: str) -> dict[str, str]:
    lines = fact_md.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("### ") and title_fragment in stripped):
            continue
        rows: dict[str, str] = {}
        for raw in lines[index + 1 :]:
            current = raw.strip()
            if current.startswith("### "):
                break
            if not current.startswith("|") or "---" in current or "항목" in current:
                continue
            cells = _table_cells(current)
            if len(cells) >= 2 and cells[0]:
                rows[cells[0]] = cells[1]
        return rows
    return {}


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _mi_implication_for_contract(contract_type: str, question: str, fact_md: str) -> str:
    match contract_type:
        case "segment_compare":
            rows = _segment_compare_support_rows(question, fact_md)
            return _mi_implication_block(_segment_compare_mi_rows(rows, _missing_axes_from_support_rows(rows)))
        case "source_crosscheck":
            rows = _source_crosscheck_support_rows(question, fact_md)
            return _mi_implication_block(_source_crosscheck_mi_rows(rows))
        case "positioning":
            return _mi_implication_block(_positioning_mi_rows(_positioning_rows(fact_md)))
        case "threat_detection":
            rows = _threat_rows(fact_md)
            if not rows:
                rows = (("위협 미식별", "관찰", "보유 signals/news fact 안에서 위협 요인을 확정할 근거가 없습니다."),)
            return _mi_implication_block(_threat_mi_rows(rows))
        case "news_ei":
            return _mi_implication_block(_news_ei_mi_rows(_news_grade_rows(question, fact_md)))
        case _:
            return ""


def _mi_implication_block(rows: tuple[MiImplicationRow, ...]) -> str:
    if not rows:
        return ""
    lines = [
        "## MI implication",
        "| 관찰 | 가능한 의미 | 확인 필요 |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {_contract_cell(row.observation)} | {_contract_cell(row.implication)} | {_contract_cell(row.next_data)} |"
        for row in rows
    )
    return "\n".join(lines)


def _segment_compare_support_rows(question: str, fact_md: str) -> tuple[tuple[str, str, str], ...]:
    mandatory = _mandatory_rows(fact_md)
    rows: list[tuple[str, str, str]] = []
    for axis in _requested_segment_axes(question):
        supported = mandatory.get(f"{axis} 지원", "")
        unsupported = mandatory.get(f"{axis} 미지원", "")
        if supported:
            rows.append((axis, "지원", supported))
        elif unsupported:
            rows.append((axis, "미지원", unsupported))
        else:
            rows.append((axis, "미지원", f"{axis} 축은 이번 fact set에 조회 결과가 없어 값을 추정하지 않습니다."))
    return tuple(rows)


def _missing_axes_from_support_rows(rows: tuple[tuple[str, str, str], ...] | list[tuple[str, str, str]]) -> tuple[str, ...]:
    return tuple(axis for axis, status, _ in rows if status != "지원")


def _segment_compare_mi_rows(
    rows: tuple[tuple[str, str, str], ...] | list[tuple[str, str, str]],
    missing: tuple[str, ...] | list[str],
) -> tuple[MiImplicationRow, ...]:
    supported = tuple((axis, note) for axis, status, note in rows if status == "지원" and note)
    if not supported:
        return ()
    missing_text = ", ".join(missing)
    next_data = (
        f"{missing_text} 데이터가 포함된 동일 기간 market mart 또는 catalog 매핑"
        if missing_text
        else "동일 기간 경쟁 브랜드의 동일 축 반복 관측"
    )
    return tuple(
        MiImplicationRow(
            observation=f"{axis} 축: {note}",
            implication="지원 축 안에서만 구성·집중도 후보를 관찰하며 미지원 축 값을 대체하지 않습니다.",
            next_data=next_data,
        )
        for axis, note in supported[:5]
    )


def _source_crosscheck_support_rows(question: str, fact_md: str) -> tuple[tuple[str, str, str], ...]:
    mandatory = _mandatory_rows(fact_md)
    rows: list[tuple[str, str, str]] = []
    for source in _requested_sources(question):
        supported = mandatory.get(f"{source} 보유", "")
        missing = mandatory.get(f"{source} 미보유", "")
        if supported:
            rows.append((source, "보유", supported))
        elif missing:
            rows.append((source, "미보유", missing))
        else:
            rows.append((source, "미보유", f"{source} 출처는 이번 fact set에 조회 결과가 없어 값을 추정하지 않습니다."))
    return tuple(rows)


def _source_crosscheck_mi_rows(rows: tuple[tuple[str, str, str], ...] | list[tuple[str, str, str]]) -> tuple[MiImplicationRow, ...]:
    supported = tuple((source, note) for source, status, note in rows if status == "보유" and note)
    missing = tuple(source for source, status, _ in rows if status != "보유")
    if not supported:
        return ()
    implication = (
        "양 소스가 모두 보유된 범위에서 기간·정의 차이를 분리해 비교 후보만 확인합니다."
        if len(supported) >= 2
        else "단일 출처 값만 관찰하며 교차 판정은 단정하지 않습니다."
    )
    next_data = (
        f"{', '.join(missing)}의 동일 기간·동일 시장정의 값"
        if missing
        else "양 소스의 기간 정렬 기준과 집계 정의"
    )
    return tuple(
        MiImplicationRow(
            observation=f"{source} 출처: {note}",
            implication=implication,
            next_data=next_data,
        )
        for source, note in supported
    )


def _positioning_mi_rows(rows: tuple[tuple[str, str, str], ...]) -> tuple[MiImplicationRow, ...]:
    result: list[MiImplicationRow] = []
    for axis, value, _ in rows:
        if axis == "시장 순위/MS":
            implication = "현재 시장 내 위치를 읽는 기준점이며 성장이나 방어를 단독으로 판단하지 않습니다."
            next_data = "동일 기간 경쟁 브랜드의 MS·순위 변화"
        elif axis == "성장성":
            implication = "성장 기여 후보로 관찰되며 지속성은 반복 기간에서 확인해야 합니다."
            next_data = "월별 share-of-growth 반복 관측과 채널·세그먼트 분해"
        else:
            implication = "경쟁 압력 후보로 관찰되며 자사 변화의 원인으로 단정하지 않습니다."
            next_data = "경쟁 cohort의 이벤트 전후 처방·매출 변화"
        result.append(MiImplicationRow(observation=f"{axis}: {value}", implication=implication, next_data=next_data))
    return tuple(result[:5])


def _threat_mi_rows(rows: tuple[tuple[str, str, str], ...]) -> tuple[MiImplicationRow, ...]:
    return tuple(
        MiImplicationRow(
            observation=f"{factor}: {basis}",
            implication=f"{direction} 방향의 위협 후보로만 관찰하며 상업적 영향은 아직 분리 확인이 필요합니다.",
            next_data="이벤트 전후 처방·매출, 채널 변화, 경쟁품 세그먼트 추이",
        )
        for factor, direction, basis in rows[:5]
    )


def _news_ei_mi_rows(rows: tuple[NewsGrade, ...]) -> tuple[MiImplicationRow, ...]:
    result: list[MiImplicationRow] = []
    for graded in rows:
        if graded.grade == "noise":
            continue
        news = graded.row
        result.append(
            MiImplicationRow(
                observation=f"{graded.grade}: {news.title} - {news.summary}",
                implication="뉴스 범위의 정성 이슈 후보이며 내부 지표 변화와 연결될 때만 MI 판단 재료로 사용합니다.",
                next_data="기사 이벤트일과 동일 기간 처방·매출·MS, 원문 기사 확인",
            )
        )
    return tuple(result[:5])


def _ranking_surface_ok(answer: str, fact: RankingFact, rule: ContractRule) -> bool:
    if any(forbidden in answer for forbidden in rule.forbidden_outputs):
        return False
    return all(token in answer for token in (fact.brand, fact.period, fact.sales, fact.share, fact.rank))


def _trend_surface_ok(answer: str, fact: TrendFact, rule: ContractRule) -> bool:
    if "| 기간 | 매출 | MS |" not in answer:
        return False
    table_rows = _answer_table_rows(answer)
    if len(table_rows) < rule.min_rows:
        return False
    first = fact.rows[0]
    last = fact.rows[-1]
    return all(token in answer for token in (fact.brand, first.period, first.sales, last.period, last.sales))


def _answer_table_rows(answer: str) -> tuple[str, ...]:
    rows: list[str] = []
    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped or "기간" in stripped:
            continue
        rows.append(stripped)
    return tuple(rows)


def _ranking_answer(fact: RankingFact) -> str:
    return "\n".join(
        (
            f"{fact.brand}는 {fact.period} 기준 매출 {fact.sales}, 시장점유율 {fact.share}, 순위 {fact.rank}입니다.",
            "",
            "| 항목 | 값 |",
            "| --- | --- |",
            f"| 브랜드 | {fact.brand} |",
            f"| 기간 | {fact.period} |",
            f"| 매출 | {fact.sales} |",
            f"| 시장점유율 | {fact.share} |",
            f"| 순위 | {fact.rank} |",
        )
    )


def _trend_answer(fact: TrendFact) -> str:
    first = fact.rows[0]
    last = fact.rows[-1]
    summary = (
        f"{fact.brand} 매출은 {first.period} {first.sales}에서 {last.period} {last.sales}으로 움직였고, "
        f"시장점유율은 {first.share}에서 {last.share}로 변했습니다."
    )
    table_lines = [
        f"### {fact.brand} 매출 시계열",
        "| 기간 | 매출 | MS |",
        "| --- | --- | --- |",
        *(f"| {row.period} | {row.sales} | {row.share} |" for row in fact.rows),
    ]
    return _join_blocks(summary, "\n".join(table_lines))


def _source_block(fact_md: str) -> str:
    return provenance_source_block_from_facts(fact_md)


def _join_blocks(*blocks: str) -> str:
    return "\n\n".join(block.strip() for block in blocks if block and block.strip()).strip()


def _enforce_structural_contract(question: str, answer: str, fact_md: str) -> str:
    contract_type = _structural_contract_type(question)
    if not contract_type or not fact_md:
        return answer
    answer = _dedupe_substantive_lines(answer)
    if contract_type == "trend_support_matrix":
        answer = _repair_split_market_support(answer, fact_md)
    if _structural_contract_present(answer, contract_type):
        if contract_type in _MI_IMPLICATION_CONTRACTS and "## MI implication" not in answer:
            implication = _mi_implication_for_contract(contract_type, question, fact_md)
            if implication:
                return _insert_before_source(answer, implication)
        return answer
    block = ""
    if contract_type == "sales_activity_link":
        answer = _sanitize_sales_activity_answer(answer, fact_md)
        block = _sales_activity_contract_block(fact_md)
    elif contract_type == "segment_compare":
        answer = _sanitize_full_unavailable_answer(answer)
        block = _segment_compare_contract_block(question, fact_md, answer)
    elif contract_type == "source_crosscheck":
        answer = _sanitize_full_unavailable_answer(answer)
        block = _source_crosscheck_contract_block(question, fact_md)
    elif contract_type == "quarter_metric":
        answer = _sanitize_full_unavailable_answer(answer)
        block = _quarter_metric_contract_block(fact_md)
    elif contract_type == "specialty_breakdown":
        block = _specialty_breakdown_contract_block(fact_md)
        if block:
            answer = _sanitize_specialty_unavailable_answer(answer)
    elif contract_type == "trend_support_matrix":
        block = _trend_support_matrix_block(fact_md)
    elif contract_type == "change_drivers":
        block = _change_drivers_contract_block(question, fact_md)
    elif contract_type == "positioning":
        block = _positioning_contract_block(fact_md)
    elif contract_type == "threat_detection":
        block = _threat_detection_contract_block(fact_md)
    elif contract_type == "news_ei":
        block = _news_ei_contract_block(question, fact_md)
    elif contract_type == "patent_exclusivity":
        answer = _sanitize_full_unavailable_answer(answer)
        block = _patent_exclusivity_contract_block(fact_md)
    elif contract_type == "clinical_evidence":
        block = _clinical_evidence_contract_block(question, fact_md)
    if not block:
        return answer
    return _insert_before_source(answer, block)


def _structural_contract_type(question: str) -> str:
    text = question.lower()
    if _is_patent_exclusivity_question(question):
        return "patent_exclusivity"
    if _is_clinical_evidence_question(question):
        return "clinical_evidence"
    if _is_source_crosscheck_question(question):
        return "source_crosscheck"
    if _is_specialty_breakdown_question(question):
        return "specialty_breakdown"
    if _is_segment_compare_question(question):
        return "segment_compare"
    if _is_quarter_metric_question(question):
        return "quarter_metric"
    if any(token in question for token in ("영업활동", "영업 활동", "Impact", "impact", "상기 콜", "콜")):
        return "sales_activity_link"
    if any(token in question for token in ("Weekly", "Monthly", "Class", "Molecule", "용량", "제형")) and "추이" in question:
        return "trend_support_matrix"
    if any(token in question for token in ("변화 요인", "변화요인", "향후 예상", "Market expansion", "External", "Internal", "보건 정책")) or (
        "목표 시장" in question and any(token in question for token in ("출시", "정책", "Line extension", "채널", "변화"))
    ):
        return "change_drivers"
    if _is_news_sales_impact_question(question):
        return "change_drivers"
    if "change driver" in text or "market expansion" in text:
        return "change_drivers"
    if _is_threat_detection_question(question):
        return "threat_detection"
    if _is_positioning_question(question):
        return "positioning"
    if _is_news_ei_question(question):
        return "news_ei"
    return ""


def _contract_tool_names(calls: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for call in calls:
        tool = str(call.get("tool") or "")
        data = call.get("render_data")
        if tool == "deep_analysis_related_news":
            names.add("search_news")
        elif tool == "get_market_landscape":
            names.add("market_scope")
        elif tool:
            names.add(tool)
        if isinstance(data, dict) and data.get("facade_tool"):
            names.add(str(data["facade_tool"]))
    return names


def _is_news_sales_impact_question(question: str) -> bool:
    return (
        "매출" in question
        and any(token in question for token in ("뉴스", "이슈"))
        and any(token in question for token in ("영향", "원인", "왜"))
    )


def _structural_contract_present(answer: str, contract_type: str) -> bool:
    markers = {
        "sales_activity_link": "## 영업-매출 연계 분석 설계",
        "segment_compare": "## 세그먼트 비교 지원 범위",
        "source_crosscheck": "## 출처별 교차 확인 범위",
        "quarter_metric": "## 분기 지표",
        "specialty_breakdown": "## 진료과별 매출 구성",
        "trend_support_matrix": "## 추이 지원 범위",
        "change_drivers": "## 변화 요인 결론",
        "positioning": "## 포지셔닝 축",
        "threat_detection": "## 위협 요인",
        "news_ei": "## 뉴스 관련성 등급",
        "patent_exclusivity": "## 특허 독점권 확인 범위",
        "clinical_evidence": "## 임상 근거 확인 범위",
    }
    return markers.get(contract_type, "\0") in answer


def _is_patent_exclusivity_question(question: str) -> bool:
    return any(token in question for token in ("특허", "독점권", "patent", "Patent", "OrangeBook", "오렌지북", "만료"))


def _is_clinical_evidence_question(question: str) -> bool:
    lowered = question.lower()
    if any(token in lowered for token in ("moa", "mechanism of action", "safety")):
        return True
    return any(
        token in question
        for token in (
            "기전",
            "작용기전",
            "안전성",
            "기대 약효",
            "약효",
            "처방 요인",
            "처방요인",
            "처방 고려",
            "처방 결정",
            "임상",
            "연구 디자인",
            "신규 연구",
        )
    )


def _is_positioning_question(question: str) -> bool:
    return any(token in question for token in ("포지셔닝", "차별점", "차별", "경쟁 대비 위치", "시장 내 위치", "positioning"))


def _is_threat_detection_question(question: str) -> bool:
    return any(token in question for token in ("위협", "리스크", "경쟁 위협", "threat", "risk"))


def _is_specialty_breakdown_question(question: str) -> bool:
    return "진료과" in question and any(token in question for token in ("별", "구성", "매출", "처방", "비중", "상위"))


def _is_quarter_metric_question(question: str) -> bool:
    compact = question.replace(" ", "")
    if not re.search(r"20\d{2}(?:-?Q[1-4]|년?[1-4]분기)", compact, flags=re.IGNORECASE):
        return False
    return any(token in question for token in ("매출", "점유율", "MS", "ms", "M/S"))


def _is_news_ei_question(question: str) -> bool:
    if any(token in question for token in ("변화 요인", "변화요인", "External", "Internal", "change driver", "Market expansion")):
        return False
    return any(token in question for token in ("뉴스", "이슈", "소식", "기사"))


def _is_segment_compare_question(question: str) -> bool:
    axes = _requested_segment_axes(question)
    if not axes:
        return False
    if any(token in question for token in ("세그먼트별", "세그먼트 별", "segment별", "Segment별")):
        return True
    return "비교" in question and len(axes) >= 2


def _requested_segment_axes(question: str) -> tuple[str, ...]:
    axis_tokens: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("Class", ("Class", "class", "클래스")),
        ("Molecule", ("Molecule", "molecule", "성분")),
        ("브랜드", ("브랜드", "Brand", "brand")),
        ("회사", ("회사", "Company", "company")),
        ("제조사", ("제조사", "Manufacturer", "manufacturer")),
        ("용량", ("용량", "Dose", "dose")),
        ("제형", ("제형", "Form", "form")),
    )
    axes: list[str] = []
    for axis, tokens in axis_tokens:
        if any(token in question for token in tokens):
            axes.append(axis)
    return tuple(axes)


def _is_source_crosscheck_question(question: str) -> bool:
    return len(_requested_sources(question)) >= 2 and any(token in question for token in ("교차", "출처별", "출처 별", "source", "Source"))


def _requested_sources(question: str) -> tuple[str, ...]:
    sources: list[str] = []
    if any(token in question for token in ("UBIST", "ubist")):
        sources.append("UBIST")
    if any(token in question for token in ("IQVIA", "iqvia")):
        sources.append("IQVIA")
    return tuple(sources)


def _insert_before_source(answer: str, block: str) -> str:
    if answer.startswith("## 출처"):
        return _join_blocks(block, answer)
    marker = "\n## 출처"
    if marker not in answer:
        return _join_blocks(answer, block)
    head, tail = answer.split(marker, 1)
    return _join_blocks(head, block) + marker + tail


def _sales_activity_contract_block(fact_md: str) -> str:
    trend = _trend_fact(fact_md)
    proxy = _sales_activity_proxy_text(trend)
    csd_activity = _mandatory_row_payload(fact_md, "CSD aggregate 콜수")
    csd_unsupported = _mandatory_row_payload(fact_md, "CSD 세부 미지원") or "impact level, HCP/의사별, 기관별, 활동일, 처방 lag, 비활동 대조군"
    if csd_activity:
        availability = f"CSD ChannelDynamics의 월별 TOTAL 채널 aggregate 콜수/활동량은 확인됩니다: {csd_activity}. 단, 세부 필드({csd_unsupported})는 포함되지 않습니다."
        missing_data = f"impact level, HCP/의사별, 기관별 세부와 활동일·처방 lag·비활동 대조군입니다. CSD aggregate 콜수/활동량은 미보유가 아니라 보유 fact로만 제한 해석합니다."
        current_proxy = f"{csd_activity} 매출 proxy는 별도입니다: {proxy}"
        needed_fields = f"인과 검증에는 {csd_unsupported} 및 활동 전후 처방·매출 연결키가 추가로 필요합니다."
    else:
        availability = "현재 fact set에는 CSD 영업활동, 상기 콜, impact level 행이 없어 영업활동 데이터는 미보유로 처리합니다."
        missing_data = "CSD 영업활동, 상기 콜, impact level, 기관·의사별 활동 원천 데이터입니다."
        current_proxy = proxy
        needed_fields = "콜 수, impact level, 기관·의사, 활동일, 제품·메시지, 처방 lag, 비활동 대조군이 필요합니다."
    return "\n".join(
        (
            "## 영업-매출 연계 분석 설계",
            "| 필수 요소 | 답변 |",
            "| --- | --- |",
            f"| 영업활동 데이터 보유 여부 | {availability} |",
            "| 인과 검증 가능·불가능 | 매출·MS proxy는 확인되지만, 활동→처방·매출 인과는 이 데이터만으로 검증할 수 없습니다. |",
            f"| 매출 proxy 해석 | {proxy} |",
            f"| 필요한 CSD 필드 | {needed_fields} |",
            "| 다음 분석 방법 | 활동 전후 1~3개월 매출·MS를 비활동군과 비교해 uplift와 lag를 추정합니다. |",
            "",
            "### 미보유 데이터 처리",
            "| 단계 | 내용 |",
            "| --- | --- |",
            f"| 1. 미보유 데이터 | {missing_data} |",
            f"| 2. 현재 가능한 proxy | {current_proxy} |",
            "| 3. 해석 가능한 상한선 | proxy는 매출·MS의 동기간 움직임만 보여주며, 영업활동 impact나 처방 인과를 증명하지 않습니다. |",
            f"| 4. 확인 필요 데이터 | {needed_fields} |",
            "| 5. 확보 시 수행할 분석 | 활동 전후 1~3개월 매출·MS를 비활동군과 비교해 uplift와 lag를 추정합니다. |",
        )
    )


def _segment_compare_contract_block(question: str, fact_md: str, answer: str = "") -> str:
    mandatory = _mandatory_rows(fact_md)
    axes = _requested_segment_axes(question)
    rows: list[tuple[str, str, str]] = []
    missing: list[str] = []
    for axis in axes:
        supported = mandatory.get(f"{axis} 지원", "")
        unsupported = mandatory.get(f"{axis} 미지원", "")
        if not supported and not unsupported:
            supported = _segment_compare_answer_payload(axis, answer)
        if supported:
            rows.append((axis, "지원", supported))
        elif unsupported:
            rows.append((axis, "미지원", unsupported))
            missing.append(axis)
        else:
            rows.append((axis, "미지원", f"{axis} 축은 이번 fact set에 조회 결과가 없어 값을 추정하지 않습니다."))
            missing.append(axis)
    if not rows:
        return ""
    lines = [
        "## 세그먼트 비교 지원 범위",
        "| 축 | 지원 여부 | 근거/값 |",
        "| --- | --- | --- |",
        *(f"| {_contract_cell(axis)} | {status} | {_contract_cell(note)} |" for axis, status, note in rows),
    ]
    if missing:
        lines.extend(
            (
                "",
                "### 미지원 축 처리",
                "| 단계 | 내용 |",
                "| --- | --- |",
                f"| 1. 미보유 데이터 | {', '.join(missing)} 축의 운영 query 결과입니다. |",
                "| 2. 현재 가능한 proxy | 지원 축의 UBIST 세그먼트 값만 비교합니다. |",
                "| 3. 해석 가능한 상한선 | 지원 축 내 구성비 비교만 가능하며 미지원 축 값은 대체하지 않습니다. |",
                f"| 4. 확인 필요 데이터 | {', '.join(missing)} 축이 포함된 market mart 또는 catalog 매핑이 필요합니다. |",
                "| 5. 확보 시 수행할 분석 | 같은 기간·같은 시장 기준으로 지원 축과 미지원 축을 병렬 비교합니다. |",
            )
        )
    implication = _mi_implication_block(_segment_compare_mi_rows(rows, missing))
    if implication:
        lines.extend(("", implication))
    return "\n".join(lines)


def _segment_compare_answer_payload(axis: str, answer: str) -> str:
    if not answer:
        return ""
    if axis == "제형":
        dosage_payload = _dosage_combination_payload_from_answer(answer)
        if dosage_payload and any(token in dosage_payload for token in ("억원", "%", "MS", "점유율")):
            return dosage_payload
        table_payload = _dosage_combination_table_payload_from_answer(answer)
        if table_payload:
            return table_payload
    answer = answer.split("### 미보유 데이터 처리", 1)[0].split("## 세그먼트 비교 지원 범위", 1)[0]
    axis_re = re.compile(rf"(?:\\*\\*)?{re.escape(axis)}(?:\\*\\*)?[^\\n|]*(?:매출|MS|점유율)[^\\n]*")
    for raw in answer.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("|"):
            if axis not in stripped or not any(token in stripped for token in ("억원", "%", "MS", "점유율")):
                continue
            cells = _table_cells(stripped)
            if len(cells) >= 3:
                payload = " / ".join(cell for cell in cells if cell and "---" not in cell)
                payload = re.sub(r"\\*+", "", payload).strip()
                note = _segment_compare_dosage_note(axis, answer)
                if note and note not in payload:
                    payload = f"{payload} {note}"
                return payload
            continue
        line = re.sub(r"[*`_]", "", stripped).lstrip("- ").strip()
        if not line or axis not in line:
            continue
        if not any(token in line for token in ("매출", "MS", "점유율")):
            continue
        if any(token in line for token in ("필요", "미보유", "미지원", "조회 실패")):
            continue
        matched = axis_re.search(line)
        payload = matched.group(0) if matched else line
        payload = re.sub(r"\\*+", "", payload).strip()
        note = _segment_compare_dosage_note(axis, answer)
        if note and note not in payload:
            payload = f"{payload} {note}"
        if payload:
            return payload
    return ""


def _dosage_combination_table_payload_from_answer(answer: str) -> str:
    in_dosage_table = False
    for raw in answer.splitlines():
        stripped = raw.strip()
        if stripped.startswith("## 출처"):
            return ""
        if stripped.startswith("### "):
            in_dosage_table = "제형" in stripped or "성분 조합" in stripped
            continue
        if stripped.startswith("|") and ("제형" in stripped or "성분 조합" in stripped):
            in_dosage_table = True
        if not in_dosage_table or not stripped.startswith("|") or "---" in stripped:
            continue
        cells = _table_cells(stripped)
        if len(cells) < 2:
            continue
        value = _dosage_combination_value_from_table_cells(cells)
        if not value or not _is_dosage_combination_value(value):
            continue
        sales = next((cell for cell in cells if "억원" in cell), "")
        share = next((cell for cell in cells if "%" in cell), "")
        parts = [
            f"성분 조합 기준 제형 축: {value}",
            f"매출 {sales}" if sales else "",
            f"MS {share}" if share else "",
        ]
        payload = " ".join(part for part in parts if part)
        return payload if sales or share else ""
    return ""


def _segment_compare_dosage_note(axis: str, answer: str) -> str:
    if axis != "제형":
        return ""
    return dosage_combination_note(axis, _dosage_combination_values_from_answer(answer))


def _append_general_dosage_combination_note(answer: str) -> str:
    if DOSAGE_COMBINATION_NOTE_PREFIX in answer or "제형" not in answer:
        return answer
    note = dosage_combination_note("제형", _dosage_combination_values_from_answer(answer))
    if not note:
        return answer
    return _insert_before_source(answer, note)


def _dosage_combination_values_from_answer(answer: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in answer.splitlines():
        if not _is_dosage_combination_context(raw):
            continue
        for match in re.finditer(r"(?<![A-Za-z가-힣])([A-Za-z가-힣]+/[A-Za-z가-힣]+)(?![A-Za-z가-힣])", raw):
            value = match.group(1)
            if _is_dosage_table_header_value(value) or not _is_dosage_combination_value(value):
                continue
            if value not in values:
                values.append(value)
        for match in re.finditer(r"(?<![A-Za-z가-힣])([A-Za-z가-힣]+)\s*단일", raw):
            value = match.group(1)
            if value not in values and _is_dosage_combination_value(value):
                values.append(value)
    in_dosage_table = False
    for raw in answer.splitlines():
        stripped = raw.strip()
        if stripped.startswith("## 출처") or stripped.startswith("### 수치별 출처"):
            in_dosage_table = False
            continue
        if stripped.startswith("### "):
            in_dosage_table = "제형" in stripped or "성분 조합" in stripped
            continue
        if stripped.startswith("|") and ("제형" in stripped or "성분 조합" in stripped):
            in_dosage_table = True
            continue
        if not in_dosage_table or not stripped.startswith("|") or "---" in stripped:
            continue
        cells = _table_cells(stripped)
        if not cells:
            continue
        value = _dosage_combination_value_from_table_cells(cells)
        if value and _is_dosage_combination_value(value) and value not in values:
            values.append(value)
    return tuple(values)


def _dosage_combination_value_from_table_cells(cells: list[str]) -> str:
    header_tokens = {"순위", "구분", "제형", "매출", "MS", "시장점유율", "기간", "출처", "값", "축", "지원 여부", "근거/값"}
    for cell in cells:
        value = cell.strip()
        if not value:
            continue
        if value in header_tokens or _is_dosage_table_header_value(value) or "구분" in value:
            continue
        embedded = re.search(r"(?<![A-Za-z가-힣])([A-Za-z가-힣]+/[A-Za-z가-힣]+)(?![A-Za-z가-힣])", value)
        if embedded and _is_dosage_combination_value(embedded.group(1)):
            return embedded.group(1)
        if re.fullmatch(r"\d+(?:위)?", value):
            continue
        if any(token in value for token in ("억원", "%", "MS", "시장점유율", "매출")):
            continue
        if value in {"지원", "미지원"}:
            continue
        return value
    return ""


def _is_dosage_table_header_value(value: str) -> bool:
    return value in {"근거/값", "지원/미지원"} or any(token in value for token in ("근거", "지원 여부", "구분", "시장점유율"))


def _is_dosage_combination_value(value: str) -> bool:
    if not value or value == "제형":
        return False
    blocked_tokens = (
        "catalog",
        "query",
        "UBIST",
        "IQVIA",
        "Class",
        "Molecule",
        "브랜드",
        "용량",
        "제형",
        "채널",
        "주간",
        "월간",
        "기간",
        "시장",
        "원천",
        "확보",
        "proxy",
        "상한선",
        "수행할 분석",
    )
    return not any(token in value for token in blocked_tokens)


def _is_dosage_combination_context(line: str) -> bool:
    if not any(token in line for token in ("제형", "성분 조합")):
        return False
    blocked_tokens = ("미지원", "미보유", "확인 필요", "수행할 분석", "해석 가능한", "조회 성공하지 못")
    return not any(token in line for token in blocked_tokens)


def _dosage_combination_payload_from_answer(answer: str) -> str:
    values = _dosage_combination_values_from_answer(answer)
    if not values:
        return ""
    return f"성분 조합 기준 제형 축: {', '.join(values[:5])}"


def _source_crosscheck_contract_block(question: str, fact_md: str) -> str:
    rows = _source_crosscheck_support_rows(question, fact_md)
    supported_count = sum(1 for _, status, _ in rows if status == "보유")
    if not rows:
        return ""
    lines = [
        "## 출처별 교차 확인 범위",
        "| 출처 | 보유 여부 | 값/처리 |",
        "| --- | --- | --- |",
        *(f"| {_contract_cell(source)} | {status} | {_contract_cell(note)} |" for source, status, note in rows),
    ]
    if supported_count < len(rows):
        lines.extend(
            (
                "",
                "### 교차 판정",
                "양 소스가 모두 확보될 때만 일치/불일치 판정을 합니다. 현재는 보유 소스 값만 표시하고 미보유 소스 값은 추정하지 않습니다.",
            )
        )
    else:
        lines.extend(("", "### 교차 판정", "양 소스가 모두 확보된 범위에서만 기간·정의 차이를 확인합니다."))
    implication = _mi_implication_block(_source_crosscheck_mi_rows(rows))
    if implication:
        lines.extend(("", implication))
    return "\n".join(lines)


def _quarter_metric_contract_block(fact_md: str) -> str:
    mandatory = _mandatory_rows(fact_md)
    metric = mandatory.get("브랜드 핵심 지표") or mandatory.get("매출 추이") or mandatory.get("조회 실패")
    if not metric:
        return ""
    return "\n".join(
        (
            "## 분기 지표",
            f"- {_contract_cell(metric)}",
            "- 표시된 기간·소스의 보유 fact만 사용하며, 미확인 값은 추정하지 않습니다.",
        )
    )


def _specialty_breakdown_contract_block(fact_md: str) -> str:
    rows = _specialty_breakdown_rows(fact_md)
    if not rows:
        return ""
    lines = [
        "## 진료과별 매출 구성",
        "| 진료과 | 순위/값 |",
        "| --- | --- |",
        *(f"| {_contract_cell(name)} | {_contract_cell(payload)} |" for name, payload in rows),
        "",
        "진료과 구분은 보유 mart fact 범위에서만 표시하며, 미반환 진료과 값은 추정하지 않습니다.",
    ]
    return "\n".join(lines)


def _specialty_breakdown_rows(fact_md: str) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for label, payload in _mandatory_row_items(fact_md):
        normalized = label.lower()
        if "specialty" not in normalized and "진료과" not in label:
            continue
        name = _specialty_name_from_payload(payload)
        rows.append((name or "진료과", payload))
    if not rows:
        rows.extend(_specialty_rows_from_fact_tables(fact_md))
    return tuple(rows[:8])


def _specialty_name_from_payload(payload: str) -> str:
    match = re.search(r"\d+위\s+(?P<name>.+?)\s+(?:시장점유율|MS|매출)", payload)
    if match is None:
        return ""
    return match.group("name").strip()


def _specialty_rows_from_fact_tables(fact_md: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    in_specialty_table = False
    for raw in fact_md.splitlines():
        stripped = raw.strip()
        if stripped.startswith("### "):
            heading = stripped.lower()
            in_specialty_table = "specialty" in heading or "진료과" in stripped or "분석 기준별" in stripped
            continue
        if not in_specialty_table:
            continue
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = _table_cells(stripped)
        if len(cells) < 4:
            continue
        if any(cell in {"순위", "구분", "시장점유율", "매출"} for cell in cells):
            continue
        rank, name, share, sales = cells[:4]
        if not name:
            continue
        parts = [part for part in (f"{rank}위" if rank and not rank.endswith("위") else rank, name, f"시장점유율 {share}" if share else "", f"매출 {sales}" if sales else "") if part]
        rows.append((name, " ".join(parts)))
    return rows


def _positioning_contract_block(fact_md: str) -> str:
    rows = _positioning_rows(fact_md)
    if not rows:
        return ""
    direct = _positioning_direct_answer(rows)
    lines = [
        "## 포지셔닝 축",
        "| 축 | 자사/관찰 값 | 시장 상위 대비 위치 |",
        "| --- | --- | --- |",
        *(f"| {_contract_cell(axis)} | {_contract_cell(value)} | {_contract_cell(position)} |" for axis, value, position in rows),
        "",
        f"자사 위치: {direct}",
    ]
    implication = _mi_implication_block(_positioning_mi_rows(rows))
    if implication:
        lines.extend(("", implication))
    return "\n".join(lines)


def _positioning_rows(fact_md: str) -> tuple[tuple[str, str, str], ...]:
    mandatory = _mandatory_rows(fact_md)
    items = _mandatory_row_items(fact_md)
    rows: list[tuple[str, str, str]] = []
    ranking = mandatory.get("브랜드 핵심 지표", "")
    if ranking:
        rows.append(("시장 순위/MS", ranking, "보유 rank/MS fact 범위에서만 자사 위치를 표시합니다."))
    growth = _first_payload_containing(items, ("share-of-growth", "성장분해", "점유"))
    if growth:
        rows.append(("성장성", growth, "share-of-growth 또는 점유 변화 fact 기준의 성장성 신호입니다."))
    pressure = _first_payload_containing(items, ("cohort z-score", "백분위", "top gainer", "top faller", "시장 변화"))
    if pressure:
        rows.append(("경쟁 압력", pressure, "경쟁 cohort·상승/하락 브랜드 fact 기준의 압력 신호입니다."))
    return tuple(rows)


def _first_payload_containing(items: tuple[tuple[str, str], ...], tokens: tuple[str, ...]) -> str:
    for label, payload in items:
        if label != "인사이트 계산":
            continue
        if any(token in payload for token in tokens):
            return payload
    return ""


def _positioning_direct_answer(rows: tuple[tuple[str, str, str], ...]) -> str:
    ranking = next((value for axis, value, _ in rows if axis == "시장 순위/MS"), "")
    growth = next((value for axis, value, _ in rows if axis == "성장성"), "")
    pressure = next((value for axis, value, _ in rows if axis == "경쟁 압력"), "")
    parts = [part for part in (ranking, growth, pressure) if part]
    if not parts:
        return "수집된 포지셔닝 fact가 없어 자사 위치를 단정하지 않습니다."
    return " / ".join(parts[:3]) + " 기준으로만 해석합니다."


def _threat_detection_contract_block(fact_md: str) -> str:
    rows = _threat_rows(fact_md)
    lines = [
        "## 위협 요인",
        "| 위협 요인 | 방향 | 근거 |",
        "| --- | --- | --- |",
    ]
    if rows:
        lines.extend(f"| {_contract_cell(factor)} | {direction} | {_contract_cell(basis)} |" for factor, direction, basis in rows)
    else:
        lines.append("| 위협 미식별 | 관찰 | 보유 signals/news fact 안에서 위협 요인을 확정할 근거가 없습니다. |")
    implication_rows = rows or (("위협 미식별", "관찰", "보유 signals/news fact 안에서 위협 요인을 확정할 근거가 없습니다."),)
    lines.extend(("", _mi_implication_block(_threat_mi_rows(implication_rows))))
    return "\n".join(lines)


def _threat_rows(fact_md: str) -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = []
    for label, payload in _mandatory_row_items(fact_md):
        if label != "인사이트 계산":
            continue
        lower = payload.lower()
        if "top gainer" in lower or "share-of-growth" in payload or "cohort z-score" in lower or "백분위" in payload:
            rows.append(("경쟁 브랜드 점유 확대", _threat_direction(payload), payload))
    for news in _news_factor_rows(fact_md):
        if news.direction == "위협":
            rows.append(("뉴스 기반 경쟁/시장 위협", "확대", _news_factor_basis(news)))
    return tuple(rows[:5])


def _threat_direction(text: str) -> str:
    if re.search(r"[+]\d", text) or any(token in text for token in ("확대", "상승", "증가", "top gainer")):
        return "확대"
    if re.search(r"-\d", text) or any(token in text for token in ("축소", "하락", "감소", "top faller")):
        return "축소"
    return "관찰"


def _news_ei_contract_block(question: str, fact_md: str) -> str:
    rows = _news_grade_rows(question, fact_md)
    if not rows:
        return ""
    lines = [
        "## 뉴스 관련성 등급",
        "| 관련성 등급 | 기사 | 방향 | 근거 | 처리 상한 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for graded in rows:
        news = graded.row
        basis = _news_factor_basis(news)
        lines.append(
            f"| {graded.grade} | {_contract_cell(news.title)} | {_contract_cell(news.direction)} | {_contract_cell(basis)} | {_contract_cell(graded.handling)} |"
        )
    lines.extend(
        (
            "",
            "뉴스는 기사 제목·요약·발췌 범위의 정성 근거이며, 입증/확인됨/달성으로 단정하지 않습니다.",
        )
    )
    implication = _mi_implication_block(_news_ei_mi_rows(rows))
    if implication:
        lines.extend(("", implication))
    return "\n".join(lines)


def _news_grade_rows(question: str, fact_md: str) -> tuple[NewsGrade, ...]:
    brand = _brand_from_question(question) or _brand_from_fact_md(fact_md)
    rows: list[NewsGrade] = []
    for row in _news_factor_rows(fact_md):
        grade = _news_relevance_grade(row, brand)
        rows.append(NewsGrade(row=row, grade=grade, handling=_news_grade_handling(grade)))
    return tuple(rows)


def _news_relevance_grade(row: NewsFactor, brand: str) -> str:
    text = " ".join((row.title, row.summary))
    if brand and re.search(rf"{re.escape(brand)}(?![가-힣A-Za-z0-9])", text):
        return "direct"
    if brand and brand in text:
        return "family"
    if any(token in text for token in ("이상지질혈증", "고지혈증", "스타틴", "Statin", "복합제", "에제티미브", "피타바스타틴", "시장", "경쟁")):
        return "market"
    if any(token in text for token in ("의약품", "제약", "급여", "약가", "정책")):
        return "background"
    return "noise"


def _news_grade_handling(grade: str) -> str:
    if grade == "direct":
        return "브랜드 직접 관련 뉴스로만 정성 참고합니다."
    if grade == "family":
        return "브랜드 패밀리 관련 맥락으로만 참고합니다."
    if grade == "market":
        return "시장/경쟁 맥락으로만 참고합니다."
    if grade == "background":
        return "배경 정보로 분리하고 브랜드 fact로 승격하지 않습니다."
    return "잡음 후보로 본문 요약 근거에서 제외합니다."


def _brand_from_question(question: str) -> str:
    bracket = re.search(r"\[([^\]]+)\]", question)
    if bracket:
        return bracket.group(1).strip()
    for brand in ("리바로", "악템라", "페린젝트", "리바로젯"):
        if brand in question:
            return brand
    return ""


def _brand_from_fact_md(fact_md: str) -> str:
    ranking = _mandatory_row_payload(fact_md, "브랜드 핵심 지표")
    parsed = _ranking_from_text(ranking) if ranking else None
    return parsed.brand if parsed else ""


def _dedupe_substantive_lines(answer: str) -> str:
    seen: set[str] = set()
    kept: list[str] = []
    for raw in answer.splitlines():
        key = re.sub(r"\s+", " ", raw.strip(" -*\t")).strip()
        if len(key) >= 45 and not raw.lstrip().startswith("|"):
            if key in seen:
                continue
            seen.add(key)
        kept.append(raw)
    return "\n".join(kept)


def _sanitize_full_unavailable_answer(answer: str) -> str:
    blocked_tokens = ("확보되지 않아 분석 불가능", "분석 불가능합니다", "확인 불가합니다", "수행할 수 없습니다")
    kept: list[str] = []
    for raw in answer.splitlines():
        if any(token in raw for token in blocked_tokens):
            continue
        kept.append(raw)
    return "\n".join(kept).strip()


def _sanitize_specialty_unavailable_answer(answer: str) -> str:
    answer = _sanitize_full_unavailable_answer(answer)
    blocked_heading_tokens = ("분석의 한계", "한계 및 시사점")
    blocked_body_tokens = ("진료과별", "데이터 부재", "데이터가 부재", "현재 데이터 부재", "추후 데이터")
    kept: list[str] = []
    skip_until_blank = False
    for raw in answer.splitlines():
        stripped = raw.strip()
        if skip_until_blank:
            if not stripped:
                skip_until_blank = False
            continue
        if stripped.startswith("**") and any(token in stripped for token in blocked_heading_tokens):
            skip_until_blank = True
            continue
        if "진료과별" in raw and any(token in raw for token in blocked_body_tokens):
            continue
        kept.append(raw)
    return "\n".join(kept).strip()


def _sanitize_sales_activity_answer(answer: str, fact_md: str) -> str:
    trend = _trend_fact(fact_md)
    if trend is None or not trend.rows:
        return answer
    first = trend.rows[0]
    latest = trend.rows[-1]
    first_share = _number_from_text(first.share)
    latest_share = _number_from_text(latest.share)
    if latest_share >= first_share:
        return answer
    replacement = (
        "매출은 저점 이후 일부 반등이 관찰되지만, 최신 MS는 "
        f"{first.share}에서 {latest.share}로 낮아져 영업활동 impact나 회복을 단정할 수 없습니다."
    )
    lines: list[str] = []
    replaced = False
    for raw in answer.splitlines():
        if "회복 흐름" in raw or "회복세" in raw:
            lines.append(replacement)
            replaced = True
        else:
            lines.append(raw)
    if not replaced:
        return answer
    return "\n".join(lines)


def _sales_activity_proxy_text(trend: TrendFact | None) -> str:
    if trend is None or not trend.rows:
        return "현재 보유 fact에서는 매출 proxy를 구성할 충분한 시계열을 확인하지 못했습니다."
    first = trend.rows[0]
    latest = trend.rows[-1]
    sales_values = tuple(_number_from_text(row.sales) for row in trend.rows)
    min_index = min(range(len(sales_values)), key=lambda index: sales_values[index]) if sales_values else 0
    min_row = trend.rows[min_index]
    share_note = ""
    first_share = _number_from_text(first.share)
    latest_share = _number_from_text(latest.share)
    if latest_share < first_share:
        share_note = f" MS는 {first.share}에서 {latest.share}로 낮아졌습니다."
    elif latest_share > first_share:
        share_note = f" MS는 {first.share}에서 {latest.share}로 높아졌습니다."
    elif first.share and latest.share:
        share_note = f" MS는 {first.share}에서 {latest.share}로 큰 방향 변화가 제한적입니다."
    rebound = ""
    if min_row.period not in {first.period, latest.period} and _number_from_text(latest.sales) > _number_from_text(min_row.sales):
        rebound = f" 중간 저점({min_row.period} {min_row.sales}) 이후 최신월은 반등했지만,"
    return f"{trend.brand} 매출은 {first.period} {first.sales}에서 {latest.period} {latest.sales}로 관찰됩니다.{rebound}{share_note}"


def _clinical_evidence_contract_block(question: str, fact_md: str) -> str:
    evidence_rows = _clinical_evidence_rows(fact_md)
    lines = ["## 임상 근거 확인 범위"]
    if evidence_rows:
        lines.extend(
            (
                "반환된 작용기전·안전성·처방요인 fact만 본문 근거로 고정합니다. UBIST 매출·순위표는 임상 근거를 대체하지 않습니다.",
                "",
                "### 임상/처방 근거 fact",
                "| 구분 | 확인 내용 | 출처 |",
                "| --- | --- | --- |",
            )
        )
        lines.extend(
            f"| {_contract_cell(row.category)} | {_contract_cell(row.content)} | {_contract_cell(row.source)} |"
            for row in evidence_rows[:8]
        )
        return "\n".join(lines)
    lines.extend(
        (
            _clinical_unavailable_sentence(question),
            "UBIST 매출·시장점유율·순위표는 참고용 시장 현황(보조)이며, 처방요인·작용기전·안전성 profile의 본문 답변을 대체하지 않습니다.",
        )
    )
    market_rows = _clinical_market_context_rows(fact_md)
    if market_rows:
        lines.extend(
            (
                "",
                "### 참고용 시장 현황(보조)",
                "| 항목 | 값 |",
                "| --- | --- |",
            )
        )
        lines.extend(f"| {_contract_cell(label)} | {_contract_cell(value)} |" for label, value in market_rows[:8])
    return "\n".join(lines)


def _clinical_unavailable_sentence(question: str) -> str:
    if any(token in question for token in ("처방 요인", "처방요인", "처방 고려", "처방 결정")):
        return "현재 fact set에는 처방요인을 확정할 임상·가이드라인·처방 근거 행이 확인되지 않습니다."
    return "현재 fact set에는 작용기전, 기대 약효, 안전성 profile을 확정할 임상 근거 행이 확인되지 않습니다."


def _clinical_evidence_rows(fact_md: str) -> tuple[ClinicalEvidenceRow, ...]:
    rows: list[ClinicalEvidenceRow] = []
    for label, payload in _mandatory_row_items(fact_md):
        if _clinical_evidence_row_has_required_surface(label, payload):
            rows.append(ClinicalEvidenceRow(category=label, content=payload, source=_clinical_source_for_payload(payload)))
    for headers, cells in _markdown_table_rows_in_sections(fact_md, ("임상", "안전성", "작용기전", "MoA", "처방 요인", "처방요인")):
        category = _cell_for_header(headers, cells, ("구분", "항목", "category", "type")) or _clinical_category_from_cells(cells)
        content = _cell_for_header(headers, cells, ("내용", "결과", "근거", "요약", "효과", "안전성", "profile", "summary"))
        source = _cell_for_header(headers, cells, ("출처", "source", "근거", "문헌", "reference")) or _clinical_source_for_payload(" ".join(cells))
        if not content:
            content = " / ".join(cell for cell in cells if cell and "---" not in cell)
        if _clinical_evidence_row_has_required_surface(category, content):
            rows.append(ClinicalEvidenceRow(category=category or "임상 근거", content=content, source=source))
    return tuple(_dedupe_clinical_evidence_rows(rows))


def _clinical_evidence_row_has_required_surface(label: str, payload: str) -> bool:
    text = f"{label} {payload}"
    clinical_tokens = (
        "MoA",
        "mechanism",
        "작용기전",
        "기전",
        "안전성",
        "Safety",
        "효과",
        "약효",
        "임상",
        "연구",
        "처방 요인",
        "처방요인",
        "가이드라인",
        "부작용",
        "profile",
    )
    market_only_tokens = ("매출", "시장점유율", "순위", "MS", "UBIST", "IQVIA")
    has_clinical = any(token in text for token in clinical_tokens)
    has_only_market = any(token in text for token in market_only_tokens) and not has_clinical
    return bool(payload.strip()) and has_clinical and not has_only_market


def _clinical_market_context_rows(fact_md: str) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for label, payload in _mandatory_row_items(fact_md):
        if any(token in payload for token in ("매출", "시장점유율", "순위", "MS", "UBIST", "IQVIA")):
            rows.append((label, payload))
    if rows:
        return tuple(rows)
    for headers, cells in _markdown_table_rows_in_sections(fact_md, ("출처", "브랜드", "시장", "후보군", "분석 기준")):
        label = _cell_for_header(headers, cells, ("구분", "항목", "브랜드", "수치", "순위")) or _clinical_category_from_cells(cells)
        value = " / ".join(cell for cell in cells if cell and "---" not in cell)
        if label and value and any(token in value for token in ("억원", "%", "MS", "시장", "순위", "UBIST", "IQVIA")):
            rows.append((label, value))
    return tuple(rows)


def _clinical_category_from_cells(cells: tuple[str, ...]) -> str:
    return next((cell for cell in cells if cell and not re.fullmatch(r"\d+(?:위)?", cell)), "임상 근거")


def _clinical_source_for_payload(payload: str) -> str:
    if "MFDS" in payload or "식약처" in payload:
        return "MFDS"
    if "OrangeBook" in payload or "오렌지북" in payload:
        return "OrangeBook"
    if "UBIST" in payload:
        return "UBIST(시장 보조)"
    if "IQVIA" in payload:
        return "IQVIA(시장 보조)"
    return "반환 fact"


def _dedupe_clinical_evidence_rows(rows: list[ClinicalEvidenceRow]) -> list[ClinicalEvidenceRow]:
    seen: set[tuple[str, str]] = set()
    deduped: list[ClinicalEvidenceRow] = []
    for row in rows:
        key = (row.category, row.content)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _patent_exclusivity_contract_block(fact_md: str) -> str:
    patent_rows = _patent_fact_rows(fact_md)
    lines = [
        "## 특허 독점권 확인 범위",
    ]
    if patent_rows:
        lines.extend(
            (
                "반환된 MFDS/OrangeBook 특허 fact만 본문에 고정합니다. UBIST 매출 후보군은 특허 상태를 대체하지 않습니다.",
                "",
                "### 특허 fact",
                "| 출처 | 제품/성분 | 특허번호 | 상태 | 만료일 | 권리자/출원인 |",
                "| --- | --- | --- | --- | --- | --- |",
            )
        )
        lines.extend(
            "| "
            + " | ".join(
                _contract_cell(value)
                for value in (row.source, row.product, row.patent_no, row.status, row.expiry, row.owner)
            )
            + " |"
            for row in patent_rows[:8]
        )
        return "\n".join(lines)
    lines.extend(
        (
            "현재 연결된 MFDS/OrangeBook 조회에서는 특허번호·만료일이 확인되지 않습니다.",
            "UBIST 매출 후보군은 조회 대상 선별 보조 정보이며, 특허 상태를 대체하지 않습니다.",
        )
    )
    candidate_rows = _patent_candidate_rows(fact_md)
    if candidate_rows:
        lines.extend(
            (
                "",
                "### 조회 대상 후보군(보조)",
                "| 순위 | 성분 | 대표 브랜드 | 출처 | 시장 | 기간 | 매출 | MS |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            )
        )
        lines.extend("| " + " | ".join(_contract_cell(value) for value in row) + " |" for row in candidate_rows[:8])
    return "\n".join(lines)


def _patent_fact_rows(fact_md: str) -> tuple[PatentFactRow, ...]:
    rows: list[PatentFactRow] = []
    for headers, cells in _markdown_table_rows_in_sections(fact_md, ("특허 fact",)):
        row = _patent_fact_row(headers, cells)
        if row is not None:
            rows.append(row)
    return tuple(_dedupe_patent_rows(rows))


def _patent_fact_row(headers: tuple[str, ...], cells: tuple[str, ...]) -> PatentFactRow | None:
    if len(headers) == 1:
        row = _patent_fact_row_from_text(cells[0] if cells else "")
        return row if row and _patent_row_has_required_surface(row) else None
    source = _cell_for_header(headers, cells, ("출처", "source"))
    product = _cell_for_header(headers, cells, ("제품", "성분", "품목", "product", "ingredient"))
    patent_no = _cell_for_header(headers, cells, ("특허번호", "DOMESTIC_PATENT_NO", "KOR_PAT_NO", "patent"))
    status = _cell_for_header(headers, cells, ("상태", "status"))
    expiry = _cell_for_header(headers, cells, ("만료일", "DOMESTIC_END_DATE", "KOR_EXP_DATE", "expiry", "expiration"))
    owner = _cell_for_header(headers, cells, ("권리자", "출원인", "PATENTEE", "KOR_APPLICANT", "owner", "applicant"))
    row = PatentFactRow(
        source=source or "MFDS/OrangeBook",
        product=product or "-",
        patent_no=patent_no or "-",
        status=status or "-",
        expiry=expiry or "-",
        owner=owner or "-",
    )
    return row if _patent_row_has_required_surface(row) else None


def _patent_fact_row_from_text(text: str) -> PatentFactRow | None:
    if not text or "조회 결과 없음" in text:
        return None
    patent_no = _regex_value(text, r"(?:DOMESTIC_PATENT_NO|KOR_PAT_NO|특허번호|patent(?:_no)?)\s*[:=]\s*([^,;|]+)")
    expiry = _regex_value(text, r"(?:DOMESTIC_END_DATE|KOR_EXP_DATE|만료일|expiry|expiration)\s*[:=]\s*([^,;|]+)")
    owner = _regex_value(text, r"(?:PATENTEE|KOR_APPLICANT|권리자|출원인|owner|applicant)\s*[:=]\s*([^,;|]+)")
    status = _regex_value(text, r"(?:DOMESTIC_PATENT_STATUS|KOR_STATUS|상태|status)\s*[:=]\s*([^,;|]+)") or "-"
    product = _regex_value(text, r"(?:ITEM_NAME|PRT_NAME|INGR_NAME|제품|성분|품목)\s*[:=]\s*([^,;|]+)") or "-"
    source = "OrangeBook" if "OrangeBook" in text or "orangebook" in text else "MFDS/OrangeBook"
    row = PatentFactRow(source=source, product=product, patent_no=patent_no or "-", status=status, expiry=expiry or "-", owner=owner or "-")
    return row if _patent_row_has_required_surface(row) else None


def _patent_row_has_required_surface(row: PatentFactRow) -> bool:
    fields = (row.patent_no, row.expiry, row.owner, row.status, row.source)
    return sum(1 for value in fields if value and value != "-") >= 2 and (row.patent_no != "-" or row.expiry != "-")


def _patent_candidate_rows(fact_md: str) -> tuple[tuple[str, str, str, str, str, str, str, str], ...]:
    rows: list[tuple[str, str, str, str, str, str, str, str]] = []
    for headers, cells in _markdown_table_rows_in_sections(fact_md, ("경쟁 성분 후보군 fact",)):
        row = tuple(_cell_for_header(headers, cells, names) or "-" for names in (
            ("순위", "rank"),
            ("성분", "molecule"),
            ("대표 브랜드", "brand"),
            ("출처", "source"),
            ("시장", "market"),
            ("기간", "period"),
            ("매출", "sales"),
            ("MS", "market_share"),
        ))
        rows.append(row)  # type: ignore[arg-type]
    return tuple(rows)


def _markdown_table_rows_in_sections(fact_md: str, title_fragments: tuple[str, ...]) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    rows: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    lines = fact_md.splitlines()
    in_section = False
    headers: tuple[str, ...] = ()
    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("### "):
            in_section = any(fragment in stripped for fragment in title_fragments)
            headers = ()
            continue
        if not in_section:
            continue
        if not stripped.startswith("|"):
            continue
        if "---" in stripped:
            continue
        cells = tuple(_table_cells(stripped))
        if not headers:
            headers = cells
            continue
        if len(cells) == len(headers):
            rows.append((headers, cells))
    return tuple(rows)


def _cell_for_header(headers: tuple[str, ...], cells: tuple[str, ...], names: tuple[str, ...]) -> str:
    for header, value in zip(headers, cells, strict=False):
        header_norm = header.lower().replace(" ", "")
        if any(name.lower().replace(" ", "") in header_norm for name in names):
            return value.strip()
    return ""


def _regex_value(text: str, pattern: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _dedupe_patent_rows(rows: list[PatentFactRow]) -> list[PatentFactRow]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[PatentFactRow] = []
    for row in rows:
        key = (row.source, row.product, row.patent_no)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _trend_support_matrix_block(fact_md: str) -> str:
    axis_trends = _axis_trend_rows(fact_md)
    supported_axes = {row["axis"] for row in axis_trends}
    has_monthly = "월별 MS fact" in fact_md or "매출 시계열 fact" in fact_md
    split_note = _split_market_structure_note(fact_md)
    rows = [
        ("Weekly", "미지원", "현재 fact set에 주간 grain 행이 없어 주간 변화는 확정하지 않습니다."),
        ("Monthly", "지원" if has_monthly else "미지원", "월별 시계열 또는 월별 MS fact 기준으로만 해석합니다." if has_monthly else "월별 행이 반환되지 않았습니다."),
        (
            "Class",
            "지원(구조)" if split_note else "지원" if "Class" in supported_axes else "미지원",
            split_note or "Class 축 fact가 있을 때만 별도 해석합니다.",
        ),
        ("Molecule", "지원" if "성분" in supported_axes else "미지원", "성분 축 fact가 있을 때만 별도 해석합니다."),
        ("Brand", "지원" if "브랜드" in supported_axes or "매출 시계열 fact" in fact_md else "미지원", "브랜드 시계열 fact 범위 안에서만 해석합니다."),
        ("Dose", "미지원", "용량 축 fact가 없어 용량별 변화는 확정하지 않습니다."),
        ("Form", "지원" if "제형" in supported_axes else "미지원", "제형 축 fact가 있을 때 제형 변화만 proxy로 사용합니다."),
    ]
    lines = [
        "## 추이 지원 범위",
        "| 축 | 지원 여부 | 처리 |",
        "| --- | --- | --- |",
        *(f"| {axis} | {status} | {note} |" for axis, status, note in rows),
    ]
    so_what = _axis_trend_so_what(axis_trends)
    if so_what:
        lines.extend(("", "### 지원 축 so-what", so_what))
    lines.extend(
        (
            "",
            "### 미지원 축 대체 분석",
            "미지원 축은 값을 추정하지 않고, 지원되는 월별·축별 proxy에서 방향성만 관찰합니다. 추가 확인에는 주간 grain, 성분·브랜드·용량별 원천 행이 필요합니다.",
        )
    )
    return "\n".join(lines)


def _repair_split_market_support(answer: str, fact_md: str) -> str:
    split_note = _split_market_structure_note(fact_md)
    if not split_note:
        return answer
    replacement = f"| Class | 지원(구조) | {split_note} |"
    pattern = re.compile(r"(?m)^\|\s*Class\s*\|\s*미지원\s*\|[^\n]*\|$")
    if pattern.search(answer):
        return pattern.sub(replacement, answer)
    if "## 추이 지원 범위" in answer and replacement not in answer:
        return _insert_before_source(answer, "\n".join(("### Class 구조 기준", replacement)))
    return answer


def _split_market_structure_note(fact_md: str) -> str:
    for line in fact_md.splitlines():
        if "Class 구조 기준" not in line or "Class 구분 존재" not in line:
            continue
        cells = _table_cells(line)
        if len(cells) >= 2:
            return cells[1]
    return ""


def _change_drivers_contract_block(question: str, fact_md: str) -> str:
    trend = _trend_fact(fact_md)
    proxy = _sales_activity_proxy_text(trend) if trend is not None else "보유 정량 fact 범위에서 매출·MS·채널 proxy만 확인 가능합니다."
    news_rows = _news_factor_rows(fact_md)
    conclusion = (
        f"반환된 뉴스 {len(news_rows)}건과 보유 UBIST/IQVIA proxy만으로 변화 요인을 분류했습니다."
        if news_rows
        else "반환된 뉴스 fact가 없어 보유 UBIST/IQVIA proxy와 미보유 항목을 분리해 표시합니다."
    )
    table_rows = [
        "| 구분(E/I) | 요인 | 관련성 등급 | 근거(뉴스명·인용 or UBIST or 미보유) | 영향방향 | 확인 필요 데이터 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    brand = _brand_from_question(question) or _brand_from_fact_md(fact_md)
    for row in news_rows:
        grade = _news_relevance_grade(row, brand)
        table_rows.append(
            f"| {_contract_cell(row.category)} | {_contract_cell(row.factor)} | {grade} | {_contract_cell(_news_factor_basis(row))} | {_contract_cell(row.direction)} | 기사 원문, 동일 기간 처방·매출 변동 |"
        )
    table_rows.extend(
        (
            "| External | 정책/약가 변화 | 미보유 | 미보유 | 불확실 | 정책 변경일, 약가/급여 변화, 경쟁품 처방 변화 |",
            f"| Internal | 자사 영업/채널 활동 | 보유 fact | UBIST proxy: {proxy} | 불확실 | 채널별 활동량, 세그먼트별 처방량 |",
        )
    )
    return "\n".join(
        (
            "## 변화 요인 결론",
            conclusion,
            "",
            "### External/Internal 결과표",
            *table_rows,
            "",
            "### 채널 현황(보조)",
            "보유 fact의 채널·매출·MS proxy는 변화 후보를 관찰하는 보조 근거이며, 원인·잠식·전환을 직접 증명하지 않습니다.",
            "",
            "### 미보유·확인필요",
            "| 단계 | 내용 |",
            "| --- | --- |",
            "| 1. 미보유 데이터 | 뉴스에 없는 외부 출시·정책·market expansion 및 내부 영업/채널 활동 원천 데이터는 확정하지 않습니다. |",
            "| 2. 현재 가능한 proxy | UBIST/IQVIA 매출·MS·순위, 반환된 채널/축별 fact, 실제 뉴스 fact의 정성 이슈입니다. |",
            "| 3. 해석 가능한 상한선 | proxy는 동기간 변화 후보를 보여줄 뿐 원인, 잠식, 직접 전환을 증명하지 않습니다. |",
            "| 4. 확인 필요 데이터 | 경쟁품 출시일, 정책/약가 이벤트, 채널별 콜·처방, 세그먼트별 처방량이 필요합니다. |",
            "| 5. 확보 시 수행할 분석 | 이벤트 전후 1~3개월을 대조군과 비교하고 채널·세그먼트별 uplift/lag를 추정합니다. |",
        )
    )


def _news_factor_rows(fact_md: str) -> tuple[NewsFactor, ...]:
    rows: list[NewsFactor] = []
    lines = fact_md.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("### ") and "뉴스/이슈" in stripped):
            continue
        for raw in lines[index + 1 :]:
            current = raw.strip()
            if current.startswith("### "):
                break
            if not current.startswith("|") or "---" in current or "날짜" in current:
                continue
            cells = _table_cells(current)
            if len(cells) < 6:
                continue
            date, title, source, url, summary, excerpt = cells[:6]
            if not title:
                continue
            basis_text = " ".join((title, summary, excerpt))
            category = _news_factor_category(basis_text)
            rows.append(
                NewsFactor(
                    category=category,
                    factor=_news_factor_label(category, basis_text),
                    title=title,
                    source=source or "뉴스",
                    url=url,
                    date=date,
                    summary=summary or excerpt,
                    direction=_news_factor_direction(basis_text),
                )
            )
    return tuple(rows[:6])


def _news_factor_category(text: str) -> str:
    internal_tokens = ("JW", "제이더블유", "JW중외", "리바로", "영업", "채널", "라인", "마케팅", "프로모션", "상기")
    return "Internal" if any(token in text for token in internal_tokens) else "External"


def _news_factor_label(category: str, text: str) -> str:
    if category == "Internal":
        if any(token in text for token in ("영업", "채널", "상기", "마케팅", "프로모션")):
            return "자사 영업/채널 뉴스"
        return "자사 제품/전략 뉴스"
    if any(token in text for token in ("정책", "약가", "급여", "보험")):
        return "정책/약가 뉴스"
    return "경쟁/시장 뉴스"


def _news_factor_direction(text: str) -> str:
    threat_tokens = ("경쟁 심화", "인하", "하락", "감소", "축소", "특허만료", "제네릭", "위협")
    opportunity_tokens = ("확대", "성장", "증가", "승인", "급여 확대", "기회")
    if any(token in text for token in threat_tokens):
        return "위협"
    if any(token in text for token in opportunity_tokens):
        return "기회"
    return "불확실"


def _news_factor_basis(row: NewsFactor) -> str:
    title = f"「{row.title}」"
    linked = f"[{title}]({row.url})" if row.url else title
    snippet = row.summary or "요약 미보유"
    return f"{row.source} {row.date} {linked} - {snippet}"


def _contract_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _axis_trend_so_what(rows: tuple[dict[str, str], ...]) -> str:
    if not rows:
        return ""
    parts: list[str] = []
    for row in rows[:3]:
        axis = row.get("axis") or "축"
        name = row.get("name") or ""
        delta = row.get("delta") or ""
        if not name or not delta:
            continue
        direction = "상승" if not delta.startswith("-") else "하락"
        parts.append(f"{name}({axis})는 {_pct_point_delta(delta)} {direction}으로 관찰됩니다")
    if not parts:
        return ""
    return " / ".join(parts) + ". 이 변화는 해당 축 proxy의 방향성만 의미하며, 미지원 축의 값을 대체하지 않습니다."


def _axis_trend_rows(fact_md: str) -> tuple[dict[str, str], ...]:
    rows: list[dict[str, str]] = []
    for line in fact_md.splitlines():
        stripped = line.strip().strip("|").strip()
        match = re.search(
            r"상위 (?P<axis>[^|:]+?) 추이\s*\|?\s*(?P<rank>\d+)위\s+(?P<name>.+?)\s+"
            r"(?P<from_period>20\d{2}-\d{2})\s+MS\s+(?P<from_share>-?\d+(?:\.\d+)?)%\s+→\s+"
            r"(?P<to_period>20\d{2}-\d{2})\s+MS\s+(?P<to_share>-?\d+(?:\.\d+)?)%.*?"
            r"점유율 변화\s+(?P<delta>[+-]?\d+(?:\.\d+)?)%p",
            stripped,
        )
        if match:
            rows.append(match.groupdict())
    return tuple(rows)


def _pct_point_delta(value: str) -> str:
    stripped = str(value or "").strip()
    if not stripped:
        return ""
    if stripped.endswith("%p"):
        return stripped
    if stripped.endswith("p"):
        return f"{stripped.removesuffix('p')}%p"
    if stripped.endswith("%"):
        return f"{stripped}p"
    return f"{stripped}%p"


def _number_from_text(value: str) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(match.group(0)) if match else 0.0
