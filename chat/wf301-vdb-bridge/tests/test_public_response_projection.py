from __future__ import annotations

import json

import src.models as models
from src.main import app


FORBIDDEN_PUBLIC_FIELDS = {
    "app_session_id",
    "chunk_id",
    "document_id",
    "document_upsert_id",
    "errors",
    "file_path",
    "i_chunk_on_doc",
    "rollback_hint",
    "session_id",
    "source_collection",
    "source_doc_key",
    "target_collection",
    "target_vdb_id",
    "temp_document_id",
    "temp_vdb_index",
    "temp_vdb_index_id",
    "weaviate_object_ids",
    "workflow_id",
}


def _encoded(model: type[models.BaseModel], raw: dict[str, object]) -> str:
    return json.dumps(model.model_validate(raw).model_dump())


def test_public_routes_use_projection_models_without_changing_file_sql() -> None:
    route_models = {
        route.path: route.response_model
        for route in app.routes
        if hasattr(route, "response_model")
    }

    assert route_models["/upload"] is models.PublicUploadResponse
    assert route_models["/upload/status"] is models.PublicUploadStatusResponse
    assert route_models["/commit"] is models.PublicCommitResponse
    assert route_models["/search"] is models.PublicSearchResponse
    assert route_models["/documents"] is models.PublicDocumentsResponse
    assert route_models["/file-sql/schema"] is models.FileSqlSchemaResponse
    assert route_models["/file-sql/query"] is models.FileSqlQueryResponse


def test_upload_status_projection_hides_session_and_storage_details() -> None:
    raw = {
        "upload_id": "upl_7Qz4R4R2Xh9pCkN8",
        "workflow_id": 301,
        "session_id": "session-a",
        "temp_document_id": 1601,
        "file_path": "/private/wide.xlsx",
        "state": "preprocessing",
        "ready": False,
        "files": [
            {
                "file_name": "wide.xlsx",
                "state": "preprocessing",
                "route": None,
                "message": None,
            }
        ],
        "message": None,
        "updated_at": "2026-07-18T00:00:00+00:00",
        "expires_at": "2026-07-19T00:00:00+00:00",
    }

    encoded = _encoded(models.PublicUploadStatusResponse, raw)

    assert not any(field in encoded for field in FORBIDDEN_PUBLIC_FIELDS)
    assert "wide.xlsx" in encoded


def test_upload_projection_hides_internal_fields_and_keeps_sql_contract() -> None:
    raw = {
        "mode": "upload",
        "target_vdb_id": 139,
        "workflow_id": 301,
        "app_session_id": "session-a",
        "session_id": "session-a",
        "temp_vdb_index_id": 1705,
        "temp_vdb_index": "InternalCollection",
        "temp_documents": [{"temp_document_id": 1601, "file_name": "survey.xlsx", "file_path": "/tmp/private.xlsx"}],
        "commit": {
            "mode": "commit",
            "target_vdb_id": 139,
            "target_collection": "InternalTarget",
            "workflow_id": 301,
            "app_session_id": "session-a",
            "rollback_hint": ["delete object abc"],
            "documents": [{
                "temp_document_id": 1601,
                "document_id": 113052,
                "document_upsert_id": 112938,
                "file_name": "survey.xlsx",
                "source_collection": "InternalSource",
                "weaviate_object_ids": ["object-1"],
                "chunk_count": 2,
                "route": "sql",
                "status": "committed_sql",
                "sql_tables": [{"logical_name": "data_abc", "sheet_name": "Raw", "row_count": 10, "column_count": 4}],
            }],
            "committed_count": 1,
            "skipped_duplicate_count": 0,
            "file_only_ready": True,
        },
        "blocked_uploads": [],
        "errors": ["internal database error"],
    }

    projected = models.PublicUploadResponse.model_validate(raw).model_dump()
    encoded = json.dumps(projected)
    assert not any(field in encoded for field in FORBIDDEN_PUBLIC_FIELDS)
    assert projected["temp_documents"] == [{"file_name": "survey.xlsx"}]
    document = projected["commit"]["documents"][0]
    assert document["route"] == "sql"
    assert document["status"] == "committed_sql"
    assert document["sql_tables"][0]["logical_name"] == "data_abc"


def test_upload_projection_exposes_only_safe_block_message() -> None:
    raw = {
        "mode": "upload",
        "blocked_uploads": [{
            "file_name": "large-report.pdf",
            "route": "preprocess_failed",
            "message": (
                "문서가 커서 처리 시간이 초과되었습니다. "
                "파일을 나누거나 페이지 범위를 줄여 다시 시도해 주세요."
            ),
            "route_reason": (
                "문서가 커서 처리 시간이 초과되었습니다. "
                "파일을 나누거나 페이지 범위를 줄여 다시 시도해 주세요."
            ),
            "file_size_bytes": 123,
        }],
        "errors": ["httpx.ReadTimeout at /private/path"],
    }

    projected = models.PublicUploadResponse.model_validate(raw).model_dump()

    assert projected["blocked_uploads"] == [{
        "file_name": "large-report.pdf",
        "route": "preprocess_failed",
        "message": (
            "문서가 커서 처리 시간이 초과되었습니다. "
            "파일을 나누거나 페이지 범위를 줄여 다시 시도해 주세요."
        ),
    }]
    encoded = json.dumps(projected, ensure_ascii=False)
    assert "ReadTimeout" not in encoded
    assert "/private/path" not in encoded


def test_documents_projection_keeps_user_assets_and_hides_ledger_fields() -> None:
    raw = {
        "target_vdb_id": 139,
        "workflow_id": 301,
        "app_session_id": "session-a",
        "session_id": "session-a",
        "documents": [{
            "document_id": 42,
            "file_name": "survey.xlsx",
            "temp_document_id": 17,
            "source_doc_key": "private-key",
            "source_collection": "InternalSource",
            "uploaded_at": "2026-07-13T00:00:00Z",
            "expires_at": None,
            "file_size_bytes": 12345,
            "chunk_count": 22,
            "is_expired": False,
            "storage_route": "hybrid",
            "route_reason": "mixed workbook",
            "sql_tables": [{"logical_name": "data_abc", "sheet_name": "Raw", "row_count": 10, "column_count": 4}],
        }],
        "errors": ["internal error"],
    }

    projected = models.PublicDocumentsResponse.model_validate(raw).model_dump()
    encoded = json.dumps(projected)
    assert not any(field in encoded for field in FORBIDDEN_PUBLIC_FIELDS)
    document = projected["documents"][0]
    assert document["file_size_bytes"] == 12345
    assert document["storage_route"] == "hybrid"
    assert document["sql_tables"][0]["logical_name"] == "data_abc"


def test_search_projection_hides_ids_and_keeps_provenance() -> None:
    raw = {
        "target_vdb_id": 139,
        "workflow_id": 301,
        "app_session_id": "session-a",
        "session_id": "session-a",
        "question": "Germany values",
        "document_count": 1,
        "result_count": 1,
        "file_context": "Germany: 61/22/13/4",
        "file_sources": [
            {
                "document_id": 42,
                "file_name": "brief.pptx",
                "chunk_id": "object-1",
                "i_page": 7,
                "i_chunk_on_doc": 257,
                "distance": 0.31,
                "source_channel": "vlm_image_extraction",
                "visual_model": "gemini-3.1-flash-lite",
                "slide_number": 7,
                "section_title": "Market outlook",
            }
        ],
        "sql_available": True,
        "sql_sources": [{"document_id": 42, "file_name": "survey.xlsx", "logical_name": "data_abc", "sheet_name": "Raw", "row_count": 10, "column_count": 4}],
        "empty_page_sources": [],
        "errors": ["internal error"],
    }

    projected = models.PublicSearchResponse.model_validate(raw).model_dump()
    encoded = json.dumps(projected)
    assert not any(field in encoded for field in FORBIDDEN_PUBLIC_FIELDS)
    assert projected["file_context"] == "Germany: 61/22/13/4"
    assert projected["file_sources"][0]["source_channel"] == "vlm_image_extraction"
    assert projected["file_sources"][0]["slide_number"] == 7
    assert projected["file_sources"][0]["section_title"] == "Market outlook"
    assert projected["sql_sources"][0]["logical_name"] == "data_abc"


def test_openapi_public_responses_exclude_internal_fields_and_keep_capacity() -> None:
    spec = app.openapi()
    schemas = spec["components"]["schemas"]
    public_schema_names = {name for name in schemas if name.startswith("Public")}
    encoded = json.dumps({name: schemas[name] for name in public_schema_names})

    assert not any(f'"{field}"' in encoded for field in FORBIDDEN_PUBLIC_FIELDS)
    assert '"file_size_bytes"' in encoded
    assert '"logical_name"' in encoded
    assert '"route"' in encoded
    assert '"status"' in encoded
    assert "document_id" not in schemas["PublicFileSqlSource"]["properties"]
    assert "current_bytes" in schemas["QuotaSnapshot"]["properties"]


def test_openapi_public_route_descriptions_do_not_publish_internal_topology() -> None:
    spec = app.openapi()
    operations = {
        "/upload": "post",
        "/commit": "post",
        "/search": "post",
        "/documents": "get",
    }
    encoded = json.dumps(
        {path: spec["paths"][path][method] for path, method in operations.items()}
    ).lower()

    for forbidden in (
        "document_upsert",
        "file_path",
        "rollback",
        "source_collection",
        "target_collection",
        "temp_vdb_index",
        "weaviate",
    ):
        assert forbidden not in encoded
