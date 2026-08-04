from __future__ import annotations

from collections.abc import Mapping, Sequence

from jw_chat_agent_poc.service.file_sql_query import SqlFileSource, SqlQueryOutcome


def file_source_values(
    arguments: Mapping[str, object],
    raw: object,
) -> Mapping[str, object]:
    argument_sources = arguments.get("sources")
    sources = (
        tuple(argument_sources)
        if isinstance(argument_sources, Sequence)
        and not isinstance(argument_sources, (str, bytes, bytearray))
        else ()
    )
    result_items = raw.file_source_items if isinstance(raw, SqlQueryOutcome) else ()
    selected_item = result_items[0] if len(result_items) == 1 else None
    selected_source = _match_selected_source(sources, selected_item)
    values: dict[str, object] = {}

    if selected_item is not None:
        _set_selected_value(
            values,
            "file_id",
            _source_value(selected_item, "document_id"),
            "raw.file_source_items[0].document_id",
        )
        if "file_id" not in values and selected_source is not None:
            document_id = _source_value(selected_source, "document_id")
            if document_id not in (None, ""):
                _set_selected_value(
                    values,
                    "file_id",
                    document_id,
                    "arguments.sources[selected].document_id",
                )
            else:
                _set_selected_value(
                    values,
                    "file_id",
                    _source_value(selected_source, "logical_name"),
                    "arguments.sources[selected].logical_name",
                )
        for field, source_key in (("sheet", "sheet_name"), ("range", "range")):
            value = _source_value(selected_item, source_key)
            origin = f"raw.file_source_items[0].{source_key}"
            if value in (None, "") and selected_source is not None:
                value = _source_value(selected_source, source_key)
                origin = f"arguments.sources[selected].{source_key}"
            _set_selected_value(values, field, value, origin)
        return values

    for field, source_key in (
        ("file_id", "document_id"),
        ("sheet", "sheet_name"),
        ("range", "range"),
    ):
        result_values = _values_from_sources(result_items, source_key)
        argument_values = _values_from_sources(sources, source_key)
        if field == "file_id" and not argument_values:
            argument_values = _values_from_sources(sources, "logical_name")
            source_key = "logical_name"
        candidate_values = result_values or argument_values
        if candidate_values:
            values[field] = candidate_values
            origin = "raw.file_source_items" if result_values else "arguments.sources"
            values[f"{field}_source"] = f"{origin}[*].{source_key}"
    return values


def _match_selected_source(
    sources: Sequence[object],
    selected_item: object | None,
) -> object | None:
    if selected_item is None:
        return sources[0] if len(sources) == 1 else None
    for key in ("document_id", "file_name"):
        selected_value = _source_value(selected_item, key)
        if selected_value in (None, ""):
            continue
        matches = tuple(
            source for source in sources if _source_value(source, key) == selected_value
        )
        if len(matches) == 1:
            return matches[0]
    return sources[0] if len(sources) == 1 else None


def _set_selected_value(
    values: dict[str, object],
    field: str,
    value: object | None,
    source: str,
) -> None:
    if value in (None, "", (), []):
        return
    values[field] = value
    values[f"{field}_source"] = source


def _source_value(source: object, key: str) -> object | None:
    match source:
        case SqlFileSource():
            return getattr(source, key, None)
        case Mapping():
            return source.get(key)
        case _:
            return None


def _values_from_sources(
    sources: Sequence[object],
    key: str,
) -> tuple[object, ...]:
    values: list[object] = []
    for source in sources:
        value = _source_value(source, key)
        if value not in (None, "", (), []) and value not in values:
            values.append(value)
    return tuple(values)
