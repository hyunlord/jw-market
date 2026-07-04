from __future__ import annotations

from dataclasses import dataclass
import json

import pymysql

from .models import JsonValue
from .row_topic_assignment import AssignmentInputRow, AssignmentParseError, RowTopicAssignment, TopicRubric
from .row_topic_sql import assignment_table_ddl, compatible_share_view_sql
from .topic_store import validated_stage_schema


TOPICS_TABLE = "mart_brand_activity_topics"
ASSIGNMENT_TABLE = "row_topic_assignment"


@dataclass(frozen=True, slots=True)
class ScopeRubric:
    """Fixed topic candidates for one stored mart scope."""

    scope_id: str
    display_name: str
    atc4_values: tuple[str, ...]
    axis_topics: tuple[TopicRubric, ...]
    brand_topics: dict[str, tuple[TopicRubric, ...]]


@dataclass(frozen=True, slots=True)
class PreparedRun:
    """Rows and rubrics resolved from DB before any LLM call."""

    topic_set_version: str
    rows: tuple[AssignmentInputRow, ...]
    rubrics: dict[tuple[str, str], tuple[TopicRubric, ...]]


def prepare_run(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    topic_set_version: str,
) -> PreparedRun:
    """Load stored topic mart payloads and matching keyword rows."""
    safe_schema = validated_stage_schema(schema)
    resolved_version = topic_set_version or latest_topic_run_id(connection, schema=safe_schema)
    scopes = load_scope_rubrics(connection, schema=safe_schema, run_id=resolved_version)
    rows = tuple(load_assignment_rows(connection, schema=safe_schema, scopes=scopes))
    scope_by_id = {scope.scope_id: scope for scope in scopes}
    rubrics: dict[tuple[str, str], tuple[TopicRubric, ...]] = {}
    for row in rows:
        scope = scope_by_id[row.scope_id]
        brand_topics = scope.brand_topics.get(row.brand, ())
        rubrics[(row.scope_id, row.brand)] = (*scope.axis_topics, *brand_topics)
    return PreparedRun(topic_set_version=resolved_version, rows=rows, rubrics=rubrics)


def apply_ddl(connection: pymysql.connections.Connection, *, schema: str) -> dict[str, JsonValue]:
    """Create the assignment table and share view, refusing to overwrite an existing table."""
    if table_exists(connection, schema=schema, table=ASSIGNMENT_TABLE):
        raise AssignmentParseError(f"{schema}.{ASSIGNMENT_TABLE} already exists; refusing to overwrite")
    with connection.cursor() as cursor:
        cursor.execute(assignment_table_ddl(schema))
        cursor.execute(compatible_share_view_sql(schema))
    connection.commit()
    return {"mode": "apply-ddl", "table": f"{schema}.{ASSIGNMENT_TABLE}", "view": f"{schema}.row_topic_assignment_share_view"}


def insert_assignments(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    assignments: list[RowTopicAssignment],
) -> int:
    """Persist one successful batch idempotently."""
    if not assignments:
        return 0
    safe_schema = validated_stage_schema(schema)
    sql = f"""
        INSERT INTO `{safe_schema}`.`{ASSIGNMENT_TABLE}`
        (row_id, scope_id, brand, topic_id, topic_set_version, prompt_version, batch_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            scope_id=VALUES(scope_id),
            brand=VALUES(brand),
            prompt_version=VALUES(prompt_version),
            batch_id=VALUES(batch_id)
    """
    values = [
        (item.row_id, item.scope_id, item.brand, item.topic_id, item.topic_set_version, item.prompt_version, item.batch_id)
        for item in assignments
    ]
    with connection.cursor() as cursor:
        affected = cursor.executemany(sql, values)
    connection.commit()
    return int(affected)


def load_scope_rubrics(connection: pymysql.connections.Connection, *, schema: str, run_id: str) -> list[ScopeRubric]:
    """Load fixed axis and brand-specific topics from stored mart payloads."""
    sql = f"SELECT scope_id, display_name, atc4_values, payload FROM `{schema}`.`{TOPICS_TABLE}` WHERE run_id=%s ORDER BY scope_id"
    scopes: list[ScopeRubric] = []
    with connection.cursor() as cursor:
        cursor.execute(sql, (run_id,))
        for row in cursor.fetchall():
            payload = _json_object(row["payload"])
            axis = _json_object(payload.get("axis"))
            brands = _json_array(payload.get("brands"))
            axis_topics = tuple(topic_rubric(topic) for topic in _json_array(axis.get("topics")))
            brand_topics = {
                _text(brand.get("brand")): tuple(topic_rubric(topic) for topic in _json_array(_json_object(brand).get("brand_specific_topics")))
                for brand in brands
                if isinstance(brand, dict)
            }
            scopes.append(
                ScopeRubric(
                    scope_id=str(row["scope_id"]),
                    display_name=str(row["display_name"]),
                    atc4_values=tuple(_json_texts(row["atc4_values"])),
                    axis_topics=axis_topics,
                    brand_topics=brand_topics,
                )
            )
    if not scopes:
        raise AssignmentParseError(f"no topic payload rows found for run_id={run_id}")
    return scopes


def load_assignment_rows(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    scopes: list[ScopeRubric],
) -> list[AssignmentInputRow]:
    """Load keyword rows belonging to stored payload scopes."""
    markets = sorted({atc4 for scope in scopes for atc4 in scope.atc4_values})
    placeholders = ",".join(["%s"] * len(markets))
    sql = (
        "SELECT id, period_ym, visit_location, specialty, product_name, therapeutic_class, "
        "keyword_text, interest, prescription_evolution "
        f"FROM `{schema}`.`km_keyword_event_stage` "
        f"WHERE keyword_text <> '' AND therapeutic_class IN ({placeholders}) "
        "ORDER BY therapeutic_class, product_name, period_ym, id"
    )
    scopes_by_atc4: dict[str, list[ScopeRubric]] = {}
    for scope in scopes:
        for atc4 in scope.atc4_values:
            scopes_by_atc4.setdefault(atc4, []).append(scope)
    rows: list[AssignmentInputRow] = []
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(sql, tuple(markets))
        for record in cursor.fetchall():
            brand = str(record["product_name"])
            atc4 = str(record["therapeutic_class"])
            for scope in scopes_by_atc4.get(atc4, []):
                rows.append(_assignment_row(record, scope.scope_id, brand))
        cursor.execute("COMMIT")
    return rows


def latest_topic_run_id(connection: pymysql.connections.Connection, *, schema: str) -> str:
    """Return the run id currently backing stored topic payload rows."""
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT run_id FROM `{schema}`.`{TOPICS_TABLE}` GROUP BY run_id ORDER BY MAX(updated_at) DESC LIMIT 1")
        row = cursor.fetchone()
    if not row:
        raise AssignmentParseError("no stored topic run found")
    return str(row["run_id"])


def table_exists(connection: pymysql.connections.Connection, *, schema: str, table: str) -> bool:
    """Return whether a table already exists."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        row = cursor.fetchone()
    return bool(row and int(row["n"]) > 0)


def topic_rubric(value: JsonValue) -> TopicRubric:
    """Convert one stored topic JSON object into a rubric item."""
    topic = _json_object(value)
    return TopicRubric(
        topic_id=_text(topic.get("topic_id")),
        label=_text(topic.get("label")),
        definition=_text(topic.get("definition")),
        keywords=tuple(_json_texts(topic.get("keywords"))),
    )


def _assignment_row(record: dict[str, JsonValue], scope_id: str, brand: str) -> AssignmentInputRow:
    return AssignmentInputRow(
        row_id=int(record["id"]),
        scope_id=scope_id,
        brand=brand,
        keyword_text=str(record["keyword_text"]),
        period_ym=str(record["period_ym"] or ""),
        visit_location=str(record["visit_location"] or ""),
        specialty=str(record["specialty"] or ""),
        interest=str(record["interest"] or ""),
        prescription_evolution=str(record["prescription_evolution"] or ""),
    )


def _json_object(value: JsonValue) -> dict[str, JsonValue]:
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return value if isinstance(value, dict) else {}


def _json_array(value: JsonValue) -> list[JsonValue]:
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, list) else []
    return value if isinstance(value, list) else []


def _json_texts(value: JsonValue) -> list[str]:
    return [_text(item) for item in _json_array(value) if _text(item)]


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""
