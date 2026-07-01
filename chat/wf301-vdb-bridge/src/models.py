"""Request and response models for the bridge API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from . import settings


class TempDocument(BaseModel):
    temp_document_id: int
    file_name: str
    file_path: str | None = None


class BridgeRequest(BaseModel):
    """wf301 Python Step thin payload."""

    workflow_id: int
    vdb_id: int = Field(default=settings.TARGET_VDB_ID)
    app_session_id: str
    chat_id: str | None = None
    user_id: int | None = None
    temp_documents: list[TempDocument] = Field(default_factory=list)


class SessionRequest(BaseModel):
    workflow_id: int
    vdb_id: int = Field(default=settings.TARGET_VDB_ID)
    app_session_id: str
    chat_id: str | None = None
    user_id: int | None = None


class SearchRequest(SessionRequest):
    question: str
    limit: int | None = None


class PlannedDocumentRow(BaseModel):
    vdb_id: int
    org_file_name: str
    file_name: str
    description: str
    is_active: int = 1


class PlannedUpsertRow(BaseModel):
    vdb_id: int
    doc_id_placeholder: str
    status_on_complete: str = settings.JS_COMPLETE
    n_vectors: int
    note: str = "preprocessor_id/serving_id/serving_rev_id are commit-stage required"


class DocumentPlan(BaseModel):
    temp_document_id: int
    file_name: str
    source_doc_key: str
    source_collection: str | None
    chunk_count: int
    vector_dim: int | None
    idempotency_key: str
    idempotency_status: str
    planned_document: PlannedDocumentRow | None
    planned_upsert: PlannedUpsertRow | None
    planned_vector_ids: int
    planned_139_objects: int
    notes: list[str] = Field(default_factory=list)


class DryRunResponse(BaseModel):
    mode: str = "dry_run"
    commit_enabled: bool = settings.COMMIT_ENABLED
    write_count: int = 0
    target_vdb_id: int
    target_collection: str
    workflow_id: int
    app_session_id: str
    documents: list[DocumentPlan]
    errors: list[str] = Field(default_factory=list)


class CommitDocumentResult(BaseModel):
    temp_document_id: int
    file_name: str
    source_doc_key: str
    source_collection: str | None
    document_id: int | None = None
    document_upsert_id: int | None = None
    chunk_count: int
    vector_dim: int | None
    weaviate_object_ids: list[str] = Field(default_factory=list)
    status: str
    notes: list[str] = Field(default_factory=list)


class CommitResponse(BaseModel):
    mode: str = "commit"
    commit_enabled: bool
    write_count: int
    target_vdb_id: int
    target_collection: str
    workflow_id: int
    app_session_id: str
    documents: list[CommitDocumentResult]
    committed_count: int = 0
    skipped_duplicate_count: int = 0
    session_document_count: int | None = None
    file_only_ready: bool = False
    quota: "QuotaSnapshot | None" = None
    rollback_hint: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class SessionDocument(BaseModel):
    document_id: int
    file_name: str
    temp_document_id: int | None = None
    source_doc_key: str | None = None
    source_collection: str | None = None
    uploaded_at: str
    expires_at: str | None = None
    file_size_bytes: int = 0
    chunk_count: int = 0
    is_expired: bool = False


class QuotaLimits(BaseModel):
    max_files: int
    max_per_request: int
    max_file_mb: int
    max_session_mb: int


class QuotaSnapshot(BaseModel):
    limits: QuotaLimits
    current_files: int
    current_bytes: int
    incoming_files: int = 0
    incoming_bytes: int = 0
    allowed: bool = True
    violations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DocumentsResponse(BaseModel):
    target_vdb_id: int
    workflow_id: int
    app_session_id: str
    session_id: str
    documents: list[SessionDocument]
    errors: list[str] = Field(default_factory=list)


class QuotaCheckResponse(BaseModel):
    target_vdb_id: int
    workflow_id: int
    app_session_id: str
    session_id: str
    quota: QuotaSnapshot
    errors: list[str] = Field(default_factory=list)


class FileSource(BaseModel):
    document_id: int
    file_name: str
    chunk_id: str | None = None
    i_page: int | None = None
    i_chunk_on_doc: int | None = None
    distance: float | None = None


class SearchResponse(BaseModel):
    target_vdb_id: int
    workflow_id: int
    app_session_id: str
    session_id: str
    question: str
    document_count: int
    result_count: int
    file_context: str
    file_sources: list[FileSource]
    errors: list[str] = Field(default_factory=list)
