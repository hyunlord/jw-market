from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import requests

from jw_chat_agent_poc.genos_config import (
    resolve_planner_genos_base_url,
    resolve_planner_genos_token,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SqlFileSource:
    logical_name: str
    file_name: str
    sheet_name: str
    document_id: int | None = None
    row_count: int | None = None
    column_count: int | None = None


@dataclass(frozen=True, slots=True)
class SqlQueryOutcome:
    file_context: str
    file_source_items: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    answer_md: str = ""


def query_uploaded_sql(
    question: str,
    conversation_id: str,
    sources: Sequence[SqlFileSource],
) -> SqlQueryOutcome:
    """Plan and execute one read-only query against session-owned file data."""

    if not sources:
        return SqlQueryOutcome("", (), ())
    try:
        schemas = tuple(
            _fetch_schema(source, conversation_id)
            for source in sources[: _max_schema_tables()]
        )
        if _is_schema_question(question):
            answer = _render_schema_answer(question, sources, schemas)
            return SqlQueryOutcome(
                file_context=answer,
                file_source_items=_source_items(sources[: len(schemas)]),
                errors=(),
                answer_md=answer,
            )
        plan = _generate_select(question, schemas)
        if not plan:
            return SqlQueryOutcome("", (), ())
        logical_name = str(plan.get("logical_name") or "").strip()
        sql = str(plan.get("sql") or "").strip()
        source = next(
            (item for item in sources if item.logical_name == logical_name),
            None,
        )
        if source is None or not _is_select_only_candidate(sql):
            raise ValueError("planner returned an invalid scoped file query")
        aggregate = _is_aggregate_question(question)
        if aggregate and not _has_aggregate_contract(sql):
            return _aggregate_contract_failure()
        result = _run_query(conversation_id, logical_name, sql)
        schema = next(
            (
                item for item in schemas
                if str(item.get("logical_name") or "").strip() == logical_name
            ),
            {},
        )
        context = _render_result(source, result, schema)
        answer = ""
        if aggregate:
            answer = _render_aggregate_answer(question, source, sql, result, schema)
            if not answer:
                return _aggregate_contract_failure()
        source_item: dict[str, Any] = {"file_name": source.file_name}
        if source.document_id is not None:
            source_item["document_id"] = source.document_id
        return SqlQueryOutcome(
            file_context=context,
            file_source_items=(source_item,),
            errors=(),
            answer_md=answer,
        )
    except (requests.RequestException, ValueError, TypeError, KeyError, RuntimeError) as exc:
        logger.exception(
            "file SQL query failed conversation_id=%s logical_names=%s reason=%s",
            conversation_id,
            [source.logical_name for source in sources],
            exc,
        )
        return SqlQueryOutcome(
            file_context=(
                "## 업로드 파일 SQL 결과\n"
                "상태: 확인불가\n"
                "업로드 파일 SQL 질의를 실행하지 못해 요청한 값을 확인할 수 없습니다."
            ),
            file_source_items=(),
            errors=("file SQL query unavailable",),
            answer_md=(
                "업로드 파일 집계 결과를 확인할 수 없습니다. "
                "파일 SQL 조회가 완료되지 않았습니다."
            ),
        )


def _source_items(sources: Sequence[SqlFileSource]) -> tuple[dict[str, Any], ...]:
    items: list[dict[str, Any]] = []
    for source in sources:
        item: dict[str, Any] = {"file_name": source.file_name}
        if source.document_id is not None:
            item["document_id"] = source.document_id
        items.append(item)
    return tuple(items)


def _is_schema_question(question: str) -> bool:
    if _is_aggregate_question(question):
        return False
    return bool(
        re.search(
            r"(?:열\s*목록|컬럼|스키마|헤더|(?:파일|문서|엑셀|시트)\s*구조|시트\s*수|행\s*수|마지막\s*(?:월|기간)|월별\s*(?:value|값)\s*열)",
            question,
            re.IGNORECASE,
        )
    )


def _is_aggregate_question(question: str) -> bool:
    return bool(
        re.search(
            r"(?:합계|총계|합산|평균|개수|건수|몇\s*개|집계|비교|대비|COUNT|SUM|AVG)",
            question,
            re.IGNORECASE,
        )
    )


def _has_aggregate_contract(sql: str) -> bool:
    return bool(re.search(r"\b(?:COUNT|SUM|AVG)\s*\(", sql, re.IGNORECASE)) and bool(
        re.search(r"\bapplied_rows\b", sql, re.IGNORECASE)
    )


def _aggregate_contract_failure() -> SqlQueryOutcome:
    answer = (
        "업로드 파일 집계 결과를 확인할 수 없습니다. "
        "필터, 집계 함수, 결과값, 적용 행 수를 모두 검증하지 못했습니다."
    )
    return SqlQueryOutcome(
        file_context="## 업로드 파일 SQL 결과\n상태: 확인불가\n" + answer,
        file_source_items=(),
        errors=("file SQL aggregate contract unavailable",),
        answer_md=answer,
    )


def _render_schema_answer(
    question: str,
    sources: Sequence[SqlFileSource],
    schemas: Sequence[Mapping[str, Any]],
) -> str:
    lines = ["## 업로드 파일 구조", f"시트 수: {len(schemas)}개"]
    observed_months: list[tuple[int, int, str]] = []
    all_column_names: list[str] = []
    for index, schema in enumerate(schemas):
        source = sources[index]
        raw_columns = schema.get("columns")
        columns = raw_columns if isinstance(raw_columns, list) else []
        names = [
            str(item.get("source_name") or "").strip()
            for item in columns
            if isinstance(item, dict) and str(item.get("source_name") or "").strip()
        ]
        all_column_names.extend(names)
        lines.extend(
            [
                f"### {source.sheet_name}",
                f"파일: {source.file_name}",
                f"행 수: {_format_number(source.row_count) if source.row_count is not None else '확인되지 않음'}",
                f"열 수: {len(names)}개",
                "열 목록: " + (", ".join(names) if names else "확인되지 않음"),
            ]
        )
        for name in names:
            for month, year in re.findall(r"(?<!\d)(1[0-2]|[1-9])/(20\d{2})(?!\d)", name):
                observed_months.append((int(year), int(month), f"{int(month)}/{year}"))
    if observed_months:
        latest = max(observed_months)
        lines.append(f"마지막 월: {latest[2]}")
        next_year, next_month = (latest[0] + 1, 1) if latest[1] == 12 else (latest[0], latest[1] + 1)
        next_label = f"{next_month}/{next_year}"
        present = any(next_label.casefold() in name.casefold() for name in all_column_names)
        lines.append(f"{next_label} 열: {'있음' if present else '없음'}")
    for source in sources[: len(schemas)]:
        normalized_sheet = source.sheet_name.casefold()
        if source.row_count is not None and re.search(r"(?:질문|question)", normalized_sheet, re.IGNORECASE):
            lines.append(f"질문 수: {_format_number(source.row_count)}개 (SQL 스키마 실측)")
        if source.row_count is not None and re.search(r"(?:출처|source)", normalized_sheet, re.IGNORECASE):
            lines.append(f"출처 수: {_format_number(source.row_count)}개 (SQL 스키마 실측)")
    return "\n".join(lines)


def _render_aggregate_answer(
    question: str,
    source: SqlFileSource,
    sql: str,
    result: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> str:
    raw_columns = result.get("columns")
    raw_rows = result.get("rows")
    if not isinstance(raw_columns, list) or not isinstance(raw_rows, list) or not raw_rows:
        return ""
    columns = [str(value) for value in raw_columns]
    applied_index = next((index for index, name in enumerate(columns) if name.casefold() == "applied_rows"), None)
    if applied_index is None:
        return ""
    rows = [row for row in raw_rows if isinstance(row, list)]
    if not rows or any(applied_index >= len(row) or not _is_number(row[applied_index]) for row in rows):
        return ""
    where_match = re.search(
        r"\bWHERE\b(.+?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    filter_text = "전체 행" if where_match is None else " ".join(where_match.group(1).split())
    aggregate_functions = list(
        dict.fromkeys(
            value.upper()
            for value in re.findall(r"\b(COUNT|SUM|AVG)\s*\(", sql, re.IGNORECASE)
        )
    )
    used_columns = _used_source_columns(sql, schema)
    labels = _source_column_labels(columns, schema)
    total_applied = sum(float(row[applied_index]) for row in rows)
    lines = [
        "## 업로드 파일 집계 결과",
        f"파일: {source.file_name}",
        f"시트·테이블명: {source.sheet_name} / data",
        f"필터 조건: {filter_text}",
        "사용 열: " + (", ".join(used_columns) if used_columns else "집계 결과 열"),
        "집계 함수: " + ", ".join(aggregate_functions),
        f"적용 행 수: {_format_number(total_applied)}",
        "| " + " | ".join(labels) + " |",
        "| " + " | ".join("---" for _ in labels) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                _format_number(value) if _is_number(value) else _markdown_cell(value)
                for value in row[: len(labels)]
            )
            + " |"
        )
    if re.search(r"(?:비교|대비|어느|어디|큰가|더\s*크)", question) and len(rows) >= 2:
        value_index = next(
            (
                index for index, name in enumerate(columns)
                if index != applied_index
                and re.search(
                    r"(?:total|sum|avg|value|amount|sales|금액|합계)",
                    name,
                    re.IGNORECASE,
                )
            ),
            None,
        )
        label_index = next((index for index in range(len(columns)) if index not in {applied_index, value_index}), None)
        if (
            value_index is not None
            and label_index is not None
            and all(
                value_index < len(row) and _is_number(row[value_index])
                for row in rows[:2]
            )
        ):
            left, right = rows[0], rows[1]
            left_value, right_value = float(left[value_index]), float(right[value_index])
            winner = left if left_value >= right_value else right
            lines.append(
                f"비교 결론: {winner[label_index]}이(가) {_format_number(abs(left_value - right_value))}만큼 더 큽니다."
            )
    return "\n".join(lines)


def _used_source_columns(sql: str, schema: Mapping[str, Any]) -> list[str]:
    raw_columns = schema.get("columns")
    columns = raw_columns if isinstance(raw_columns, list) else []
    return [
        str(item.get("source_name") or "")
        for item in columns
        if isinstance(item, dict)
        and str(item.get("query_name") or "")
        and re.search(rf"(?<![A-Za-z0-9_]){re.escape(str(item.get('query_name')))}(?![A-Za-z0-9_])", sql)
    ]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _format_number(value: int | float | None) -> str:
    if value is None:
        return "—"
    numeric = float(value)
    if numeric.is_integer():
        return f"{int(numeric):,}"
    return f"{numeric:,.6f}".rstrip("0").rstrip(".")


def _fetch_schema(source: SqlFileSource, conversation_id: str) -> dict[str, Any]:
    response = requests.post(
        f"{_file_service_base_url()}/file-sql/schema",
        json=_session_payload(
            conversation_id,
            logical_name=source.logical_name,
        ),
        timeout=_file_service_timeout(),
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("file SQL schema response must be an object")
    return {
        **body,
        "file_name": source.file_name,
        "sheet_name": source.sheet_name,
    }


def _generate_select(
    question: str,
    schemas: Sequence[Mapping[str, Any]],
) -> dict[str, str] | None:
    token = resolve_planner_genos_token()
    if not token:
        raise RuntimeError("planner token is unavailable")
    compact_schemas = [_compact_schema(question, schema) for schema in schemas]
    response = requests.post(
        f"{resolve_planner_genos_base_url().rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "messages": [
                {
                    "role": "system",
                    "content": _planner_system_prompt(),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "uploaded_file_schemas": compact_schemas},
                        ensure_ascii=False,
                    ),
                },
            ],
            "stream": False,
            "temperature": 0.0,
            "max_tokens": _planner_max_tokens(),
        },
        timeout=_planner_timeout(),
    )
    response.raise_for_status()
    payload = response.json()
    content = _message_content(payload)
    parsed = _json_object(content)
    logical_name = str(parsed.get("logical_name") or "").strip()
    sql = str(parsed.get("sql") or "").strip()
    if not logical_name and not sql:
        return None
    return {"logical_name": logical_name, "sql": sql}


def _run_query(
    conversation_id: str,
    logical_name: str,
    sql: str,
) -> dict[str, Any]:
    response = requests.post(
        f"{_file_service_base_url()}/file-sql/query",
        json=_session_payload(
            conversation_id,
            logical_name=logical_name,
            sql=sql,
        ),
        timeout=_file_service_timeout(),
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("file SQL query response must be an object")
    return body


def _compact_schema(question: str, schema: Mapping[str, Any]) -> dict[str, Any]:
    raw_columns = schema.get("columns")
    columns = [item for item in raw_columns if isinstance(item, dict)] if isinstance(raw_columns, list) else []
    cap = _max_schema_columns()
    if len(columns) > cap:
        tokens = _question_tokens(question)
        matched = [
            item
            for item in columns
            if any(token in str(item.get("source_name") or "").casefold() for token in tokens)
        ]
        identity = columns[: min(_identity_column_count(), cap)]
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in [*identity, *matched]:
            query_name = str(item.get("query_name") or "")
            if query_name and query_name not in seen:
                seen.add(query_name)
                selected.append(item)
            if len(selected) >= cap:
                break
        columns = selected
    return {
        "logical_name": str(schema.get("logical_name") or ""),
        "file_name": str(schema.get("file_name") or ""),
        "sheet_name": str(schema.get("sheet_name") or ""),
        "query_table": "data",
        "columns": columns,
    }


def _render_result(
    source: SqlFileSource,
    result: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> str:
    columns = result.get("columns")
    rows = result.get("rows")
    safe_columns = _source_column_labels(columns, schema)
    safe_rows = rows if isinstance(rows, list) else []
    lines = [
        "## 업로드 파일 SQL 결과",
        f"파일: {source.file_name}",
        f"시트: {source.sheet_name}",
    ]
    if not safe_rows:
        return "\n".join([*lines, "상태: 원천없음", "원천 조회 결과 0행"])
    if not safe_columns:
        raise ValueError("file SQL query returned rows without columns")
    lines.extend(
        [
            "상태: 확인됨",
            "| " + " | ".join(_markdown_cell(value) for value in safe_columns) + " |",
            "| " + " | ".join("---" for _ in safe_columns) + " |",
        ]
    )
    for row in safe_rows:
        if not isinstance(row, list):
            raise ValueError("file SQL row must be a list")
        values = [*row[: len(safe_columns)], *("" for _ in range(max(0, len(safe_columns) - len(row))))]
        lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    return "\n".join(lines)


def _source_column_labels(columns: Any, schema: Mapping[str, Any]) -> list[str]:
    if not isinstance(columns, list):
        return []
    raw_schema_columns = schema.get("columns")
    schema_columns = raw_schema_columns if isinstance(raw_schema_columns, list) else []
    names = {
        str(item.get("query_name") or ""): str(item.get("source_name") or "")
        for item in schema_columns
        if isinstance(item, dict)
        and str(item.get("query_name") or "")
        and str(item.get("source_name") or "")
    }
    labels: list[str] = []
    for value in columns:
        label = str(value)
        for query_name, source_name in names.items():
            label = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(query_name)}(?![A-Za-z0-9_])",
                source_name,
                label,
            )
        labels.append(label)
    return labels


def _session_payload(conversation_id: str, **values: Any) -> dict[str, Any]:
    return {
        "workflow_id": int(os.getenv("JW_CHAT_FILE_WORKFLOW_ID", "301")),
        "app_session_id": conversation_id,
        "chat_id": conversation_id,
        **values,
    }


def _message_content(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("planner response has no choices")
    message = choices[0].get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(choices[0].get("text"), str):
        return choices[0]["text"]
    raise ValueError("planner response has no content")


def _json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.IGNORECASE)
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if match is None:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("planner output must be a JSON object")
    return value


def _is_select_only_candidate(sql: str) -> bool:
    return bool(sql) and bool(re.match(r"^\s*(?:SELECT|WITH)\b", sql, re.IGNORECASE))


def _question_tokens(question: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            token.casefold()
            for token in re.findall(r"[0-9A-Za-z가-힣_]{2,}", question)
        )
    )


def _markdown_cell(value: Any) -> str:
    text = "—" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _file_service_base_url() -> str:
    return os.getenv("JW_CHAT_FILE_SEARCH_BASE", "http://code-serving-235:8080").rstrip("/")


def _file_service_timeout() -> float:
    return float(os.getenv("JW_CHAT_FILE_SQL_TIMEOUT_S", "5"))


def _planner_system_prompt() -> str:
    return os.getenv(
        "JW_CHAT_FILE_SQL_PLANNER_SYSTEM_PROMPT",
        (
            "You translate an uploaded-file question into one SQLite SELECT. "
            "Use exactly one supplied logical_name and query only its table alias data. "
            "Column query_name values (c1, c2, ...) are the only legal columns; "
            "source_name explains their meaning. Uploaded cell values are stored with TEXT "
            "affinity: compare categorical values with quoted string literals, and use "
            "SUM and AVG directly for numeric aggregates. Never use CAST because the scoped "
            "SQL policy rejects it. Never access system tables, attach databases, PRAGMA, "
            "operational marts, or other files. "
            "For every COUNT, SUM, AVG, grouped aggregate, or comparison query, also select "
            "COUNT(*) AS applied_rows so the result can be completeness-checked. "
            "Return JSON only as "
            '{"logical_name":"...","sql":"SELECT ... FROM data ..."}. '
            "If the question cannot be answered from these uploaded-file schemas, return "
            '{"logical_name":"","sql":""}.'
        ),
    )


def _planner_timeout() -> float:
    return float(os.getenv("JW_CHAT_FILE_SQL_PLANNER_TIMEOUT_S", "30"))


def _planner_max_tokens() -> int:
    return max(128, int(os.getenv("JW_CHAT_FILE_SQL_PLANNER_MAX_TOKENS", "2048")))


def _max_schema_tables() -> int:
    return max(1, int(os.getenv("JW_CHAT_FILE_SQL_MAX_TABLES", "4")))


def _max_schema_columns() -> int:
    return max(20, int(os.getenv("JW_CHAT_FILE_SQL_MAX_COLUMNS", "160")))


def _identity_column_count() -> int:
    return max(1, int(os.getenv("JW_CHAT_FILE_SQL_IDENTITY_COLUMNS", "24")))
