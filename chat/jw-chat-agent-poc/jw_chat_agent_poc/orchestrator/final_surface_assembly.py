from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jw_chat_agent_poc.orchestrator.query_spec import QueryOperation, RequestQuerySpec


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FinalSurfaceAssemblyResult:
    answer: str
    actions: tuple[str, ...] = ()
    failed_open: bool = False


def apply_final_surface_assembly(
    question: str,
    answer: str,
    spec: RequestQuerySpec | None,
    *,
    markdown_response: Mapping[str, Any] | None = None,
) -> FinalSurfaceAssemblyResult:
    """Select the requested answer shape without mutating the underlying facts."""

    if spec is None or not answer.strip():
        return FinalSurfaceAssemblyResult(answer)
    try:
        return _assemble(question, answer, spec, markdown_response=markdown_response)
    except Exception:  # noqa: BLE001 - presentation reduction must never hide the original answer
        LOGGER.exception("final_surface_assembly_failed_open")
        return FinalSurfaceAssemblyResult(answer, failed_open=True)


def _assemble(
    question: str,
    answer: str,
    spec: RequestQuerySpec,
    *,
    markdown_response: Mapping[str, Any] | None,
) -> FinalSurfaceAssemblyResult:
    del markdown_response  # Facts remain owned by the caller; this layer only selects answer text.

    hira = _concise_hira_criteria(question, answer)
    if hira != answer:
        return FinalSurfaceAssemblyResult(hira, ("omit_hira_raw_page_dump",))

    positioning = _prioritize_positioning(question, answer)
    if positioning != answer:
        return FinalSurfaceAssemblyResult(positioning, ("prioritize_positioning_conclusion",))

    if spec.operation is QueryOperation.COMPARE_CURRENT:
        comparison = _lead_current_comparison(answer)
        if comparison != answer:
            return FinalSurfaceAssemblyResult(comparison, ("lead_current_comparison",))

    if spec.operation is QueryOperation.CURRENT_VALUE:
        concise = _concise_current_value(answer, spec)
        if concise != answer:
            return FinalSurfaceAssemblyResult(concise, ("concise_current_value",))

    return FinalSurfaceAssemblyResult(answer)


def _concise_current_value(answer: str, spec: RequestQuerySpec) -> str:
    if len(spec.entities) != 1 or not spec.metrics:
        return answer
    if not _has_current_value_over_inclusion(answer):
        return answer

    values = _metric_table(answer) or _latest_series_values(answer)
    requested = tuple(
        (label, values.get(label, ""))
        for metric, label in (("sales", "매출"), ("share", "시장점유율"))
        if metric in spec.metrics
    )
    if not requested or any(not value for _, value in requested):
        return answer

    period = values.get("기간", "")
    if not period:
        return answer
    brand = spec.entities[0].display_name
    lead = _current_value_lead(brand, period, requested)
    rows = (("기간", period), *requested)
    table = "\n".join(
        (
            "| 지표 | 값 |",
            "| --- | --- |",
            *(f"| {label} | {value} |" for label, value in rows),
        )
    )
    source = _source_and_notices(answer)
    return _join_blocks(lead, table, source)


def _has_current_value_over_inclusion(answer: str) -> bool:
    return any(
        marker in answer
        for marker in (
            "매출 시계열",
            "### 분석 기준별 점유율",
            "## 일반뷰 (ATC4)",
        )
    ) or answer.count("## 전략뷰") > 1


def _metric_table(answer: str) -> dict[str, str]:
    lines = answer.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "### 지표":
            continue
        values: dict[str, str] = {}
        for raw in lines[index + 1 :]:
            stripped = raw.strip()
            if stripped.startswith("#") or stripped.startswith("**"):
                break
            cells = _table_cells(stripped)
            if len(cells) != 2 or cells[0] in {"지표", "---"}:
                continue
            if set(cells[0]) == {"-"}:
                continue
            values[cells[0]] = cells[1]
        return values
    return {}


def _latest_series_values(answer: str) -> dict[str, str]:
    lines = answer.splitlines()
    for index, line in enumerate(lines):
        if not re.fullmatch(r"\*\*.+\s매출 시계열\*\*", line.strip()):
            continue
        rows: list[tuple[str, ...]] = []
        for raw in lines[index + 1 :]:
            cells = _table_cells(raw)
            if not cells:
                if rows:
                    break
                continue
            if cells[0] == "기간" or _separator_row(cells):
                continue
            if len(cells) >= 3:
                rows.append(cells)
        if rows:
            period, sales, share = rows[-1][:3]
            return {"기간": period, "매출": sales, "시장점유율": share}
    return {}


def _current_value_lead(
    brand: str,
    period: str,
    requested: tuple[tuple[str, str], ...],
) -> str:
    values = dict(requested)
    if tuple(values) == ("매출", "시장점유율"):
        return (
            f"{brand}의 {period} 매출은 {values['매출']}이고 "
            f"시장점유율은 {values['시장점유율']}입니다."
        )
    label, value = requested[0]
    return f"{brand}의 {period} {label}은 {value}입니다."


def _lead_current_comparison(answer: str) -> str:
    table = _heading_table(answer, "## 브랜드 비교")
    if table is None:
        return answer
    block, header, rows = table
    try:
        brand_index = header.index("브랜드")
        sales_index = header.index("최신 매출")
    except ValueError:
        return answer
    values = tuple(
        (row[brand_index], row[sales_index])
        for row in rows
        if len(row) > max(brand_index, sales_index)
        and row[brand_index]
        and row[sales_index]
        and "제외" not in row[sales_index]
    )
    if len(values) < 2:
        return answer
    rendered_values = ", ".join(f"{brand} {value}" for brand, value in values)
    lead = f"최신 매출은 {rendered_values}입니다."
    if answer.lstrip().startswith(lead):
        return answer
    return _join_blocks(lead, block, _source_and_notices(answer))


def _prioritize_positioning(question: str, answer: str) -> str:
    if "경쟁" not in question or not any(
        token in question for token in ("위치", "포지셔닝")
    ):
        return answer
    direct = next(
        (line.strip() for line in answer.splitlines() if line.strip().startswith("자사 위치:")),
        "",
    )
    positioning = _markdown_section(answer, "## 포지셔닝 축")
    competitors = _table_with_header(answer, ("순위", "브랜드", "점유율", "매출"))
    if not direct or not positioning or not competitors:
        return answer
    positioning = "\n".join(
        line for line in positioning.splitlines() if line.strip() != direct
    ).strip()
    return _join_blocks(direct, competitors, positioning, _source_and_notices(answer))


def _concise_hira_criteria(question: str, answer: str) -> str:
    if "급여기준" not in question or "심사평가원(HIRA) 보험인정기준" not in answer:
        return answer
    raw_marker = re.search(
        r"(?m)^<?\s*건강보험심사평가원 보험인정기준 상세내용 인쇄\s*$",
        answer,
    )
    if raw_marker is None:
        return answer
    source_marker = re.search(r"(?m)^## 출처\s*$", answer[raw_marker.end() :])
    if source_marker is None:
        return answer
    source_start = raw_marker.end() + source_marker.start()
    source = _source_and_notices(answer[source_start:])
    if not source:
        return answer
    before = answer[: raw_marker.start()].rstrip()
    before = re.sub(r"\n---\s*$", "", before).rstrip()
    kept_lines = tuple(
        line
        for line in before.splitlines()
        if "상세 고시 원문" not in line
        and not line.strip().startswith("집계 데이터는")
    )
    summary = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    if not summary:
        return answer
    return _join_blocks(summary, source)


def _heading_table(
    answer: str,
    heading: str,
) -> tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]] | None:
    lines = answer.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return None
    table_start = next(
        (index for index in range(start + 1, len(lines)) if _table_cells(lines[index])),
        None,
    )
    if table_start is None:
        return None
    table_end = table_start
    parsed: list[tuple[str, ...]] = []
    while table_end < len(lines):
        cells = _table_cells(lines[table_end])
        if not cells:
            break
        parsed.append(cells)
        table_end += 1
    if len(parsed) < 3:
        return None
    header = parsed[0]
    rows = tuple(row for row in parsed[2:] if not _separator_row(row))
    block = "\n".join(lines[start:table_end]).strip()
    return block, header, rows


def _table_with_header(answer: str, header: tuple[str, ...]) -> str:
    lines = answer.splitlines()
    for index, line in enumerate(lines):
        if _table_cells(line) != header:
            continue
        end = index
        while end < len(lines) and _table_cells(lines[end]):
            end += 1
        return "\n".join(lines[index:end]).strip()
    return ""


def _markdown_section(answer: str, heading: str) -> str:
    lines = answer.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return ""
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[index].strip())
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def _source_and_notices(answer: str) -> str:
    lines = answer.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == "## 출처")
    except StopIteration:
        return ""
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.match(r"^#{1,2}\s+", lines[index].strip())
        ),
        len(lines),
    )
    source = "\n".join(lines[start:end]).strip()
    safety_sections = _safety_sections(answer, excluded_range=(start, end))
    notices = tuple(
        line.strip()
        for index, line in enumerate(lines)
        if not start <= index < end
        and (
            _safety_notice_line(line.strip())
            or _source_measurement_basis_line(line.strip())
        )
    )
    return _join_blocks(
        source,
        *safety_sections,
        "\n".join(dict.fromkeys(notices)),
    )


def _source_measurement_basis_line(line: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:원외 처방\(UBIST\)|제조사 출하\(IQVIA NSA\)) 기준으로 답합니다\.",
            line,
        )
    )


def _safety_sections(
    answer: str,
    *,
    excluded_range: tuple[int, int],
) -> tuple[str, ...]:
    lines = answer.splitlines()
    excluded_start, excluded_end = excluded_range
    sections: list[str] = []
    for index, line in enumerate(lines):
        if excluded_start <= index < excluded_end:
            continue
        match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if match is None or not _safety_text(match.group(2)):
            continue
        level = len(match.group(1))
        end = next(
            (
                cursor
                for cursor in range(index + 1, len(lines))
                if (
                    (heading := re.match(r"^(#{1,6})\s+", lines[cursor].strip()))
                    and len(heading.group(1)) <= level
                )
            ),
            len(lines),
        )
        sections.append("\n".join(lines[index:end]).strip())
    return tuple(dict.fromkeys(section for section in sections if section))


def _safety_notice_line(line: str) -> bool:
    if not line or line.startswith("#") or line.startswith("|"):
        return False
    if not (
        line.startswith("-")
        or line.startswith("근거의 기준 기간")
        or line.startswith("근거 정합을")
    ):
        return False
    return _safety_text(line)


def _safety_text(text: str) -> bool:
    return any(
        token in text
        for token in (
            "주의",
            "제한",
            "검증",
            "확인할 수",
            "조회할 수 없",
            "추정하지",
            "근거 정합",
            "근거 불일치",
            "부분 결과",
        )
    )


def _table_cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return ()
    return tuple(cell.strip() for cell in stripped.strip("|").split("|"))


def _separator_row(row: tuple[str, ...]) -> bool:
    return bool(row) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in row)


def _join_blocks(*blocks: str) -> str:
    return "\n\n".join(block.strip() for block in blocks if block and block.strip()).strip()
