#!/usr/bin/env python3
"""Create isolated Keyword/Meeting stage artifacts without production writes."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import os
from pathlib import Path
import re
import sys
from typing import Final, Sequence

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.etl.brand_activity.ingest_keyword import (  # noqa: E402
    read_keyword_events,
    read_keyword_message_counts,
)
from pipeline.scripts.etl.brand_activity.ingest_meeting import (  # noqa: E402
    read_meeting_events,
    read_meeting_message_counts,
)
from pipeline.scripts.etl.brand_activity.km_core import (  # noqa: E402
    JsonValue,
    KeywordEvent,
    MeetingEvent,
    MessageCountCell,
    source_period_from_name,
    source_sha256,
    text_sha256,
)
from pipeline.scripts.etl.brand_activity.km_validation import (  # noqa: E402
    compare_core_to_message_count,
    compare_message_count_overlaps,
    duplicate_hash_summary,
    file_period_distribution,
    keyword_enum_distribution,
    meeting_enum_distribution,
    period_distribution,
    text_field_summary,
)


DEFAULT_STAGE_SCHEMA: Final[str] = "jw_brand_activity_stage"
EXPECTED_MONTHS: Final[tuple[str, ...]] = ("2025-07", "2025-08", "2025-09", "2025-10")
KEYWORD_TABLE: Final[str] = "km_keyword_event_stage"
MEETING_TABLE: Final[str] = "km_meeting_event_stage"
SYSTEM_SCHEMAS: Final[tuple[str, ...]] = ("information_schema", "mysql", "performance_schema", "sys")
BRAND_ACTIVITY_SCHEMA_PATTERN: Final = re.compile(r"^jw_brand_activity_[A-Za-z0-9_]+$")


def quote_schema_name(schema: str) -> str:
    """Validate that a DB schema name is safe and isolated to stage use."""
    if re.fullmatch(r"[A-Za-z0-9_]+", schema) is None:
        raise ValueError(f"unsafe schema name: {schema!r}")
    if schema != DEFAULT_STAGE_SCHEMA and BRAND_ACTIVITY_SCHEMA_PATTERN.fullmatch(schema) is None:
        raise ValueError(f"refusing schema outside {DEFAULT_STAGE_SCHEMA} or brand-activity scratch schema: {schema!r}")
    return schema


def stage_ddl(schema: str) -> str:
    """Build two append-preserving stage table DDL statements."""
    safe_schema = quote_schema_name(schema)
    return f"""CREATE SCHEMA IF NOT EXISTS `{safe_schema}`;

CREATE TABLE IF NOT EXISTS `{safe_schema}`.`{KEYWORD_TABLE}` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `period_ym` char(7) NOT NULL,
  `visit_location` varchar(255) NOT NULL,
  `specialty` varchar(255) NOT NULL,
  `representing_company` varchar(255) NOT NULL,
  `product_name` varchar(255) NOT NULL,
  `therapeutic_class` varchar(64) NOT NULL,
  `keyword_text` longtext NOT NULL,
  `interest` varchar(64) NOT NULL,
  `prescription_frequency` varchar(128) NOT NULL,
  `prescription_evolution` varchar(128) NOT NULL,
  `abstract_lit` varchar(16) NOT NULL,
  `patient_lit` varchar(16) NOT NULL,
  `promotional_lit` varchar(16) NOT NULL,
  `samples_left` varchar(16) NOT NULL,
  `other_materials_left` varchar(16) NOT NULL,
  `what_other_materials` text NOT NULL,
  `other_comments` text NOT NULL,
  `source_file` varchar(255) NOT NULL,
  `source_sheet` varchar(64) NOT NULL,
  `source_row_no` int NOT NULL,
  `source_file_sha256` char(64) NOT NULL,
  `stage_row_sha256` char(64) NOT NULL,
  `loaded_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_km_keyword_period_class` (`period_ym`, `therapeutic_class`),
  KEY `idx_km_keyword_product` (`product_name`),
  KEY `idx_km_keyword_lineage` (`source_file`, `source_sheet`, `source_row_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `{safe_schema}`.`{MEETING_TABLE}` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `meeting_date` date NOT NULL,
  `period_ym` char(7) NOT NULL,
  `meeting_topic` text NOT NULL,
  `meeting_format` varchar(128) NOT NULL,
  `pharma_sponsor` varchar(255) NOT NULL,
  `non_pharma_sponsor` varchar(255) NOT NULL,
  `no_at_meeting` int NULL,
  `product_name` varchar(255) NOT NULL,
  `therapeutic_class` varchar(64) NOT NULL,
  `prescription_frequency` varchar(128) NOT NULL,
  `prescription_evolution` varchar(128) NOT NULL,
  `interest` varchar(64) NOT NULL,
  `verbatim_message` text NOT NULL,
  `other_comments` text NOT NULL,
  `source_file` varchar(255) NOT NULL,
  `source_sheet` varchar(64) NOT NULL,
  `source_row_no` int NOT NULL,
  `source_file_sha256` char(64) NOT NULL,
  `stage_row_sha256` char(64) NOT NULL,
  `loaded_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_km_meeting_period_class` (`period_ym`, `therapeutic_class`),
  KEY `idx_km_meeting_product` (`product_name`),
  KEY `idx_km_meeting_lineage` (`source_file`, `source_sheet`, `source_row_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def parse_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE dotenv lines for local MariaDB credentials."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def discover_workbooks(root: Path, prefix: str, expected_months: Sequence[str]) -> tuple[list[Path], list[str]]:
    """Find expected monthly workbooks and report missing source months."""
    workbooks = sorted(path for path in root.glob(f"{prefix} for JW *. 25.xlsx") if not path.name.startswith("~$"))
    selected = [path for path in workbooks if source_period_from_name(path) in expected_months]
    present = {source_period_from_name(path) for path in selected}
    missing = [month for month in expected_months if month not in present]
    return selected, missing


def stage_row_hash(row: dict[str, JsonValue]) -> str:
    """Hash a stage row so idempotency can be checked without natural keys."""
    return text_sha256(json.dumps(row, ensure_ascii=False, sort_keys=True))


def write_json(path: Path, payload: JsonValue) -> None:
    """Write deterministic UTF-8 JSON for audit evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_redacted_csv(path: Path, rows: Sequence[dict[str, JsonValue]]) -> None:
    """Write redacted stage-like rows without sensitive source text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_source_manifest(path: Path, workbooks: Sequence[Path]) -> None:
    """Write SHA256 lineage for every source workbook used by the PoC."""
    payload = [
        {
            "path": str(workbook),
            "file": workbook.name,
            "source_period_ym": source_period_from_name(workbook),
            "sha256": source_sha256(workbook),
        }
        for workbook in workbooks
    ]
    write_json(path, payload)


def keyword_stage_tuple(event: KeywordEvent) -> tuple[JsonValue, ...]:
    """Convert a Keyword event into the DB insert tuple including row hash."""
    row = event.to_stage_row()
    return (
        event.period_ym,
        event.visit_location,
        event.specialty,
        event.representing_company,
        event.product_name,
        event.therapeutic_class,
        event.keyword_text,
        event.interest,
        event.prescription_frequency,
        event.prescription_evolution,
        event.abstract_lit,
        event.patient_lit,
        event.promotional_lit,
        event.samples_left,
        event.other_materials_left,
        event.what_other_materials,
        event.other_comments,
        event.source_file,
        event.source_sheet,
        event.source_row_no,
        event.source_file_sha256,
        stage_row_hash(row),
    )


def meeting_stage_tuple(event: MeetingEvent) -> tuple[JsonValue, ...]:
    """Convert a Meeting event into the DB insert tuple including row hash."""
    row = event.to_stage_row()
    return (
        event.meeting_date,
        event.period_ym,
        event.meeting_topic,
        event.meeting_format,
        event.pharma_sponsor,
        event.non_pharma_sponsor,
        event.no_at_meeting,
        event.product_name,
        event.therapeutic_class,
        event.prescription_frequency,
        event.prescription_evolution,
        event.interest,
        event.verbatim_message,
        event.other_comments,
        event.source_file,
        event.source_sheet,
        event.source_row_no,
        event.source_file_sha256,
        stage_row_hash(row),
    )


def database_password(args: argparse.Namespace) -> str:
    """Resolve the DB password from flags, environment, or local docker env."""
    if args.db_password:
        return args.db_password
    if os.environ.get(args.db_password_env):
        return os.environ[args.db_password_env]
    docker_env = parse_env_file(ROOT / "pipeline" / "docker" / ".env")
    return docker_env.get(args.db_password_env, "")


def inventory_snapshot(cursor: "pymysql.cursors.Cursor") -> list[dict[str, JsonValue]]:
    """Capture non-system database inventory before and after isolated load."""
    placeholders = ", ".join(["%s"] * len(SYSTEM_SCHEMAS))
    cursor.execute(
        f"""
        SELECT TABLE_SCHEMA, TABLE_NAME, COALESCE(TABLE_ROWS, 0)
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA NOT IN ({placeholders})
        ORDER BY TABLE_SCHEMA, TABLE_NAME
        """,
        SYSTEM_SCHEMAS,
    )
    return [
        {"schema": str(schema), "table": str(table), "table_rows_estimate": int(rows)}
        for schema, table, rows in cursor.fetchall()
    ]


def load_isolated_db(
    args: argparse.Namespace,
    keyword_events: Sequence[KeywordEvent],
    meeting_events: Sequence[MeetingEvent],
) -> dict[str, JsonValue]:
    """Load raw events into isolated stage tables and return DB evidence."""
    import pymysql

    schema = quote_schema_name(args.stage_schema)
    connection = pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=database_password(args),
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=8,
    )
    try:
        with connection.cursor() as cursor:
            before_inventory = inventory_snapshot(cursor)
            for statement in stage_ddl(schema).split(";"):
                sql = statement.strip()
                if sql:
                    cursor.execute(sql)
            cursor.execute(f"TRUNCATE TABLE `{schema}`.`{KEYWORD_TABLE}`")
            cursor.execute(f"TRUNCATE TABLE `{schema}`.`{MEETING_TABLE}`")
            cursor.executemany(
                f"""
                INSERT INTO `{schema}`.`{KEYWORD_TABLE}`
                (period_ym, visit_location, specialty, representing_company, product_name, therapeutic_class,
                 keyword_text, interest, prescription_frequency, prescription_evolution, abstract_lit, patient_lit,
                 promotional_lit, samples_left, other_materials_left, what_other_materials, other_comments,
                 source_file, source_sheet, source_row_no, source_file_sha256, stage_row_sha256)
                VALUES ({", ".join(["%s"] * 22)})
                """,
                [keyword_stage_tuple(event) for event in keyword_events],
            )
            cursor.executemany(
                f"""
                INSERT INTO `{schema}`.`{MEETING_TABLE}`
                (meeting_date, period_ym, meeting_topic, meeting_format, pharma_sponsor, non_pharma_sponsor,
                 no_at_meeting, product_name, therapeutic_class, prescription_frequency, prescription_evolution,
                 interest, verbatim_message, other_comments, source_file, source_sheet, source_row_no,
                 source_file_sha256, stage_row_sha256)
                VALUES ({", ".join(["%s"] * 19)})
                """,
                [meeting_stage_tuple(event) for event in meeting_events],
            )
            cursor.execute(f"SELECT COUNT(*), COUNT(DISTINCT stage_row_sha256) FROM `{schema}`.`{KEYWORD_TABLE}`")
            keyword_count, keyword_hashes = cursor.fetchone()
            cursor.execute(f"SELECT COUNT(*), COUNT(DISTINCT stage_row_sha256) FROM `{schema}`.`{MEETING_TABLE}`")
            meeting_count, meeting_hashes = cursor.fetchone()
            after_inventory = inventory_snapshot(cursor)
        connection.commit()
        return {
            "schema": schema,
            "tables": {
                KEYWORD_TABLE: {"rows": int(keyword_count), "distinct_stage_row_hashes": int(keyword_hashes)},
                MEETING_TABLE: {"rows": int(meeting_count), "distinct_stage_row_hashes": int(meeting_hashes)},
            },
            "inventory_before": before_inventory,
            "inventory_after": after_inventory,
        }
    except pymysql.MySQLError:
        connection.rollback()
        raise
    finally:
        connection.close()


def class_month_summary(keyword_events: Sequence[KeywordEvent], meeting_events: Sequence[MeetingEvent]) -> dict[str, JsonValue]:
    """Summarize monthly ATC4/class distributions for validation docs."""
    keyword_counts: Counter[tuple[str, str]] = Counter((event.period_ym, event.therapeutic_class) for event in keyword_events)
    meeting_counts: Counter[tuple[str, str]] = Counter((event.period_ym, event.therapeutic_class) for event in meeting_events)
    return {
        "keyword": [
            {"period_ym": period, "therapeutic_class": cls, "rows": rows}
            for (period, cls), rows in sorted(keyword_counts.items())
        ],
        "meeting": [
            {"period_ym": period, "therapeutic_class": cls, "rows": rows}
            for (period, cls), rows in sorted(meeting_counts.items())
        ],
    }


def build_validation_payload(
    keyword_events: Sequence[KeywordEvent],
    meeting_events: Sequence[MeetingEvent],
    keyword_counts: Sequence[Sequence[MessageCountCell]],
    meeting_counts: Sequence[Sequence[MessageCountCell]],
    missing_keyword_months: Sequence[str],
    missing_meeting_months: Sequence[str],
) -> dict[str, JsonValue]:
    """Build the redacted validation JSON used by docs and package audit."""
    keyword_message_sets = [list(cells) for cells in keyword_counts]
    meeting_message_sets = [list(cells) for cells in meeting_counts]
    flat_keyword_counts = [cell for cells in keyword_message_sets for cell in cells]
    flat_meeting_counts = [cell for cells in meeting_message_sets for cell in cells]
    return {
        "source_completeness": {
            "expected_months": list(EXPECTED_MONTHS),
            "missing_keyword_months": list(missing_keyword_months),
            "missing_meeting_months": list(missing_meeting_months),
        },
        "core_rows": {
            "keyword": len(keyword_events),
            "meeting": len(meeting_events),
        },
        "core_period_distribution": {
            "keyword": period_distribution(keyword_events),
            "meeting": period_distribution(meeting_events),
        },
        "file_period_distribution": {
            "keyword": file_period_distribution(keyword_events),
            "meeting": file_period_distribution(meeting_events),
        },
        "duplicate_hash_summary": {
            "keyword": duplicate_hash_summary(keyword_events),
            "meeting": duplicate_hash_summary(meeting_events),
        },
        "message_count_overlap": {
            "keyword": compare_message_count_overlaps(keyword_message_sets),
            "meeting": compare_message_count_overlaps(meeting_message_sets),
        },
        "core_to_message_count": {
            "keyword": compare_core_to_message_count("keyword", keyword_events, flat_keyword_counts),
            "meeting": compare_core_to_message_count("meeting", meeting_events, flat_meeting_counts),
        },
        "enum_distribution": {
            "keyword": keyword_enum_distribution(keyword_events),
            "meeting": meeting_enum_distribution(meeting_events),
        },
        "text_summary_no_raw_text": {
            "keyword_message": text_field_summary(keyword_events, "keyword_text"),
            "meeting_topic": text_field_summary(meeting_events, "meeting_topic"),
            "meeting_verbatim": text_field_summary(meeting_events, "verbatim_message"),
        },
        "class_month_summary": class_month_summary(keyword_events, meeting_events),
    }


def sensitive_values(keyword_events: Sequence[KeywordEvent], meeting_events: Sequence[MeetingEvent]) -> set[str]:
    """Collect sensitive source strings for artifact redaction scanning."""
    values: set[str] = set()
    for event in keyword_events:
        values.update({event.keyword_text, event.what_other_materials, event.other_comments})
    for event in meeting_events:
        values.update({event.meeting_topic, event.verbatim_message, event.other_comments})
    return {value for value in values if len(value) >= 8}


def redaction_scan(
    artifact_roots: Sequence[Path],
    keyword_events: Sequence[KeywordEvent],
    meeting_events: Sequence[MeetingEvent],
) -> dict[str, JsonValue]:
    """Scan audit/output artifacts for sensitive source strings."""
    candidates = sensitive_values(keyword_events, meeting_events)
    matches: list[dict[str, JsonValue]] = []
    scanned_files = 0
    for root in artifact_roots:
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in {".csv", ".json", ".log", ".md", ".py", ".sql", ".tsv", ".txt"}:
                continue
            scanned_files += 1
            text = path.read_text(encoding="utf-8")
            for value in candidates:
                if value in text:
                    matches.append({"path": str(path), "value_sha256": text_sha256(value), "value_len": len(value)})
                    break
    return {
        "scanned_files": scanned_files,
        "sensitive_values_tested": len(candidates),
        "raw_sensitive_value_matches": len(matches),
        "matches": matches,
    }


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the local isolated PoC runner."""
    parser = argparse.ArgumentParser(description="Create isolated Keyword/Meeting stage artifacts.")
    parser.add_argument("--keyword-root", type=Path, required=True)
    parser.add_argument("--meeting-root", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--expected-months", nargs="*", default=list(EXPECTED_MONTHS))
    parser.add_argument("--db-load", action="store_true")
    parser.add_argument("--no-db-load", action="store_true")
    parser.add_argument("--stage-schema", default=DEFAULT_STAGE_SCHEMA)
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=3308)
    parser.add_argument("--db-user", default="root")
    parser.add_argument("--db-password", default="")
    parser.add_argument("--db-password-env", default="MARIADB_ROOT_PASSWORD")
    return parser.parse_args()


def main() -> int:
    """Run workbook extraction, validation, optional DB load, and audit writing."""
    args = parse_args()
    if args.db_load and args.no_db_load:
        raise SystemExit("--db-load and --no-db-load are mutually exclusive")
    quote_schema_name(args.stage_schema)
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    expected_months = tuple(args.expected_months)
    keyword_files, missing_keyword_months = discover_workbooks(args.keyword_root, "Keywords", expected_months)
    meeting_files, missing_meeting_months = discover_workbooks(args.meeting_root, "Meetings", expected_months)
    if missing_keyword_months or missing_meeting_months:
        raise SystemExit(
            f"Missing source months: keyword={missing_keyword_months}, meeting={missing_meeting_months}"
        )
    keyword_events = [event for workbook in keyword_files for event in read_keyword_events(workbook)]
    meeting_events = [event for workbook in meeting_files for event in read_meeting_events(workbook)]
    keyword_message_counts = [read_keyword_message_counts(workbook) for workbook in keyword_files]
    meeting_message_counts = [read_meeting_message_counts(workbook) for workbook in meeting_files]
    keyword_redacted = [event.to_redacted_row() for event in keyword_events]
    meeting_redacted = [event.to_redacted_row() for event in meeting_events]
    write_redacted_csv(args.output_dir / "keyword_events_stage_redacted.csv", keyword_redacted)
    write_redacted_csv(args.output_dir / "meeting_events_stage_redacted.csv", meeting_redacted)
    (args.output_dir / "km_keyword_meeting_stage.sql").write_text(stage_ddl(args.stage_schema), encoding="utf-8")
    write_source_manifest(args.audit_dir / "source_sha256_manifest.json", [*keyword_files, *meeting_files])
    validation = build_validation_payload(
        keyword_events,
        meeting_events,
        keyword_message_counts,
        meeting_message_counts,
        missing_keyword_months,
        missing_meeting_months,
    )
    validation["db_load"] = (
        load_isolated_db(args, keyword_events, meeting_events) if args.db_load else "skipped"
    )
    write_json(args.audit_dir / "km_ingest_validation.json", validation)
    scan = redaction_scan([args.audit_dir, args.output_dir], keyword_events, meeting_events)
    write_json(args.audit_dir / "redaction_scan.json", scan)
    if scan["raw_sensitive_value_matches"] != 0:
        raise SystemExit(f"Redaction scan failed: {scan}")
    run_log = {
        "keyword_root": str(args.keyword_root),
        "meeting_root": str(args.meeting_root),
        "audit_dir": str(args.audit_dir),
        "output_dir": str(args.output_dir),
        "keyword_files": [path.name for path in keyword_files],
        "meeting_files": [path.name for path in meeting_files],
        "keyword_rows": len(keyword_events),
        "meeting_rows": len(meeting_events),
        "redaction_scan": scan,
    }
    write_json(args.audit_dir / "km_ingest_run_log.json", run_log)
    print(json.dumps(run_log, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
