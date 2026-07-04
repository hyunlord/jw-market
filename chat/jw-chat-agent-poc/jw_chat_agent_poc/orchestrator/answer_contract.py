from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Final, Mapping

from jw_chat_agent_poc.agent_loop.models import ToolCallPlan


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
    return _enforce_structural_contract(question, repaired, fact_md)


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
    value = markdown_response.get("fact_md") or markdown_response.get("data_md") or ""
    return value if isinstance(value, str) else ""


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
    if contract_type == "trend_support_matrix":
        answer = _repair_split_market_support(answer, fact_md)
    if _structural_contract_present(answer, contract_type):
        return answer
    block = ""
    if contract_type == "sales_activity_link":
        answer = _sanitize_sales_activity_answer(answer, fact_md)
        block = _sales_activity_contract_block(fact_md)
    elif contract_type == "trend_support_matrix":
        block = _trend_support_matrix_block(fact_md)
    elif contract_type == "change_drivers":
        block = _change_drivers_contract_block(fact_md)
    if not block:
        return answer
    return _insert_before_source(answer, block)


def _structural_contract_type(question: str) -> str:
    text = question.lower()
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
    return ""


def _structural_contract_present(answer: str, contract_type: str) -> bool:
    markers = {
        "sales_activity_link": "## 영업-매출 연계 분석 설계",
        "trend_support_matrix": "## 추이 지원 범위",
        "change_drivers": "## 변화 요인 결론",
    }
    return markers.get(contract_type, "\0") in answer


def _insert_before_source(answer: str, block: str) -> str:
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


def _change_drivers_contract_block(fact_md: str) -> str:
    trend = _trend_fact(fact_md)
    proxy = _sales_activity_proxy_text(trend) if trend is not None else "보유 정량 fact 범위에서 매출·MS·채널 proxy만 확인 가능합니다."
    news_rows = _news_factor_rows(fact_md)
    conclusion = (
        f"반환된 뉴스 {len(news_rows)}건과 보유 UBIST/IQVIA proxy만으로 변화 요인을 분류했습니다."
        if news_rows
        else "반환된 뉴스 fact가 없어 보유 UBIST/IQVIA proxy와 미보유 항목을 분리해 표시합니다."
    )
    table_rows = [
        "| 구분(E/I) | 요인 | 근거(뉴스명·인용 or UBIST or 미보유) | 영향방향 | 확인 필요 데이터 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in news_rows:
        table_rows.append(
            f"| {_contract_cell(row.category)} | {_contract_cell(row.factor)} | {_contract_cell(_news_factor_basis(row))} | {_contract_cell(row.direction)} | 기사 원문, 동일 기간 처방·매출 변동 |"
        )
    table_rows.extend(
        (
            "| External | 정책/약가 변화 | 미보유 | 불확실 | 정책 변경일, 약가/급여 변화, 경쟁품 처방 변화 |",
            f"| Internal | 자사 영업/채널 활동 | UBIST proxy: {proxy} | 불확실 | 채널별 활동량, 세그먼트별 처방량 |",
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
