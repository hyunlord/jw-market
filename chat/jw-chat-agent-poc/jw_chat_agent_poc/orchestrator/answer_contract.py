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

    if _intent(question) != "ranking":
        return ()
    if _has_ranking_fact(calls, brand):
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
    if intent is None:
        return answer
    rule = ANSWER_CONTRACT[intent]
    fact_md = _fact_markdown(markdown_response)
    if not fact_md:
        return answer
    if intent == "ranking":
        fact = _ranking_fact(fact_md)
        if fact is None:
            return answer
        if _ranking_surface_ok(answer, fact, rule):
            return answer
        return _join_blocks(_ranking_answer(fact), _source_block(fact_md))
    if intent == "trend":
        fact = _trend_fact(fact_md)
        if fact is None or len(fact.rows) < rule.min_rows:
            return answer
        if _trend_surface_ok(answer, fact, rule):
            return answer
        return _join_blocks(_trend_answer(fact), _source_block(fact_md))
    return answer


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
    for call in calls:
        if call.get("tool") != "get_brand_metric":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        if data.get("brand") != brand:
            continue
        if data.get("rank") is not None and (data.get("sales_krw") is not None or data.get("sales_억원") is not None):
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
