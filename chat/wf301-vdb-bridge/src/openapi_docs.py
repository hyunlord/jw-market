"""OpenAPI documentation metadata for the wf301 file bridge."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from . import settings

API_TITLE = "JW wf301 File Bridge API"
API_VERSION = settings.OPENAPI_VERSION
SESSION_KEY_DESCRIPTION = (
    "Portal session identifier. Use either app_session_id or chat_id consistently. "
    "The temporary VDB layer stores this value in a 36-character column; values "
    "longer than 36 characters fail with temp-vdb-index error 09040008. Tested "
    "characters: letters, digits, hyphen, underscore. 36-character UUID strings "
    "are acceptable; prefixed UUIDs are not unless the total length stays <=36."
)
SESSION_KEY_PATTERN = r"^[A-Za-z0-9_-]{1,36}$"

APP_DESCRIPTION = f"""
Session-scoped wf301 file upload bridge for JW Market chat.

Flow:
1. `POST /upload` saves files into a temporary VDB index.
2. `POST /commit` registers the returned `temp_documents` into shared VDB
   `{settings.TARGET_VDB_ID}` so wf301 chat can retrieve them as `file_context`.
3. `GET /documents`, `GET /quota/check`, `POST /search`, and
   `/documents/delete` operate on the same session key.

Operational constraints:
- `workflow_id` must be `301`.
- `app_session_id` / `chat_id` must be <=36 chars; 37+ chars fail in the
  temp-vdb-index layer with `09040008`.
- Quota: {settings.QUOTA_MAX_FILES} files per session,
  {settings.QUOTA_MAX_PER_REQUEST} files per request,
  {settings.QUOTA_MAX_FILE_MB}MB per file,
  {settings.QUOTA_MAX_SESSION_MB}MB per session.
- TTL: uploaded session documents expire after {settings.TTL_DAYS} days.
- `/upload` quota.current_files may still be 0 before `/commit`; use
  `/quota/check` after commit for the authoritative session count.
"""

PATH_DOCS: dict[tuple[str, str], dict[str, str]] = {
    ("/health", "get"): {
        "summary": "Health and runtime limits",
        "description": "Returns service health, commit mode, target VDB, TTL, and active quota limits.",
    },
    ("/dry-run", "post"): {
        "summary": "Plan VDB registration without writes",
        "description": "Validates temp_documents and shows the document/document_upsert rows that /commit would create. This endpoint is read-only.",
    },
    ("/commit", "post"): {
        "summary": "Register uploaded temp documents",
        "description": "Commits /upload temp_documents into shared VDB 139 and GenOS document ledgers. Duplicate temp documents are skipped idempotently.",
    },
    ("/upload", "post"): {
        "summary": "Upload files into a session temp VDB",
        "description": (
            "Multipart upload endpoint. Send workflow_id=301 and either app_session_id or chat_id. "
            "The effective session key is chat_id when present, otherwise app_session_id. "
            f"{SESSION_KEY_DESCRIPTION} The response temp_documents array is the input to /commit. "
            "For xlsx files, the bridge applies header-value preserving chunks when /nfs-root contains the source file. "
            "quota.current_files can be 0 before commit; call /quota/check after commit for the authoritative count."
        ),
    },
    ("/documents", "get"): {
        "summary": "List committed session documents",
        "description": "Lists active documents registered for the session, including TTL expiry status and chunk counts.",
    },
    ("/documents/delete", "post"): {
        "summary": "Delete one session document",
        "description": "Deletes one committed document by document_id or temp_document_id. Provide exactly one target identifier when possible.",
    },
    ("/documents/delete", "delete"): {
        "summary": "Delete one session document",
        "description": "DELETE variant of /documents/delete with the same JSON request body and response schema.",
    },
    ("/quota/check", "get"): {
        "summary": "Check session upload quota",
        "description": "Returns current committed file count/bytes and the configured per-session and per-request upload limits.",
    },
    ("/search", "post"): {
        "summary": "Search committed files for chat context",
        "description": "Runs session-scoped vector search and returns file_context plus file_sources for wf301 chat injection.",
    },
}

FIELD_DOCS: dict[str, tuple[str, Any]] = {
    "workflow_id": ("GenOS workflow id. For wf301 file upload use 301.", 301),
    "vdb_id": ("Target shared VDB id. Current production target is 139.", settings.TARGET_VDB_ID),
    "target_vdb_id": ("Target shared VDB id used by this response.", settings.TARGET_VDB_ID),
    "target_collection": ("Target Weaviate collection backing shared VDB 139.", settings.TARGET_VDB_COLLECTION),
    "app_session_id": (SESSION_KEY_DESCRIPTION, "puc-004928"),
    "chat_id": ("Optional chat/session id override. If present, it becomes the effective session key.", "puc-004928"),
    "session_id": ("Effective session key used by the bridge: chat_id if supplied, else app_session_id.", "puc-004928"),
    "user_id": ("Optional GenOS user id for temp-vdb/preprocessor calls.", 7),
    "temp_documents": ("Temporary documents returned by /upload and consumed by /commit.", [{"temp_document_id": 709, "file_name": "TEMP_DOCUMENT_709.xlsx", "file_path": "/nfs-root/temp-document/709/TEMP_DOCUMENT_709.xlsx"}]),
    "temp_document_id": ("Temporary document id created during /upload.", 709),
    "file_name": ("Original or registered file name.", "TEMP_DOCUMENT_709.xlsx"),
    "file_path": ("NFS temp-document path used by the preprocessor.", "/nfs-root/temp-document/709/TEMP_DOCUMENT_709.xlsx"),
    "mode": ("Execution mode for this response.", "upload"),
    "commit_enabled": ("Whether /commit writes are enabled in this deployment.", True),
    "write_count": ("Number of DB/vector rows written by the operation.", 3),
    "documents": ("Document plans or committed session documents.", []),
    "errors": ("User-visible validation or downstream errors. Empty means success.", []),
    "rollback_hint": ("Operational rollback hints returned when commit/delete writes partially fail.", []),
    "temp_vdb_index_id": ("Temporary VDB index row id returned by temp-vdb-index /create.", 709),
    "temp_vdb_index": ("Temporary Weaviate collection name returned by temp-vdb-index.", "Ab12_cd34_ef56"),
    "quota": ("Quota snapshot for the effective session.", None),
    "limits": (
        "Configured upload quota limits.",
        {
            "max_files": settings.QUOTA_MAX_FILES,
            "max_per_request": settings.QUOTA_MAX_PER_REQUEST,
            "max_file_mb": settings.QUOTA_MAX_FILE_MB,
            "max_session_mb": settings.QUOTA_MAX_SESSION_MB,
        },
    ),
    "max_files": ("Maximum committed files per session.", settings.QUOTA_MAX_FILES),
    "max_per_request": ("Maximum files accepted by one /upload request.", settings.QUOTA_MAX_PER_REQUEST),
    "max_file_mb": ("Maximum size of one uploaded file in MB.", settings.QUOTA_MAX_FILE_MB),
    "max_session_mb": ("Maximum total committed session upload size in MB.", settings.QUOTA_MAX_SESSION_MB),
    "current_files": ("Current committed document count for the session.", 1),
    "current_bytes": ("Current committed file bytes for the session.", 124000),
    "incoming_files": ("Incoming files counted for an /upload quota check.", 1),
    "incoming_bytes": ("Incoming bytes counted for an /upload quota check.", 26000),
    "allowed": ("Whether the quota check allows the request.", True),
    "violations": ("Quota violation messages, if any.", []),
    "notes": ("Non-fatal processing notes.", []),
    "document_id": ("GenOS document id after /commit.", 112510),
    "document_upsert_id": ("GenOS document_upsert id after /commit.", 221900),
    "source_doc_key": ("Bridge idempotency source key for the temp document.", "temp:709:TEMP_DOCUMENT_709.xlsx"),
    "source_collection": ("Temporary Weaviate source collection copied during /commit.", "Ab12_cd34_ef56"),
    "chunk_count": ("Number of chunks copied or planned.", 18),
    "vector_dim": ("Embedding vector dimension detected in source chunks.", 3072),
    "idempotency_key": ("Stable key used to skip duplicate commits.", "wf301:puc-004928:709:TEMP_DOCUMENT_709.xlsx"),
    "idempotency_status": ("Whether a document would be inserted or skipped.", "new"),
    "planned_document": ("Document ledger row that /commit would insert.", None),
    "planned_upsert": ("Document upsert row that /commit would insert.", None),
    "planned_vector_ids": ("Number of vector ids planned for copy.", 18),
    "planned_139_objects": ("Number of shared VDB objects planned for insertion.", 18),
    "org_file_name": ("Original uploaded file name stored in document ledger.", "시장분석.xlsx"),
    "file_only_ready": ("True when files are committed and ready for later questions without a user prompt.", True),
    "committed_count": ("Number of documents committed in this call.", 1),
    "skipped_duplicate_count": ("Number of temp documents skipped because they were already committed.", 0),
    "session_document_count": ("Total committed document count after commit.", 1),
    "uploaded_at": ("Document registration timestamp.", "2026-07-06T10:00:00+09:00"),
    "expires_at": ("Session document expiry timestamp.", "2026-07-13T10:00:00+09:00"),
    "file_size_bytes": ("Uploaded file size in bytes.", 26000),
    "is_expired": ("Whether the document has passed its TTL.", False),
    "status": ("Delete or commit status.", "deleted"),
    "deleted_weaviate_object_ids": ("Shared VDB object ids deleted with the document.", ["6f42..."]),
    "question": ("User question used for file search.", "업로드한 파일의 시장 규모 요약해줘"),
    "limit": ("Optional top-k search limit. Defaults to deployment SEARCH_LIMIT.", 5),
    "document_count": ("Committed documents searched in this session.", 1),
    "result_count": ("Vector search hit count returned.", 3),
    "file_context": ("Concatenated context snippets for chat injection.", "[1] 시장분석.xlsx\\n컬럼: 값 ..."),
    "file_sources": ("Source chunks used to build file_context.", []),
    "chunk_id": ("Weaviate object id for the source chunk.", "local-xlsx-709-1"),
    "i_page": ("Page number from the preprocessor when available.", 1),
    "i_chunk_on_doc": ("Chunk index within the source document.", 1),
    "distance": ("Vector distance returned by Weaviate search.", 0.21),
}

RESPONSE_EXAMPLES: dict[tuple[str, str], dict[str, Any]] = {
    ("/health", "get"): {"status": "ok", "service": "wf301-vdb-bridge", "commit_enabled": True, "target_vdb_id": 139, "ttl_days": 7},
    ("/dry-run", "post"): {"mode": "dry-run", "commit_enabled": False, "write_count": 0, "target_vdb_id": 139, "target_collection": settings.TARGET_VDB_COLLECTION, "workflow_id": 301, "app_session_id": "puc-004928", "documents": [{"temp_document_id": 709, "file_name": "TEMP_DOCUMENT_709.xlsx", "source_doc_key": "temp:709:TEMP_DOCUMENT_709.xlsx", "source_collection": "Ab12_cd34_ef56", "idempotency_key": "wf301:puc-004928:709:TEMP_DOCUMENT_709.xlsx", "idempotency_status": "new", "planned_document": {"name": "TEMP_DOCUMENT_709.xlsx"}, "planned_upsert": {"status": "PENDING"}, "planned_vector_ids": 18, "planned_139_objects": 18, "notes": []}], "errors": []},
    ("/upload", "post"): {"mode": "upload", "target_vdb_id": 139, "workflow_id": 301, "app_session_id": "puc-004928", "session_id": "puc-004928", "temp_vdb_index_id": 709, "temp_vdb_index": "Ab12_cd34_ef56", "temp_documents": [{"temp_document_id": 709, "file_name": "TEMP_DOCUMENT_709.xlsx", "file_path": "/nfs-root/temp-document/709/TEMP_DOCUMENT_709.xlsx"}], "quota": {"limits": {"max_files": settings.QUOTA_MAX_FILES, "max_per_request": settings.QUOTA_MAX_PER_REQUEST, "max_file_mb": settings.QUOTA_MAX_FILE_MB, "max_session_mb": settings.QUOTA_MAX_SESSION_MB}, "current_files": 0, "current_bytes": 0, "incoming_files": 1, "incoming_bytes": 26000, "allowed": True, "violations": [], "notes": []}, "errors": []},
    ("/commit", "post"): {"mode": "commit", "commit_enabled": True, "write_count": 3, "target_vdb_id": 139, "target_collection": settings.TARGET_VDB_COLLECTION, "workflow_id": 301, "app_session_id": "puc-004928", "documents": [{"temp_document_id": 709, "file_name": "TEMP_DOCUMENT_709.xlsx", "source_doc_key": "temp:709:TEMP_DOCUMENT_709.xlsx", "source_collection": "Ab12_cd34_ef56", "document_id": 112510, "document_upsert_id": 221900, "chunk_count": 18, "vector_dim": 3072, "weaviate_object_ids": ["6f42..."], "status": "committed", "notes": []}], "committed_count": 1, "skipped_duplicate_count": 0, "session_document_count": 1, "file_only_ready": True, "quota": None, "rollback_hint": [], "errors": []},
    ("/documents", "get"): {"target_vdb_id": 139, "workflow_id": 301, "app_session_id": "puc-004928", "session_id": "puc-004928", "documents": [{"document_id": 112510, "file_name": "TEMP_DOCUMENT_709.xlsx", "temp_document_id": 709, "source_doc_key": "temp:709:TEMP_DOCUMENT_709.xlsx", "source_collection": "Ab12_cd34_ef56", "uploaded_at": "2026-07-06T10:00:00+09:00", "expires_at": "2026-07-13T10:00:00+09:00", "file_size_bytes": 26000, "chunk_count": 18, "is_expired": False}], "errors": []},
    ("/quota/check", "get"): {"target_vdb_id": 139, "workflow_id": 301, "app_session_id": "puc-004928", "session_id": "puc-004928", "quota": {"limits": {"max_files": settings.QUOTA_MAX_FILES, "max_per_request": settings.QUOTA_MAX_PER_REQUEST, "max_file_mb": settings.QUOTA_MAX_FILE_MB, "max_session_mb": settings.QUOTA_MAX_SESSION_MB}, "current_files": 1, "current_bytes": 26000, "incoming_files": 0, "incoming_bytes": 0, "allowed": True, "violations": [], "notes": []}, "errors": []},
    ("/documents/delete", "post"): {"target_vdb_id": 139, "workflow_id": 301, "app_session_id": "puc-004928", "session_id": "puc-004928", "document_id": 112510, "temp_document_id": 709, "status": "deleted", "write_count": 19, "deleted_weaviate_object_ids": ["6f42..."], "rollback_hint": [], "errors": []},
    ("/documents/delete", "delete"): {"target_vdb_id": 139, "workflow_id": 301, "app_session_id": "puc-004928", "session_id": "puc-004928", "document_id": 112510, "temp_document_id": 709, "status": "deleted", "write_count": 19, "deleted_weaviate_object_ids": ["6f42..."], "rollback_hint": [], "errors": []},
    ("/search", "post"): {"target_vdb_id": 139, "workflow_id": 301, "app_session_id": "puc-004928", "session_id": "puc-004928", "question": "업로드한 파일의 시장 규모 요약해줘", "document_count": 1, "result_count": 3, "file_context": "[1] 시장분석.xlsx (document_id=112510)\\n컬럼명: 값 ...", "file_sources": [{"document_id": 112510, "file_name": "시장분석.xlsx", "chunk_id": "local-xlsx-709-1", "i_page": 1, "i_chunk_on_doc": 1, "distance": 0.21}], "errors": []},
}


def configure_openapi_docs(app: FastAPI) -> None:
    """Install the self-contained portal OpenAPI schema."""

    app.title = API_TITLE
    app.version = API_VERSION
    app.description = APP_DESCRIPTION

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=API_TITLE,
            version=API_VERSION,
            description=APP_DESCRIPTION,
            routes=app.routes,
            servers=app.servers,
        )
        _apply_path_docs(schema)
        _apply_component_docs(schema)
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi


def _apply_path_docs(schema: dict[str, Any]) -> None:
    paths = schema.get("paths", {})
    for (path, method), docs in PATH_DOCS.items():
        operation = paths.get(path, {}).get(method)
        if not isinstance(operation, dict):
            continue
        operation.update(docs)
        responses = operation.setdefault("responses", {})
        ok_response = responses.setdefault("200", {})
        ok_response.setdefault("description", "Successful response")
        example = RESPONSE_EXAMPLES.get((path, method))
        if example is not None:
            content = ok_response.setdefault("content", {}).setdefault("application/json", {})
            content["example"] = example
        _apply_parameter_docs(operation)


def _apply_parameter_docs(operation: dict[str, Any]) -> None:
    for parameter in operation.get("parameters", []):
        if not isinstance(parameter, dict):
            continue
        name = str(parameter.get("name", ""))
        docs = FIELD_DOCS.get(name)
        if docs is None:
            continue
        description, example = docs
        parameter["description"] = description
        schema = parameter.setdefault("schema", {})
        if name in {"app_session_id", "chat_id"}:
            schema["maxLength"] = 36
            schema["pattern"] = SESSION_KEY_PATTERN
        parameter["example"] = example


def _apply_component_docs(schema: dict[str, Any]) -> None:
    components = schema.get("components", {}).get("schemas", {})
    for component in components.values():
        if not isinstance(component, dict):
            continue
        properties = component.get("properties", {})
        if not isinstance(properties, dict):
            continue
        for name, prop in properties.items():
            if not isinstance(prop, dict):
                continue
            docs = FIELD_DOCS.get(str(name))
            if docs is None:
                continue
            description, example = docs
            prop["description"] = description
            if name in {"app_session_id", "chat_id"}:
                prop["maxLength"] = 36
                prop["pattern"] = SESSION_KEY_PATTERN
            prop["examples"] = [example]
