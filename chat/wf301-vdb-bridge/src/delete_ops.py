"""Document delete ledger operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pymysql

from . import ledger, session_wiki, settings, weaviate_ops
from .file_sql import FileSqlNotFoundError, FileSqlRejectedError, drop_logical_table
from .logging_utils import safe_log
from .models import DeleteDocumentRequest, DeleteDocumentResponse


@dataclass(frozen=True, slots=True)
class DeleteTarget:
    document_id: int
    file_name: str
    temp_document_id: int | None
    idempotency_key: str | None
    is_active: bool
    authorized: bool
    storage_route: str
    sql_logical_names: tuple[str, ...]


def _target_from_row(row: dict[str, Any], *, workflow_id: int, session_id: str) -> DeleteTarget:
    description = ledger._parse_description(row.get("description"))
    raw_tables = description.get("sql_tables")
    sql_logical_names = tuple(
        str(table.get("logical_name"))
        for table in raw_tables
        if isinstance(table, dict) and table.get("logical_name")
    ) if isinstance(raw_tables, list) else ()
    return DeleteTarget(
        document_id=int(row["document_id"]),
        file_name=str(row.get("file_name") or ""),
        temp_document_id=(
            int(description["temp_document_id"])
            if description.get("temp_document_id") is not None
            else None
        ),
        idempotency_key=(
            str(description["idempotency_key"])
            if description.get("idempotency_key") is not None
            else None
        ),
        is_active=bool(row.get("is_active")),
        authorized=ledger._session_matches(description, workflow_id, session_id),
        storage_route=str(description.get("storage_route") or "vdb"),
        sql_logical_names=sql_logical_names,
    )


def find_delete_target(
    conn: pymysql.connections.Connection,
    *,
    workflow_id: int,
    session_id: str,
    document_id: int | None,
    temp_document_id: int | None,
) -> DeleteTarget | None:
    if document_id is not None:
        return _find_by_document_id(
            conn,
            workflow_id=workflow_id,
            session_id=session_id,
            document_id=document_id,
        )
    if temp_document_id is not None:
        return _find_by_temp_document_id(
            conn,
            workflow_id=workflow_id,
            session_id=session_id,
            temp_document_id=temp_document_id,
        )
    return None


def _find_by_document_id(
    conn: pymysql.connections.Connection,
    *,
    workflow_id: int,
    session_id: str,
    document_id: int,
) -> DeleteTarget | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id AS document_id, file_name, description, is_active
            FROM document
            WHERE vdb_id=%s
              AND id=%s
            LIMIT 1
            """,
            (settings.TARGET_VDB_ID, document_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    return _target_from_row(row, workflow_id=workflow_id, session_id=session_id)


def _find_by_temp_document_id(
    conn: pymysql.connections.Connection,
    *,
    workflow_id: int,
    session_id: str,
    temp_document_id: int,
) -> DeleteTarget | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id AS document_id, file_name, description, is_active
            FROM document
            WHERE vdb_id=%s
              AND description LIKE %s
            ORDER BY id DESC
            """,
            (settings.TARGET_VDB_ID, f"%{temp_document_id}%"),
        )
        rows = cur.fetchall()
    for row in rows:
        description = ledger._parse_description(row.get("description"))
        if description.get("temp_document_id") == temp_document_id:
            return _target_from_row(row, workflow_id=workflow_id, session_id=session_id)
    return None


def soft_delete_document(
    conn: pymysql.connections.Connection,
    *,
    document_id: int,
    user_id: int,
) -> int:
    with conn.cursor() as cur:
        document_count = cur.execute(
            """
            UPDATE document
            SET is_active=0,
                mod_user_id=%s,
                mod_date=NOW()
            WHERE vdb_id=%s
              AND id=%s
              AND is_active=1
            """,
            (user_id, settings.TARGET_VDB_ID, document_id),
        )
        upsert_count = cur.execute(
            """
            UPDATE document_upsert
            SET is_active=0,
                mod_user_id=%s,
                mod_date=NOW()
            WHERE vdb_id=%s
              AND doc_id=%s
              AND is_active=1
            """,
            (user_id, settings.TARGET_VDB_ID, document_id),
        )
    return int(document_count) + int(upsert_count)


def error_response(
    req: DeleteDocumentRequest,
    *,
    session_id: str,
    status: str,
    errors: list[str],
) -> DeleteDocumentResponse:
    return DeleteDocumentResponse(
        target_vdb_id=req.vdb_id,
        workflow_id=req.workflow_id,
        app_session_id=req.app_session_id,
        session_id=session_id,
        document_id=req.document_id,
        temp_document_id=req.temp_document_id,
        status=status,
        errors=errors,
    )


def delete_session_document(
    req: DeleteDocumentRequest,
    *,
    session_id: str,
    user_id: int,
) -> DeleteDocumentResponse:
    with ledger.ledger_connection() as conn:
        target = find_delete_target(
            conn,
            workflow_id=req.workflow_id,
            session_id=session_id,
            document_id=req.document_id,
            temp_document_id=req.temp_document_id,
        )
        if target is None:
            return error_response(
                req,
                session_id=session_id,
                status="not_found",
                errors=["document not found"],
            )
        if not target.authorized:
            return error_response(
                req,
                session_id=session_id,
                status="not_found",
                errors=["document not found"],
            )
        if not target.is_active:
            return DeleteDocumentResponse(
                target_vdb_id=req.vdb_id,
                workflow_id=req.workflow_id,
                app_session_id=req.app_session_id,
                session_id=session_id,
                document_id=target.document_id,
                temp_document_id=target.temp_document_id,
                status="already_deleted",
                rollback_hint=[f"document_id={target.document_id}: already inactive"],
            )
        try:
            if target.storage_route == "sql":
                deleted_ids = []
            else:
                with httpx.Client() as client:
                    deleted_ids = weaviate_ops.delete_target_objects_for_document(
                        client,
                        document_id=target.document_id,
                    )
            ledger_updates = soft_delete_document(
                conn,
                document_id=target.document_id,
                user_id=user_id,
            )
            if target.storage_route != "sql":
                session_wiki.mark_pages_stale(conn, req.workflow_id, session_id)
            conn.commit()
        except (httpx.HTTPError, pymysql.MySQLError):
            conn.rollback()
            raise

    for logical_name in target.sql_logical_names:
        try:
            drop_logical_table(session_id, logical_name)
        except (FileSqlNotFoundError, FileSqlRejectedError):
            safe_log(
                "file_sql_delete_missing",
                document_id=target.document_id,
                logical_name=logical_name,
            )

    safe_log(
        "delete_done",
        workflow_id=req.workflow_id,
        document_id=target.document_id,
        object_count=len(deleted_ids),
    )
    return DeleteDocumentResponse(
        target_vdb_id=req.vdb_id,
        workflow_id=req.workflow_id,
        app_session_id=req.app_session_id,
        session_id=session_id,
        document_id=target.document_id,
        temp_document_id=target.temp_document_id,
        status="deleted",
        write_count=ledger_updates + len(deleted_ids),
        deleted_weaviate_object_ids=deleted_ids,
        rollback_hint=[
            f"document_id={target.document_id}: set document/document_upsert is_active=1",
            "reimport Weaviate objects from the original temp chunks if vector restore is required",
        ],
    )
