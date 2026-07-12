from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import requests

from jw_chat_agent_poc.genos_config import (
    resolve_planner_genos_base_url,
    resolve_planner_genos_token,
)


@dataclass(frozen=True, slots=True)
class SqlFileSource:
    logical_name: str
    file_name: str
    sheet_name: str
    document_id: int
    row_count: int | None = None
    column_count: int | None = None


@dataclass(frozen=True, slots=True)
class SqlQueryOutcome:
    file_context: str
    file_source_items: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]


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
        result = _run_query(conversation_id, logical_name, sql)
        schema = next(
            (
                item for item in schemas
                if str(item.get("logical_name") or "").strip() == logical_name
            ),
            {},
        )
        context = _render_result(source, result, schema)
        return SqlQueryOutcome(
            file_context=context,
            file_source_items=(
                {
                    "file_name": source.file_name,
                    "document_id": source.document_id,
                },
            ),
            errors=(),
        )
    except (requests.RequestException, ValueError, TypeError, KeyError, RuntimeError):
        return SqlQueryOutcome(
            file_context=(
                "## 업로드 파일 SQL 결과\n"
                "상태: 확인불가\n"
                "업로드 파일 SQL 질의를 실행하지 못해 요청한 값을 확인할 수 없습니다."
            ),
            file_source_items=(),
            errors=("file SQL query unavailable",),
        )


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
        for item in [*matched, *identity]:
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
            "affinity: compare categorical values with quoted string literals, and use CAST "
            "when numeric comparison is required. Never access system tables, attach "
            "databases, PRAGMA, operational marts, or other files. Return JSON only as "
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
