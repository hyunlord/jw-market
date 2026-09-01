from __future__ import annotations

from dataclasses import dataclass

from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer


_PORTFOLIO_FACT_HEADING = "### JW 주요 브랜드 포트폴리오 fact"
_SUMMARY_TABLE_HEADER = "| 브랜드 | 기간 | MS 변화 | 최신 매출 | 동시장 상승 후보 |"


@dataclass(frozen=True, slots=True)
class _PortfolioDeclineRow:
    brand: str
    period: str
    ms_path: str
    ms_delta: str
    latest_sales: str
    top_gainers: str


def ensure_portfolio_decline_summary(answer: str, fact_md: str) -> str:
    """Render multi-brand portfolio decline facts as one summary and one table."""

    rows = _portfolio_decline_rows(fact_md)
    if len(rows) < 2:
        return answer
    cleaned = _remove_portfolio_decline_prefix_lines(answer)
    if _SUMMARY_TABLE_HEADER in cleaned:
        return cleanup_markdown_answer(cleaned)
    block = _portfolio_decline_block(rows)
    return cleanup_markdown_answer("\n\n".join(part for part in (cleaned.strip(), block) if part))


def _portfolio_decline_rows(fact_md: str) -> tuple[_PortfolioDeclineRow, ...]:
    lines = fact_md.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == _PORTFOLIO_FACT_HEADING), -1)
    if start < 0:
        return ()
    rows: list[_PortfolioDeclineRow] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("### "):
            break
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = tuple(cell.strip() for cell in stripped.strip("|").split("|"))
        if cells[:2] == ("브랜드", "시장"):
            continue
        row = _portfolio_decline_row_from_cells(cells)
        if row is not None:
            rows.append(row)
    return tuple(rows)


def _portfolio_decline_row_from_cells(cells: tuple[str, ...]) -> _PortfolioDeclineRow | None:
    if len(cells) < 7:
        return None
    brand, _market, period, ms_path, ms_delta, latest_sales, top_gainers = cells[:7]
    if not brand or not period or not ms_path or not ms_delta:
        return None
    return _PortfolioDeclineRow(
        brand=brand,
        period=period,
        ms_path=ms_path,
        ms_delta=ms_delta,
        latest_sales=latest_sales,
        top_gainers=top_gainers,
    )


def _remove_portfolio_decline_prefix_lines(answer: str) -> str:
    kept = [line for line in answer.splitlines() if "포트폴리오 MS 하락:" not in line]
    return cleanup_markdown_answer("\n".join(kept))


def _portfolio_decline_block(rows: tuple[_PortfolioDeclineRow, ...]) -> str:
    leader = min(rows, key=_portfolio_decline_delta_value)
    period = _shared_portfolio_period(rows)
    intro_parts = [_leader_sentence(leader, period)]
    livaro = next((row for row in rows if row.brand == "리바로" and row.top_gainers), None)
    if livaro is not None:
        intro_parts.append(f"리바로 행은 동시장 상승 후보에 {livaro.top_gainers}를 포함합니다.")
    intro_parts.append("동시장 상승 후보는 관측 후보이며 직접 인과나 처방 이동은 단정하지 않습니다.")
    table_lines = [
        "### JW 주요 브랜드 MS 하락 요약",
        _SUMMARY_TABLE_HEADER,
        "| --- | --- | --- | ---: | --- |",
    ]
    for row in rows:
        table_lines.append(f"| {row.brand} | {row.period} | {row.ms_path} ({row.ms_delta}) | {row.latest_sales} | {row.top_gainers or '-'} |")
    return "\n\n".join((" ".join(intro_parts), "\n".join(table_lines)))


def _leader_sentence(row: _PortfolioDeclineRow, period: str) -> str:
    if period:
        return f"{period} 기준 {row.brand}가 {row.ms_delta}로 가장 크게 하락했습니다."
    return f"{row.brand}가 {row.ms_delta}로 가장 크게 하락했습니다."


def _portfolio_decline_delta_value(row: _PortfolioDeclineRow) -> float:
    try:
        return float(row.ms_delta.replace("%p", "").replace("%", "").strip())
    except ValueError:
        return 0.0


def _shared_portfolio_period(rows: tuple[_PortfolioDeclineRow, ...]) -> str:
    periods = tuple(dict.fromkeys(row.period for row in rows if row.period))
    if len(periods) == 1:
        return periods[0]
    return ""
