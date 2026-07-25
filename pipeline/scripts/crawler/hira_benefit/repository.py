from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, Self

from .change_detection import StoredNoticeState
from .contract import HiraRunMetrics
from .models import ParsedNotice
from .scope import brands_from_cache_payload


class Cursor(Protocol):
    def execute(self, sql: str, params: object = None) -> Any: ...
    def fetchone(self) -> dict[str, Any] | None: ...
    def fetchall(self) -> Sequence[dict[str, Any]]: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *args: object) -> None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PersistableNotice:
    parsed: ParsedNotice
    listing_fingerprint: str
    brand_names: tuple[str, ...]


def latest_notice_id(notice_ids: Sequence[str]) -> str | None:
    """Return a deterministic receipt marker without treating it as a watermark."""

    if not notice_ids:
        return None
    if all(notice_id.isdecimal() for notice_id in notice_ids):
        return max(notice_ids, key=int)
    return max(notice_ids)


def connect_from_env() -> Connection:
    import pymysql

    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", ""),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def load_jw_brand_scope(conn: Connection) -> tuple[tuple[str, ...], str]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT response_json, build_sha
            FROM cache_brands
            WHERE query_key = 'default'
            """
        )
        row = cursor.fetchone()
    if row is None:
        raise RuntimeError("cache_brands.default is missing")
    return (
        brands_from_cache_payload(str(row["response_json"])),
        f"cache_brands:{row.get('build_sha') or 'unknown'!s}",
    )


def load_notice_state(conn: Connection) -> dict[str, StoredNoticeState]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT source_notice_id, listing_fingerprint FROM hira_benefit_notice"
        )
        rows = cursor.fetchall()
    return {
        str(row["source_notice_id"]): StoredNoticeState(
            source_notice_id=str(row["source_notice_id"]),
            listing_fingerprint=str(row["listing_fingerprint"]),
        )
        for row in rows
    }


def has_crawl_state(conn: Connection, *, source_key: str = "hira_insurance_criteria") -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 AS found FROM hira_benefit_crawl_state WHERE source_key=%s",
            (source_key,),
        )
        return cursor.fetchone() is not None


def persist_batch(
    conn: Connection,
    *,
    notices: Sequence[PersistableNotice],
    run_id: str,
    index_tag_signature_sha256: str,
    mapping_revision: str,
    collected_at: datetime,
    run_metrics: HiraRunMetrics | None = None,
    source_key: str = "hira_insurance_criteria",
) -> None:
    """Commit notice rows, brand links, and the success watermark atomically."""

    timestamp = collected_at.replace(tzinfo=None)
    metrics = run_metrics or HiraRunMetrics(0, 0, 0, 0, len(notices), 0, 0)
    try:
        with conn.cursor() as cursor:
            for item in notices:
                parsed = item.parsed
                cursor.execute(
                    """
                    INSERT INTO hira_benefit_notice (
                      source_notice_id, source_url, title, notice_no, notice_date,
                      target_condition, exclusion_rule, dosage_limit, raw_text,
                      raw_html_sha256, listing_fingerprint, parse_status,
                      parse_failed_fields_json, collected_at, updated_at
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE
                      source_url=VALUES(source_url),
                      title=VALUES(title),
                      notice_no=VALUES(notice_no),
                      notice_date=VALUES(notice_date),
                      target_condition=VALUES(target_condition),
                      exclusion_rule=VALUES(exclusion_rule),
                      dosage_limit=VALUES(dosage_limit),
                      raw_text=VALUES(raw_text),
                      raw_html_sha256=VALUES(raw_html_sha256),
                      listing_fingerprint=VALUES(listing_fingerprint),
                      parse_status=VALUES(parse_status),
                      parse_failed_fields_json=VALUES(parse_failed_fields_json),
                      collected_at=VALUES(collected_at),
                      updated_at=VALUES(updated_at)
                    """,
                    (
                        parsed.source_notice_id,
                        parsed.source_url,
                        parsed.title,
                        parsed.notice_no,
                        parsed.notice_date,
                        parsed.target_condition,
                        parsed.exclusion_rule,
                        parsed.dosage_limit,
                        parsed.raw_text,
                        parsed.raw_html_sha256,
                        item.listing_fingerprint,
                        parsed.parse_status.value,
                        json.dumps(parsed.failed_fields, ensure_ascii=False),
                        timestamp,
                        timestamp,
                    ),
                )
                cursor.execute(
                    "DELETE FROM hira_benefit_notice_brand WHERE source_notice_id=%s",
                    (parsed.source_notice_id,),
                )
                for brand_name in item.brand_names:
                    cursor.execute(
                        """
                        INSERT INTO hira_benefit_notice_brand (
                          source_notice_id, brand_name, brand_key, match_method, created_at
                        ) VALUES (%s, %s, NULL, 'exact_normalized_name', %s)
                        """,
                        (parsed.source_notice_id, brand_name, timestamp),
                    )
            last_seen_notice_id = latest_notice_id(
                tuple(item.parsed.source_notice_id for item in notices)
            )
            receipt = {
                "run_id": run_id,
                "notice_count": len(notices),
                "mapping_revision": mapping_revision,
            }
            cursor.execute(
                """
                INSERT INTO hira_benefit_crawl_run (
                  run_id, started_at, finished_at, exit_code, failures,
                  identity_gap, pending_gap, parsed_count, partial_count,
                  failed_count, status, alert_status, receipt_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'complete', NULL, %s)
                ON DUPLICATE KEY UPDATE
                  finished_at=VALUES(finished_at),
                  exit_code=VALUES(exit_code),
                  failures=VALUES(failures),
                  identity_gap=VALUES(identity_gap),
                  pending_gap=VALUES(pending_gap),
                  parsed_count=VALUES(parsed_count),
                  partial_count=VALUES(partial_count),
                  failed_count=VALUES(failed_count),
                  status=VALUES(status),
                  receipt_json=VALUES(receipt_json)
                """,
                (
                    run_id,
                    timestamp,
                    timestamp,
                    metrics.exit_code,
                    metrics.failures,
                    metrics.identity_gap,
                    metrics.pending_gap,
                    metrics.parsed_count,
                    metrics.partial_count,
                    metrics.failed_count,
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                ),
            )
            cursor.execute(
                """
                INSERT INTO hira_benefit_crawl_state (
                  source_key, last_success_run_id, last_success_at,
                  last_seen_notice_id, index_tag_signature_sha256,
                  mapping_revision, receipt_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  last_success_run_id=VALUES(last_success_run_id),
                  last_success_at=VALUES(last_success_at),
                  last_seen_notice_id=VALUES(last_seen_notice_id),
                  index_tag_signature_sha256=VALUES(index_tag_signature_sha256),
                  mapping_revision=VALUES(mapping_revision),
                  receipt_json=VALUES(receipt_json)
                """,
                (
                    source_key,
                    run_id,
                    timestamp,
                    last_seen_notice_id,
                    index_tag_signature_sha256,
                    mapping_revision,
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
