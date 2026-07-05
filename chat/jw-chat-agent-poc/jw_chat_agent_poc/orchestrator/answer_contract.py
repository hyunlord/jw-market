from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Final, Mapping

from jw_chat_agent_poc.agent_loop.models import ToolCallPlan
from jw_chat_agent_poc.orchestrator.dosage_notes import DOSAGE_COMBINATION_NOTE_PREFIX, dosage_combination_note


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
}


def answer_contract_backfill_tool_calls(question: str, brand: str, calls: list[dict[str, Any]]) -> tuple[ToolCallPlan, ...]:
    """Return deterministic tool calls needed before final answer generation."""

    structural = _structural_contract_type(question)
    if structural in {"sales_activity_link", "change_drivers"} and not _has_brand_metric_fact(calls, brand):
        return (
            ToolCallPlan(
                name="get_metric",
                arguments={"brand": brand, "measure": "sales", "period": "latest"},
                reason="AnswerContract structural proxy backfill",
            ),
        )
    if _intent(question) != "ranking":
        return ()
    if _has_brand_metric_fact(calls, brand):
        return ()
    return (
        ToolCallPlan(
            name="get_metric",
            arguments={"brand": brand, "measure": "market_share", "period": "latest"},
            reason="AnswerContract ranking fact backfill",
        ),
    )


def enforce_answer_contract(question: str, answer: str, markdown_response: Mapping[str, Any] | None) -> str:
    """Repair final-model omissions when required facts already exist."""

    intent = _intent(question)
    fact_md = _fact_markdown(markdown_response)
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
    return _append_general_dosage_combination_note(_enforce_structural_contract(question, repaired, fact_md))


def evaluate_answer_contract(question: str, answer: str, markdown_response: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the contract status for trace metadata without mutating the answer."""

    intent = _intent(question)
    structural = _structural_contract_type(question)
    if intent is None:
        return {
            "intent": None,
            "structural_contract": structural,
            "status": "pass" if structural and _structural_contract_present(answer, structural) else "not_applicable",
        }
    rule = ANSWER_CONTRACT[intent]
    fact_md = _fact_markdown(markdown_response)
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


def _intent(question: str) -> str | None:
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
    rows = _key_value_section(fact_md, "출처 유형 fact")
    if not rows:
        return ""
    lines = ["## 출처"]
    for label, value in rows.items():
        lines.append(f"- {label}: {value}")
    return "\n".join(lines)


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
    if not block:
        return answer
    return _insert_before_source(answer, block)


def _structural_contract_type(question: str) -> str:
    text = question.lower()
    if _is_source_crosscheck_question(question):
        return "source_crosscheck"
    if _is_specialty_breakdown_question(question):
        return "specialty_breakdown"
    if _is_segment_compare_question(question):
        return "segment_compare"
    if any(token in question for token in ("영업활동", "영업 활동", "Impact", "impact", "상기 콜", "콜")):
        return "sales_activity_link"
    if any(token in question for token in ("Weekly", "Monthly", "Class", "Molecule", "용량", "제형")) and "추이" in question:
        return "trend_support_matrix"
    if any(token in question for token in ("변화 요인", "변화요인", "향후 예상", "Market expansion", "External", "Internal", "보건 정책")) or (
        "목표 시장" in question and any(token in question for token in ("출시", "정책", "Line extension", "채널", "변화"))
    ):
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


def _structural_contract_present(answer: str, contract_type: str) -> bool:
    markers = {
        "sales_activity_link": "## 영업-매출 연계 분석 설계",
        "segment_compare": "## 세그먼트 비교 지원 범위",
        "source_crosscheck": "## 출처별 교차 확인 범위",
        "specialty_breakdown": "## 진료과별 매출 구성",
        "trend_support_matrix": "## 추이 지원 범위",
        "change_drivers": "## 변화 요인 결론",
        "positioning": "## 포지셔닝 축",
        "threat_detection": "## 위협 요인",
        "news_ei": "## 뉴스 관련성 등급",
    }
    return markers.get(contract_type, "\0") in answer


def _is_positioning_question(question: str) -> bool:
    return any(token in question for token in ("포지셔닝", "차별점", "차별", "경쟁 대비 위치", "시장 내 위치", "positioning"))


def _is_threat_detection_question(question: str) -> bool:
    return any(token in question for token in ("위협", "리스크", "경쟁 위협", "threat", "risk"))


def _is_specialty_breakdown_question(question: str) -> bool:
    return "진료과" in question and any(token in question for token in ("별", "구성", "매출", "처방", "비중", "상위"))


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
    return "\n".join(
        (
            "## 영업-매출 연계 분석 설계",
            "| 필수 요소 | 답변 |",
            "| --- | --- |",
            "| 영업활동 데이터 보유 여부 | 현재 fact set에는 CSD 영업활동, 상기 콜, impact level 행이 없어 영업활동 데이터는 미보유로 처리합니다. |",
            "| 인과 검증 가능·불가능 | 매출·MS proxy는 확인되지만, 활동→처방·매출 인과는 이 데이터만으로 검증할 수 없습니다. |",
            f"| 매출 proxy 해석 | {proxy} |",
            "| 필요한 CSD 필드 | 콜 수, impact level, 기관·의사, 활동일, 제품·메시지, 처방 lag, 비활동 대조군이 필요합니다. |",
            "| 다음 분석 방법 | 활동 전후 1~3개월 매출·MS를 비활동군과 비교해 uplift와 lag를 추정합니다. |",
            "",
            "### 미보유 데이터 처리",
            "| 단계 | 내용 |",
            "| --- | --- |",
            "| 1. 미보유 데이터 | CSD 영업활동, 상기 콜, impact level, 기관·의사별 활동 원천 데이터입니다. |",
            f"| 2. 현재 가능한 proxy | {proxy} |",
            "| 3. 해석 가능한 상한선 | proxy는 매출·MS의 동기간 움직임만 보여주며, 영업활동 impact나 처방 인과를 증명하지 않습니다. |",
            "| 4. 확인 필요 데이터 | 콜 수, impact level, 기관·의사, 활동일, 제품·메시지, 처방 lag, 비활동 대조군이 필요합니다. |",
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
    return "\n".join(lines)


def _segment_compare_answer_payload(axis: str, answer: str) -> str:
    if not answer:
        return ""
    if axis == "제형":
        dosage_payload = _dosage_combination_payload_from_answer(answer)
        if dosage_payload:
            return dosage_payload
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
        if stripped.startswith("### "):
            in_dosage_table = "제형" in stripped or "성분 조합" in stripped
            continue
        if stripped.startswith("|") and ("제형" in stripped or "성분 조합" in stripped):
            in_dosage_table = True
            continue
        if not in_dosage_table or not stripped.startswith("|") or "---" in stripped or "제형" in stripped:
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
    mandatory = _mandatory_rows(fact_md)
    sources = _requested_sources(question)
    rows: list[tuple[str, str, str]] = []
    supported_count = 0
    for source in sources:
        supported = mandatory.get(f"{source} 보유", "")
        missing = mandatory.get(f"{source} 미보유", "")
        if supported:
            supported_count += 1
            rows.append((source, "보유", supported))
        elif missing:
            rows.append((source, "미보유", missing))
        else:
            rows.append((source, "미보유", f"{source} 출처는 이번 fact set에 조회 결과가 없어 값을 추정하지 않습니다."))
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
    return "\n".join(lines)


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
