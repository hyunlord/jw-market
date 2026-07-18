from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from jw_chat_agent_poc.orchestrator.provenance_model import (
    ProvenanceRow,
    dedupe_rows,
    normalized_row,
    period_range,
    period_tokens,
    public_market,
    public_source,
    public_view,
)


_FILE_NAME_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\s*\[\d+\]\s+([^\n]+)", re.MULTILINE),
    re.compile(r"^\s*파일\s*:\s*([^\n]+)", re.MULTILINE),
    re.compile(r"^\s*업로드\s*파일\s+([^\s:]+)", re.MULTILINE),
    re.compile(r"^\s*(?:filename|file_name)\s*[:=]\s*([^\n]+)", re.MULTILINE | re.IGNORECASE),
)
_EXTERNAL_BULLET_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*[-*]\s+.+?\[(?P<label>ClinicalTrials\.gov 임상시험 정보|식약처 의약품 정보|"
    r"(?:OpenFDA|FDA)[^\]]*|HIRA[^\]]*|건강보험심사평가원[^\]]*|웹 검색 결과)\]\s*$",
    re.IGNORECASE,
)
_MARKDOWN_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def provenance_rows_from_fact_markdown(fact_md: str) -> tuple[ProvenanceRow, ...]:
    rows = _rows_from_seven_field_fact(fact_md)
    if not rows:
        rows = _rows_from_legacy_value_fact(fact_md)
    if not rows:
        rows = _rows_from_legacy_source_fact(fact_md)
    rows = _replace_generic_external_rows(rows, _rows_from_external_facts(fact_md))
    return dedupe_rows(rows)


def provenance_rows_from_file_context(file_context: str) -> tuple[ProvenanceRow, ...]:
    if not file_context.strip():
        return ()
    sql_marker = "## 업로드 파일 SQL 결과"
    provenance_context = (
        file_context.split(sql_marker, 1)[1]
        if sql_marker in file_context
        else file_context
    )
    filenames = tuple(
        dict.fromkeys(
            match.group(1).strip()
            for pattern in _FILE_NAME_PATTERNS
            for match in pattern.finditer(provenance_context)
            if match.group(1).strip()
        )
    )
    if not filenames:
        filenames = ("",)
    if sql_marker in file_context:
        return tuple(
            ProvenanceRow(source=f"업로드 파일({filename})" if filename else "업로드 파일")
            for filename in filenames
        )
    return tuple(
        ProvenanceRow(
            source=f"업로드 파일({filename})" if filename else "업로드 파일",
            view="파일",
        )
        for filename in filenames
    )


def provenance_row_from_file_context(file_context: str) -> ProvenanceRow | None:
    rows = provenance_rows_from_file_context(file_context)
    return rows[0] if rows else None


def _rows_from_seven_field_fact(fact_md: str) -> list[ProvenanceRow]:
    rows: list[ProvenanceRow] = []
    for cells in _table_records(fact_md, "provenance fact"):
        if len(cells) < 7:
            continue
        rows.append(normalized_row(*cells[:7]))
    return rows


def _rows_from_legacy_value_fact(fact_md: str) -> list[ProvenanceRow]:
    rows: list[ProvenanceRow] = []
    for cells in _table_records(fact_md, "수치별 출처 fact"):
        padded = [*cells[:6], *("" for _ in range(max(0, 6 - len(cells))))]
        _value, source, period, market, _axis, _tool_call_id = padded[:6]
        rows.append(
            normalized_row(
                source=public_source(source),
                period=period,
                view=public_view("", market),
                market=public_market("", market),
            )
        )
    return rows


def _rows_from_legacy_source_fact(fact_md: str) -> list[ProvenanceRow]:
    rows: list[ProvenanceRow] = []
    for cells in _table_records(fact_md, "출처 유형 fact"):
        label = cells[0] if cells else ""
        detail = cells[1] if len(cells) > 1 else ""
        source = _legacy_source_label(label, detail)
        if not source:
            continue
        periods = sorted(set(re.findall(r"20\d{2}-(?:\d{2}|Q[1-4])", detail)))
        raw_market = _legacy_market(detail)
        denominator_match = re.search(r"분모\s+(\d[\d,]*)", detail)
        rows.append(
            normalized_row(
                source=source,
                period=period_range(periods),
                view=public_view(detail, raw_market),
                market=public_market(raw_market, raw_market),
                denominator=denominator_match.group(1) if denominator_match else "",
            )
        )
    return rows


def _rows_from_external_facts(fact_md: str) -> list[ProvenanceRow]:
    rows: list[ProvenanceRow] = []
    for cells in _table_records(fact_md, "인사이트 근거 fact - 뉴스/이슈"):
        if len(cells) < 3:
            continue
        date, title, outlet = cells[:3]
        url = cells[3] if len(cells) > 3 else ""
        citation = f"뉴스/이슈 · {outlet or '뉴스'}"
        if title:
            citation = f"{citation} 「{title}」"
        if url:
            citation = f"{citation} {url}"
        rows.append(normalized_row(source=citation, period=date))
    rows.extend(_rows_from_external_tool_bullets(fact_md))
    return _external_source_rows(fact_md, rows)


def _rows_from_external_tool_bullets(fact_md: str) -> list[ProvenanceRow]:
    rows: list[ProvenanceRow] = []
    for line in fact_md.splitlines():
        match = _EXTERNAL_BULLET_LABEL_RE.match(line)
        if match is None:
            continue
        label = match.group("label")
        label_fold = label.casefold()
        if label_fold.startswith("clinicaltrials.gov"):
            source = "ClinicalTrials.gov"
        elif label == "식약처 의약품 정보":
            source = label
        elif label_fold.startswith(("openfda", "fda")):
            source = label
        elif "hira" in label_fold or "건강보험심사평가원" in label:
            source = "HIRA 질병정보서비스"
        else:
            links = _MARKDOWN_LINK_RE.findall(line)
            title, url = links[-1] if links else ("", "")
            source = "뉴스/이슈"
            if title:
                source = f"{source} 「{title}」"
            if url:
                source = f"{source} {url}"
        rows.append(normalized_row(source=source, period=period_range(period_tokens(line))))
    return rows


def _external_source_rows(fact_md: str, rows: list[ProvenanceRow]) -> list[ProvenanceRow]:
    has_news_citation = bool(rows)
    hira_details: list[str] = []
    for cells in _table_records(fact_md, "출처 유형 fact"):
        label = cells[0] if cells else ""
        detail = cells[1] if len(cells) > 1 else ""
        if label not in {"뉴스 검색", "외부 HIRA", "외부 API"} or not detail:
            continue
        if label == "외부 HIRA":
            hira_details.append(detail)
            continue
        if label == "뉴스 검색":
            if not has_news_citation:
                rows.append(normalized_row(source="뉴스/이슈"))
            continue
        periods = sorted(set(period_tokens(detail)))
        rows.append(normalized_row(source=f"외부 API · {detail}", period=period_range(periods)))
    if hira_details:
        conditions = [detail.split(" — ", 1)[1] for detail in hira_details if " — " in detail]
        public_condition = next((condition for condition in conditions if re.search(r"\b[A-Z]\d{2}\b", condition)), "")
        source = "HIRA 질병정보서비스"
        if public_condition:
            source = f"{source} · {public_condition.split(' — ', 1)[0]}"
        periods = sorted({period for detail in hira_details for period in period_tokens(detail)})
        rows.append(normalized_row(source=source, period=period_range(periods), unit="명"))
    return rows


def _replace_generic_external_rows(
    rows: Sequence[ProvenanceRow],
    external_rows: Sequence[ProvenanceRow],
) -> list[ProvenanceRow]:
    if not external_rows:
        return list(rows)
    detailed_families = {_source_family(row.source) for row in external_rows}
    kept = [
        row
        for row in rows
        if row.source.casefold() not in {"external", "외부 api"}
        and _source_family(row.source) not in detailed_families
    ]
    return [*kept, *external_rows]


def _source_family(source: str) -> str:
    if source.startswith("뉴스/이슈"):
        return "news"
    if source.startswith("HIRA"):
        return "hira"
    if source.startswith("외부 API"):
        return "external_api"
    return source


def _table_records(markdown: str, title_fragment: str) -> list[list[str]]:
    records: list[list[str]] = []
    in_section = False
    header_seen = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            in_section = title_fragment in stripped
            header_seen = False
            continue
        if not in_section or not stripped.startswith("|"):
            continue
        cells = [cell.strip().replace("\\|", "|") for cell in stripped.strip("|").split("|")]
        if not header_seen:
            header_seen = True
            continue
        if cells and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        records.append(cells)
    return records


def _legacy_source_label(label: str, detail: str) -> str:
    if label == "데이터 상세":
        first = re.split(r"\s+—\s+", detail, maxsplit=1)[0]
        return " / ".join(dict.fromkeys(public_source(part.strip()) for part in first.split("/") if part.strip()))
    if label == "뉴스 검색":
        return "뉴스/이슈"
    if label == "외부 HIRA":
        return "HIRA"
    if label == "외부 API":
        return "외부 API"
    return public_source(label) if label else ""


def _legacy_market(detail: str) -> str:
    match = re.search(r"시장:\s*([^,(—]+)", detail)
    return match.group(1).strip() if match else ""
