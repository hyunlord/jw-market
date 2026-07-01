"""wf301 VDB registration bridge.

The bridge copies already-preprocessed Temp VDB chunks into registered VDB 139
and creates the GenOS document/document_upsert ledger rows required for portal
list/delete behavior. The `/dry-run` endpoint is read-only; `/commit` is gated
by COMMIT_ENABLED and DB credentials supplied through Kubernetes secrets.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import FastAPI

from . import delete_ops, ledger, settings, weaviate_ops
from .logging_utils import safe_log
from .models import (
    BridgeRequest,
    CommitDocumentResult,
    CommitResponse,
    DeleteDocumentRequest,
    DeleteDocumentResponse,
    DocumentPlan,
    DocumentsResponse,
    FileSource,
    DryRunResponse,
    PlannedDocumentRow,
    PlannedUpsertRow,
    QuotaCheckResponse,
    QuotaLimits,
    QuotaSnapshot,
    SearchRequest,
    SearchResponse,
    SessionDocument,
    SessionRequest,
)

app = FastAPI(title="wf301-vdb-bridge", version="api-0.1.0")


def _guard(req: BridgeRequest) -> list[str]:
    errors: list[str] = []
    if req.workflow_id not in settings.ALLOWED_WORKFLOW_IDS:
        errors.append(f"workflow_id {req.workflow_id} not allowed")
    if req.vdb_id != settings.TARGET_VDB_ID:
        errors.append(f"vdb_id {req.vdb_id} != target {settings.TARGET_VDB_ID}")
    return errors


def _session_guard(req: SessionRequest) -> list[str]:
    errors: list[str] = []
    if req.workflow_id not in settings.ALLOWED_WORKFLOW_IDS:
        errors.append(f"workflow_id {req.workflow_id} not allowed")
    if req.vdb_id != settings.TARGET_VDB_ID:
        errors.append(f"vdb_id {req.vdb_id} != target {settings.TARGET_VDB_ID}")
    return errors


def _session_id(req: SessionRequest | BridgeRequest) -> str:
    return req.chat_id or req.app_session_id


def _expires_at() -> str:
    expires = datetime.now(timezone.utc) + timedelta(days=settings.TTL_DAYS)
    return expires.isoformat()


def _quota_limits() -> QuotaLimits:
    return QuotaLimits(
        max_files=settings.QUOTA_MAX_FILES,
        max_per_request=settings.QUOTA_MAX_PER_REQUEST,
        max_file_mb=settings.QUOTA_MAX_FILE_MB,
        max_session_mb=settings.QUOTA_MAX_SESSION_MB,
    )


def _quota_snapshot(
    current_docs: list[dict[str, Any]],
    *,
    incoming_files: int = 0,
    incoming_bytes: int = 0,
    incoming_file_sizes: list[int] | None = None,
) -> QuotaSnapshot:
    limits = _quota_limits()
    current_bytes = sum(int(doc.get("file_size_bytes") or 0) for doc in current_docs)
    violations: list[str] = []
    notes: list[str] = []
    if incoming_files > limits.max_per_request:
        violations.append(f"request file count {incoming_files} exceeds {limits.max_per_request}")
    if len(current_docs) + incoming_files > limits.max_files:
        violations.append(f"session file count would exceed {limits.max_files}")
    max_file_bytes = limits.max_file_mb * 1024 * 1024
    for size in incoming_file_sizes or []:
        if size > max_file_bytes:
            violations.append(f"file size {size} exceeds {max_file_bytes}")
    if incoming_file_sizes and any(size == 0 for size in incoming_file_sizes):
        notes.append("one or more incoming files had no file_size metadata")
    max_session_bytes = limits.max_session_mb * 1024 * 1024
    if current_bytes + incoming_bytes > max_session_bytes:
        violations.append(f"session bytes would exceed {max_session_bytes}")
    return QuotaSnapshot(
        limits=limits,
        current_files=len(current_docs),
        current_bytes=current_bytes,
        incoming_files=incoming_files,
        incoming_bytes=incoming_bytes,
        allowed=not violations,
        violations=violations,
        notes=notes,
    )


def _description(
    req: BridgeRequest,
    *,
    source_doc_key: str,
    idempotency_key: str,
    temp_document_id: int,
    source_collection: str | None,
    expires_at: str,
    file_size_bytes: int,
) -> dict[str, object]:
    return {
        "workflow_id": req.workflow_id,
        "app_session_id": req.app_session_id,
        "chat_id": req.chat_id,
        "user_id": req.user_id,
        "temp_document_id": temp_document_id,
        "source_doc_key": source_doc_key,
        "idempotency_key": idempotency_key,
        "source_collection": source_collection,
        "expires_at": expires_at,
        "ttl_days": settings.TTL_DAYS,
        "file_size_bytes": file_size_bytes,
        "bridge": "wf301-vdb-bridge",
    }


def _load_temp_chunks(
    client: httpx.Client, temp_document_id: int
) -> tuple[str | None, list[weaviate_ops.Chunk], list[str]]:
    notes: list[str] = []
    classes = weaviate_ops.schema_classes(client)
    candidates = weaviate_ops.candidate_temp_classes(classes)
    collection = weaviate_ops.resolve_temp_collection(client, candidates, temp_document_id)
    if not collection:
        return None, [], ["temp collection not resolved (no candidate matched)"]
    try:
        chunks = weaviate_ops.read_temp_chunks(client, collection, temp_document_id)
    except httpx.HTTPError as exc:
        return collection, [], [f"chunk read failed: {exc}"]
    if not chunks:
        notes.append("no chunks found for temp document")
    if chunks and weaviate_ops.first_vector_dim(chunks) is None:
        notes.append("vector not present in chunk read")
    return collection, chunks, notes


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "wf301-vdb-bridge",
        "mode": "commit" if settings.COMMIT_ENABLED else "dry_run",
        "commit_enabled": settings.COMMIT_ENABLED,
        "db_configured": bool(settings.DB_PASSWORD),
        "target_vdb_id": settings.TARGET_VDB_ID,
        "paths": [
            "/health",
            "/dry-run",
            "/commit",
            "/documents",
            "/documents/delete",
            "/quota/check",
            "/search",
        ],
        "ttl_days": settings.TTL_DAYS,
        "quota": _quota_limits().model_dump(),
    }


@app.post("/dry-run", response_model=DryRunResponse)
def dry_run(req: BridgeRequest) -> DryRunResponse:
    errors = _guard(req)
    plans: list[DocumentPlan] = []
    if errors and any("not allowed" in item for item in errors):
        return DryRunResponse(
            target_vdb_id=req.vdb_id,
            target_collection=settings.TARGET_VDB_COLLECTION,
            workflow_id=req.workflow_id,
            app_session_id=req.app_session_id,
            documents=[],
            errors=errors,
        )

    session_id = _session_id(req)
    with httpx.Client() as client:
        for temp_doc in req.temp_documents:
            source_doc_key = f"temp:{temp_doc.temp_document_id}:{temp_doc.file_name}"
            idempotency_key = f"wf301:{session_id}:{temp_doc.temp_document_id}:{temp_doc.file_name}"
            collection, chunks, notes = _load_temp_chunks(client, temp_doc.temp_document_id)
            vector_dim = weaviate_ops.first_vector_dim(chunks)
            file_size_bytes = weaviate_ops.max_file_size_bytes(chunks)
            description = json.dumps(
                _description(
                    req,
                    source_doc_key=source_doc_key,
                    idempotency_key=idempotency_key,
                    temp_document_id=temp_doc.temp_document_id,
                    source_collection=collection,
                    expires_at=_expires_at(),
                    file_size_bytes=file_size_bytes,
                ),
                ensure_ascii=False,
            )
            planned_doc = None
            planned_upsert = None
            if chunks:
                planned_doc = PlannedDocumentRow(
                    vdb_id=req.vdb_id,
                    org_file_name=temp_doc.file_name,
                    file_name=temp_doc.file_name,
                    description=description,
                )
                planned_upsert = PlannedUpsertRow(
                    vdb_id=req.vdb_id,
                    doc_id_placeholder="<document.id at commit>",
                    n_vectors=len(chunks),
                )
            plans.append(
                DocumentPlan(
                    temp_document_id=temp_doc.temp_document_id,
                    file_name=temp_doc.file_name,
                    source_doc_key=source_doc_key,
                    source_collection=collection,
                    chunk_count=len(chunks),
                    vector_dim=vector_dim,
                    idempotency_key=idempotency_key,
                    idempotency_status="new",
                    planned_document=planned_doc,
                    planned_upsert=planned_upsert,
                    planned_vector_ids=0,
                    planned_139_objects=len(chunks),
                    notes=notes,
                )
            )

    safe_log("dry_run_done", workflow_id=req.workflow_id, doc_count=len(plans), errors=len(errors))
    return DryRunResponse(
        target_vdb_id=req.vdb_id,
        target_collection=settings.TARGET_VDB_COLLECTION,
        workflow_id=req.workflow_id,
        app_session_id=req.app_session_id,
        documents=plans,
        errors=errors,
    )


@app.post("/commit", response_model=CommitResponse)
def commit(req: BridgeRequest) -> CommitResponse:
    errors = _guard(req)
    if not settings.COMMIT_ENABLED:
        errors.append("commit stage is disabled")
    if errors:
        return CommitResponse(
            commit_enabled=settings.COMMIT_ENABLED,
            write_count=0,
            target_vdb_id=req.vdb_id,
            target_collection=settings.TARGET_VDB_COLLECTION,
            workflow_id=req.workflow_id,
            app_session_id=req.app_session_id,
            documents=[],
            errors=errors,
        )

    session_id = _session_id(req)
    user_id = req.user_id or settings.DEFAULT_USER_ID
    results: list[CommitDocumentResult] = []
    rollback: list[str] = []
    write_count = 0
    committed_count = 0
    skipped_duplicate_count = 0
    quota: QuotaSnapshot | None = None

    with httpx.Client() as client, ledger.ledger_connection() as conn:
        current_docs = ledger.list_session_documents(
            conn,
            workflow_id=req.workflow_id,
            session_id=session_id,
        )
        prepared: list[dict[str, Any]] = []
        for temp_doc in req.temp_documents:
            source_doc_key = f"temp:{temp_doc.temp_document_id}:{temp_doc.file_name}"
            idempotency_key = f"wf301:{session_id}:{temp_doc.temp_document_id}:{temp_doc.file_name}"
            collection, chunks, notes = _load_temp_chunks(client, temp_doc.temp_document_id)
            vector_dim = weaviate_ops.first_vector_dim(chunks)
            file_size_bytes = weaviate_ops.max_file_size_bytes(chunks)
            existing_doc_id = ledger.find_existing_document(conn, source_doc_key) if chunks else None
            prepared.append(
                {
                    "temp_doc": temp_doc,
                    "source_doc_key": source_doc_key,
                    "idempotency_key": idempotency_key,
                    "collection": collection,
                    "chunks": chunks,
                    "notes": notes,
                    "vector_dim": vector_dim,
                    "file_size_bytes": file_size_bytes,
                    "existing_doc_id": existing_doc_id,
                }
            )

        new_items = [item for item in prepared if item["chunks"] and item["existing_doc_id"] is None]
        incoming_sizes = [int(item["file_size_bytes"] or 0) for item in new_items]
        quota = _quota_snapshot(
            current_docs,
            incoming_files=len(new_items),
            incoming_bytes=sum(incoming_sizes),
            incoming_file_sizes=incoming_sizes,
        )
        if not quota.allowed:
            return CommitResponse(
                commit_enabled=settings.COMMIT_ENABLED,
                write_count=0,
                target_vdb_id=req.vdb_id,
                target_collection=settings.TARGET_VDB_COLLECTION,
                workflow_id=req.workflow_id,
                app_session_id=req.app_session_id,
                documents=[],
                session_document_count=len(current_docs),
                quota=quota,
                errors=quota.violations,
            )

        for item in prepared:
            temp_doc = item["temp_doc"]
            source_doc_key = item["source_doc_key"]
            idempotency_key = item["idempotency_key"]
            collection = item["collection"]
            chunks = item["chunks"]
            notes = item["notes"]
            vector_dim = item["vector_dim"]
            file_size_bytes = int(item["file_size_bytes"] or 0)
            if not chunks:
                results.append(
                    CommitDocumentResult(
                        temp_document_id=temp_doc.temp_document_id,
                        file_name=temp_doc.file_name,
                        source_doc_key=source_doc_key,
                        source_collection=collection,
                        chunk_count=0,
                        vector_dim=None,
                        status="no_chunks",
                        notes=notes,
                    )
                )
                continue

            existing_doc_id = item["existing_doc_id"]
            if existing_doc_id is not None:
                skipped_duplicate_count += 1
                results.append(
                    CommitDocumentResult(
                        temp_document_id=temp_doc.temp_document_id,
                        file_name=temp_doc.file_name,
                        source_doc_key=source_doc_key,
                        source_collection=collection,
                        document_id=existing_doc_id,
                        chunk_count=len(chunks),
                        vector_dim=vector_dim,
                        status="skipped_duplicate",
                        notes=["existing active document found by source_doc_key"],
                    )
                )
                continue

            try:
                description = _description(
                    req,
                    source_doc_key=source_doc_key,
                    idempotency_key=idempotency_key,
                    temp_document_id=temp_doc.temp_document_id,
                    source_collection=collection,
                    expires_at=_expires_at(),
                    file_size_bytes=file_size_bytes,
                )
                document_id = ledger.insert_document(
                    conn,
                    file_name=temp_doc.file_name,
                    description=description,
                    user_id=user_id,
                )
                upsert_id = ledger.insert_document_upsert(
                    conn,
                    document_id=document_id,
                    chunk_count=len(chunks),
                    user_id=user_id,
                )
                object_ids = weaviate_ops.copy_chunks_to_target(
                    client,
                    chunks,
                    document_id=document_id,
                    file_name=temp_doc.file_name,
                    idempotency_key=idempotency_key,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            write_count += 2 + len(object_ids)
            committed_count += 1
            rollback.append(
                f"document_id={document_id}: set document/document_upsert is_active=0 "
                f"and delete Weaviate object ids {object_ids}"
            )
            results.append(
                CommitDocumentResult(
                    temp_document_id=temp_doc.temp_document_id,
                    file_name=temp_doc.file_name,
                    source_doc_key=source_doc_key,
                    source_collection=collection,
                    document_id=document_id,
                    document_upsert_id=upsert_id,
                    chunk_count=len(chunks),
                    vector_dim=vector_dim,
                    weaviate_object_ids=object_ids,
                    status="committed",
                    notes=notes,
                )
            )

        session_document_count = len(
            ledger.list_session_documents(
                conn,
                workflow_id=req.workflow_id,
                session_id=session_id,
            )
        )

    safe_log("commit_done", workflow_id=req.workflow_id, docs=len(results), write_count=write_count)
    return CommitResponse(
        commit_enabled=settings.COMMIT_ENABLED,
        write_count=write_count,
        target_vdb_id=req.vdb_id,
        target_collection=settings.TARGET_VDB_COLLECTION,
        workflow_id=req.workflow_id,
        app_session_id=req.app_session_id,
        documents=results,
        committed_count=committed_count,
        skipped_duplicate_count=skipped_duplicate_count,
        session_document_count=session_document_count,
        file_only_ready=bool(results) and not any(item.status == "no_chunks" for item in results),
        quota=quota,
        rollback_hint=rollback,
    )


def _documents_from_rows(rows: list[dict[str, Any]]) -> list[SessionDocument]:
    return [SessionDocument(**row) for row in rows]


def _session_request(
    *,
    workflow_id: int,
    app_session_id: str,
    chat_id: str | None,
    user_id: int | None,
    vdb_id: int,
) -> SessionRequest:
    return SessionRequest(
        workflow_id=workflow_id,
        vdb_id=vdb_id,
        app_session_id=app_session_id,
        chat_id=chat_id,
        user_id=user_id,
    )


@app.get("/documents", response_model=DocumentsResponse)
def documents(
    workflow_id: int,
    app_session_id: str,
    chat_id: str | None = None,
    user_id: int | None = None,
    vdb_id: int = settings.TARGET_VDB_ID,
) -> DocumentsResponse:
    req = _session_request(
        workflow_id=workflow_id,
        app_session_id=app_session_id,
        chat_id=chat_id,
        user_id=user_id,
        vdb_id=vdb_id,
    )
    errors = _session_guard(req)
    session_id = _session_id(req)
    if errors:
        return DocumentsResponse(
            target_vdb_id=vdb_id,
            workflow_id=workflow_id,
            app_session_id=app_session_id,
            session_id=session_id,
            documents=[],
            errors=errors,
        )
    with ledger.ledger_connection() as conn:
        rows = ledger.list_session_documents(
            conn,
            workflow_id=workflow_id,
            session_id=session_id,
        )
    return DocumentsResponse(
        target_vdb_id=vdb_id,
        workflow_id=workflow_id,
        app_session_id=app_session_id,
        session_id=session_id,
        documents=_documents_from_rows(rows),
    )


@app.post("/documents/delete", response_model=DeleteDocumentResponse)
@app.delete("/documents/delete", response_model=DeleteDocumentResponse)
def delete_document(req: DeleteDocumentRequest) -> DeleteDocumentResponse:
    errors = _session_guard(req)
    session_id = _session_id(req)
    if req.document_id is None and req.temp_document_id is None:
        errors.append("document_id or temp_document_id is required")
    if errors:
        return delete_ops.error_response(
            req,
            session_id=session_id,
            status="rejected",
            errors=errors,
        )
    return delete_ops.delete_session_document(
        req,
        session_id=session_id,
        user_id=req.user_id or settings.DEFAULT_USER_ID,
    )


@app.get("/quota/check", response_model=QuotaCheckResponse)
def quota_check(
    workflow_id: int,
    app_session_id: str,
    chat_id: str | None = None,
    user_id: int | None = None,
    vdb_id: int = settings.TARGET_VDB_ID,
) -> QuotaCheckResponse:
    req = _session_request(
        workflow_id=workflow_id,
        app_session_id=app_session_id,
        chat_id=chat_id,
        user_id=user_id,
        vdb_id=vdb_id,
    )
    errors = _session_guard(req)
    session_id = _session_id(req)
    rows: list[dict[str, Any]] = []
    if not errors:
        with ledger.ledger_connection() as conn:
            rows = ledger.list_session_documents(
                conn,
                workflow_id=workflow_id,
                session_id=session_id,
            )
    return QuotaCheckResponse(
        target_vdb_id=vdb_id,
        workflow_id=workflow_id,
        app_session_id=app_session_id,
        session_id=session_id,
        quota=_quota_snapshot(rows),
        errors=errors,
    )


def _context_from_hits(hits: list[dict[str, Any]]) -> tuple[str, list[FileSource]]:
    lines: list[str] = []
    sources: list[FileSource] = []
    used_chars = 0
    for index, hit in enumerate(hits, start=1):
        text = str(hit.get("text") or "").strip()
        if not text:
            continue
        remaining = settings.SEARCH_CONTEXT_CHAR_LIMIT - used_chars
        if remaining <= 0:
            break
        clipped = text[:remaining]
        used_chars += len(clipped)
        doc_id = int(hit.get("doc_id") or 0)
        file_name = str(hit.get("file_name") or "")
        lines.append(f"[{index}] {file_name} (document_id={doc_id})\n{clipped}")
        additional = hit.get("_additional") or {}
        sources.append(
            FileSource(
                document_id=doc_id,
                file_name=file_name,
                chunk_id=additional.get("id"),
                i_page=hit.get("i_page"),
                i_chunk_on_doc=hit.get("i_chunk_on_doc"),
                distance=additional.get("distance"),
            )
        )
    return "\n\n".join(lines), sources


@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest) -> SearchResponse:
    errors = _session_guard(req)
    session_id = _session_id(req)
    if errors:
        return SearchResponse(
            target_vdb_id=req.vdb_id,
            workflow_id=req.workflow_id,
            app_session_id=req.app_session_id,
            session_id=session_id,
            question=req.question,
            document_count=0,
            result_count=0,
            file_context="",
            file_sources=[],
            errors=errors,
        )
    with ledger.ledger_connection() as conn:
        rows = ledger.list_session_documents(
            conn,
            workflow_id=req.workflow_id,
            session_id=session_id,
        )
    doc_ids = [int(row["document_id"]) for row in rows]
    if not doc_ids:
        return SearchResponse(
            target_vdb_id=req.vdb_id,
            workflow_id=req.workflow_id,
            app_session_id=req.app_session_id,
            session_id=session_id,
            question=req.question,
            document_count=0,
            result_count=0,
            file_context="",
            file_sources=[],
        )
    with httpx.Client() as client:
        vector = weaviate_ops.embed_text(client, req.question)
        hits = weaviate_ops.search_target_chunks(
            client,
            vector=vector,
            doc_ids=doc_ids,
            limit=req.limit or settings.SEARCH_LIMIT,
        )
    file_context, file_sources = _context_from_hits(hits)
    return SearchResponse(
        target_vdb_id=req.vdb_id,
        workflow_id=req.workflow_id,
        app_session_id=req.app_session_id,
        session_id=session_id,
        question=req.question,
        document_count=len(doc_ids),
        result_count=len(hits),
        file_context=file_context,
        file_sources=file_sources,
    )
