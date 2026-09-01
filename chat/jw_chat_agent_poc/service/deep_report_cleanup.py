from __future__ import annotations

import re


_MISSING_CELLS = frozenset({"", "-", "—", "–", "해당 없음", "n/a", "na", "null"})
_GENERIC_SOURCES = frozenset({"외부 api", "external api", "external"})


def repair_plain_table_urls(markdown: str) -> str:
    """Keep URL-valued Markdown table cells atomic."""

    repaired: list[str] = []
    for line in markdown.splitlines():
        if not _is_table_line(line):
            repaired.append(line)
            continue
        cells = _table_cells(line)
        compacted = tuple(_compact_url_cell(cell) for cell in cells)
        repaired.append(_render_table_row(compacted))
    return "\n".join(repaired)


def slim_source_tables(source_section: str) -> str:
    """Remove empty source rows and columns while preserving factual cells."""

    lines = source_section.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        if index + 1 >= len(lines) or not _is_table_header(lines[index], lines[index + 1]):
            output.append(lines[index])
            index += 1
            continue
        end = index + 2
        while end < len(lines) and _is_table_line(lines[end]):
            end += 1
        output.extend(_slim_table(lines[index:end]))
        index = end
    return "\n".join(output).strip()


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _is_table_header(header: str, separator: str) -> bool:
    if not _is_table_line(header) or not _is_table_line(separator):
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in _table_cells(separator))


def _table_cells(line: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in line.strip().strip("|").split("|"))


def _compact_url_cell(cell: str) -> str:
    stripped = cell.strip()
    if not stripped.startswith(("http://", "https://")):
        return cell
    return re.sub(r"\s+", "", stripped)


def _is_missing(cell: str) -> bool:
    return cell.strip().casefold() in _MISSING_CELLS


def _is_empty_source_row(cells: tuple[str, ...]) -> bool:
    if not cells or all(_is_missing(cell) for cell in cells):
        return True
    return cells[0].strip().casefold() in _GENERIC_SOURCES and all(_is_missing(cell) for cell in cells[1:])


def _slim_table(lines: list[str]) -> list[str]:
    header = _table_cells(lines[0])
    rows = tuple(
        cells
        for cells in (_table_cells(line) for line in lines[2:])
        if len(cells) == len(header) and not _is_empty_source_row(cells)
    )
    if not rows:
        return []
    kept_columns = tuple(
        index
        for index in range(len(header))
        if index == 0 or any(not _is_missing(row[index]) for row in rows)
    )
    projected_header = tuple(header[index] for index in kept_columns)
    projected_rows = tuple(tuple(row[index] for index in kept_columns) for row in rows)
    return [
        _render_table_row(projected_header),
        _render_table_row(tuple("---" for _ in kept_columns)),
        *(_render_table_row(row) for row in projected_rows),
    ]


def _render_table_row(cells: tuple[str, ...]) -> str:
    return f"| {' | '.join(cells)} |"
