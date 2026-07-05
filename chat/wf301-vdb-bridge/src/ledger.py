"""MariaDB ledger operations for registered VDB commits."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import pymysql
import pymysql.cursors

from . import settings


class LedgerConfigError(RuntimeError):
    pass


@contextmanager
def ledger_connection() -> Iterator[pymysql.connections.Connection]:
    if not settings.DB_PASSWORD:
        raise LedgerConfigError("DB_PASSWORD is not configured")
    conn = pymysql.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        yield conn
    finally:
        conn.close()


def find_existing_document(conn: pymysql.connections.Connection, source_doc_key: str) -> int | None:
    pattern = f'%"{source_doc_key}"%'
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM document
            WHERE vdb_id=%s
              AND is_active=1
              AND description LIKE %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (settings.TARGET_VDB_ID, pattern),
        )
        row = cur.fetchone()
    return int(row["id"]) if row else None


def _parse_description(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _session_matches(description: dict[str, Any], workflow_id: int, session_id: str) -> bool:
    return (
        int(description.get("workflow_id") or 0) == workflow_id
        and session_id in {description.get("app_session_id"), description.get("chat_id")}
    )


def _is_expired(description: dict[str, Any], now: datetime) -> bool:
    expires_at = description.get("expires_at")
    if not isinstance(expires_at, str) or not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed <= now


def list_session_documents(
    conn: pymysql.connections.Connection,
    *,
    workflow_id: int,
    session_id: str,
    include_expired: bool = False,
) -> list[dict[str, Any]]:
    pattern = f"%{session_id}%"
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                d.id AS document_id,
                d.file_name,
                d.description,
                d.reg_date,
                COALESCE(du.n_vectors, 0) AS chunk_count
            FROM document d
            LEFT JOIN document_upsert du
              ON du.doc_id=d.id AND du.is_active=1
            WHERE d.vdb_id=%s
              AND d.is_active=1
              AND d.description LIKE %s
            ORDER BY d.id DESC
            """,
            (settings.TARGET_VDB_ID, pattern),
        )
        rows = cur.fetchall()

    documents: list[dict[str, Any]] = []
    for row in rows:
        description = _parse_description(row.get("description"))
        if not _session_matches(description, workflow_id, session_id):
            continue
        expired = _is_expired(description, now)
        if expired and not include_expired:
            continue
        reg_date = row.get("reg_date")
        documents.append(
            {
                "document_id": int(row["document_id"]),
                "file_name": str(row.get("file_name") or ""),
                "temp_document_id": description.get("temp_document_id"),
                "source_doc_key": description.get("source_doc_key"),
                "source_collection": description.get("source_collection"),
                "uploaded_at": reg_date.isoformat() if hasattr(reg_date, "isoformat") else str(reg_date),
                "expires_at": description.get("expires_at"),
                "file_size_bytes": int(description.get("file_size_bytes") or 0),
                "chunk_count": int(row.get("chunk_count") or 0),
                "is_expired": expired,
            }
        )
    return documents


def insert_document(
    conn: pymysql.connections.Connection,
    *,
    file_name: str,
    description: dict[str, object],
    user_id: int,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO document
                (vdb_id, org_file_name, file_name, description,
                 reg_user_id, reg_date, mod_user_id, mod_date, is_active)
            VALUES
                (%s, %s, %s, %s, %s, NOW(), %s, NOW(), 1)
            """,
            (
                settings.TARGET_VDB_ID,
                file_name,
                file_name,
                json.dumps(description, ensure_ascii=False),
                user_id,
                user_id,
            ),
        )
        return int(cur.lastrowid)


def insert_document_upsert(
    conn: pymysql.connections.Connection,
    *,
    document_id: int,
    chunk_count: int,
    user_id: int,
) -> int:
    params = {"chunk_size": 1000, "chunk_overlap": 100, "source": "wf301-vdb-bridge"}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO document_upsert
                (vdb_id, doc_id, preprocessor_id, serving_id, serving_rev_id,
                 batch_size, params, status, start_date, runtime, error_message,
                 n_vectors, reg_user_id, reg_date, mod_user_id, mod_date,
                 is_active, is_encrypted)
            VALUES
                (%s, %s, %s, %s, %s,
                 %s, %s, %s, NOW(), 0, NULL,
                 %s, %s, NOW(), %s, NOW(),
                 1, 0)
            """,
            (
                settings.TARGET_VDB_ID,
                document_id,
                settings.PREPROCESSOR_ID,
                settings.EMBEDDING_SERVING_ID,
                settings.EMBEDDING_SERVING_REV_ID,
                settings.BATCH_SIZE,
                json.dumps(params),
                settings.JS_COMPLETE,
                chunk_count,
                user_id,
                user_id,
            ),
        )
        return int(cur.lastrowid)
