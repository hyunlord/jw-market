from __future__ import annotations

import json

from pydantic import TypeAdapter

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

    assert route_models["/upload"] == models.PublicAcceptedUploadResponse | models.PublicUploadResponse
    assert route_models["/upload/status"] is models.PublicUploadStatusResponse
    assert route_models["/commit"] is models.PublicCommitResponse
    assert route_models["/search"] is models.PublicSearchResponse
    assert route_models["/documents"] is models.PublicDocumentsResponse
    assert route_models["/file-sql/schema"] is models.FileSqlSchemaResponse
    assert route_models["/file-sql/query"] is models.FileSqlQueryResponse


def test_upload_async_mode_is_explicit_and_complete_remains_default() -> None:
    schema = app.openapi()
    body_ref = schema["paths"]["/upload"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]["$ref"]
    body_name = body_ref.rsplit("/", 1)[-1]
    return_when = schema["components"]["schemas"][body_name]["properties"]["return_when"]

    assert return_when["enum"] == ["complete", "accepted"]
    assert return_when["default"] == "complete"


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
                "query_ready": True,
                "indexed_pages": 20,
                "total_pages": 185,
                "card": {
                    "file_name": "wide.xlsx",
                    "file_type": "xlsx",
                    "size_bytes": 17103441,
                    "sheet_count": 1,
                    "sheets": [
                        {
                            "name": "Sell Out Standard",
                            "row_count": 12269,
                            "column_count": 252,
                        }
                    ],
                },
            }
        ],
        "message": None,
        "updated_at": "2026-07-18T00:00:00+00:00",
        "expires_at": "2026-07-19T00:00:00+00:00",
    }

    encoded = _encoded(models.PublicUploadStatusResponse, raw)

    assert not any(field in encoded for field in FORBIDDEN_PUBLIC_FIELDS)
    assert "wide.xlsx" in encoded
    assert "Sell Out Standard" in encoded
    assert "252" in encoded
    assert '"query_ready": true' in encoded
    assert '"indexed_pages": 20' in encoded
    assert '"total_pages": 185' in encoded


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
    union_value = TypeAdapter(
        models.PublicAcceptedUploadResponse | models.PublicUploadResponse
    ).validate_python(raw)
    assert not any(field in encoded for field in FORBIDDEN_PUBLIC_FIELDS)
    assert isinstance(union_value, models.PublicUploadResponse)
    assert not {"upload_id", "state", "ready", "status_url"} & projected.keys()
    assert projected["temp_documents"] == [{"file_name": "survey.xlsx"}]
    document = projected["commit"]["documents"][0]
    assert document["route"] == "sql"
    assert document["status"] == "committed_sql"
    assert document["sql_tables"][0]["logical_name"] == "data_abc"


def test_accepted_upload_projection_exposes_only_polling_contract() -> None:
    raw = {
        "mode": "upload",
        "target_vdb_id": 139,
        "workflow_id": 301,
        "app_session_id": "session-a",
        "session_id": "session-a",
        "temp_vdb_index_id": 1705,
        "temp_vdb_index": "InternalCollection",
        "temp_documents": [
            {
                "temp_document_id": 1601,
                "file_name": "wide.xlsx",
                "file_path": "/private/wide.xlsx",
            }
        ],
        "upload_id": "upl_7Qz4R4R2Xh9pCkN8",
        "state": "accepted",
        "ready": False,
        "message": "파일 확인 완료. 질문 준비를 진행하고 있습니다.",
        "status_url": "/upload/status",
        "file_cards": [
            {
                "file_name": "wide.xlsx",
                "file_type": "xlsx",
                "size_bytes": 17103441,
                "title": "CHSO Sell Out Basic",
                "sheet_count": 1,
                "sheets": [
                    {
                        "name": "Sell Out Standard",
                        "row_count": 12269,
                        "column_count": 252,
                    }
                ],
            }
        ],
    }

    projected = models.PublicAcceptedUploadResponse.model_validate(raw).model_dump()
    encoded = json.dumps(projected)
    union_value = TypeAdapter(
        models.PublicAcceptedUploadResponse | models.PublicUploadResponse
    ).validate_python(raw)

    assert isinstance(union_value, models.PublicAcceptedUploadResponse)
    assert projected["upload_id"] == "upl_7Qz4R4R2Xh9pCkN8"
    assert projected["state"] == "accepted"
    assert projected["ready"] is False
    assert projected["message"] == "파일 확인 완료. 질문 준비를 진행하고 있습니다."
    assert projected["status_url"] == "/upload/status"
    assert projected["file_cards"][0]["title"] == "CHSO Sell Out Basic"
    assert projected["file_cards"][0]["sheet_count"] == 1
    assert projected["file_cards"][0]["sheets"][0]["column_count"] == 252
    assert not any(field in encoded for field in FORBIDDEN_PUBLIC_FIELDS)


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
            "file_card": {
                "file_name": "survey.xlsx",
                "file_type": "xlsx",
                "size_bytes": 12345,
                "title": "Survey Workbook",
                "sheet_count": 1,
                "sheets": [{"name": "Raw", "row_count": 10, "column_count": 4}],
            },
            "sql_tables": [{"logical_name": "data_abc", "sheet_name": "Raw", "row_count": 10, "column_count": 4}],
        }],
        "errors": ["internal error"],
    }

    projected = models.PublicDocumentsResponse.model_validate(raw).model_dump()
    encoded = json.dumps(projected)
    # Contract change (PL): /documents now exposes the two identifiers that
    # /documents/delete requires, so the public contract pair is usable. Every
    # OTHER ledger/topology field (source_doc_key, source_collection, session ids,
    # workflow/vdb ids, errors, ...) MUST stay hidden — regression guard preserved.
    documents_forbidden = FORBIDDEN_PUBLIC_FIELDS - {"document_id", "temp_document_id"}
    assert not any(field in encoded for field in documents_forbidden)
    document = projected["documents"][0]
    assert document["document_id"] == 42
    assert document["temp_document_id"] == 17
    assert document["file_size_bytes"] == 12345
    assert document["storage_route"] == "hybrid"
    assert document["file_card"]["title"] == "Survey Workbook"
    assert document["file_card"]["sheets"][0]["column_count"] == 4
    assert document["sql_tables"][0]["logical_name"] == "data_abc"


def test_documents_contract_pair_exposes_delete_identifiers() -> None:
    """/documents now carries exactly the identifiers /documents/delete keys deletion on.

    Closes the contract-pair gap: /documents previously hid document_id and
    temp_document_id while /documents/delete required them, so the public response
    alone could not delete a document.
    """
    delete_identifiers = {"document_id", "temp_document_id"}
    assert delete_identifiers <= set(models.DeleteDocumentRequest.model_fields)
    assert delete_identifiers <= set(models.PublicSessionDocument.model_fields)

    document = models.PublicSessionDocument(
        document_id=42,
        temp_document_id=17,
        file_name="survey.xlsx",
        uploaded_at="2026-07-13T00:00:00Z",
    )
    assert document.document_id == 42
    assert document.temp_document_id == 17

    # A public document builds a valid delete request without any internal ledger lookup.
    delete_request = models.DeleteDocumentRequest.model_validate(
        {
            "app_session_id": "session-a",
            "workflow_id": 301,
            "vdb_id": 139,
            "document_id": document.document_id,
            "temp_document_id": document.temp_document_id,
        }
    )
    assert delete_request.document_id == 42
    assert delete_request.temp_document_id == 17


def test_documents_public_item_still_hides_topology_identifiers() -> None:
    """Only the two delete identifiers are opened; storage/session ledger stays hidden."""
    still_hidden = {"source_doc_key", "source_collection", "session_id", "target_vdb_id", "workflow_id"}
    assert still_hidden.isdisjoint(set(models.PublicSessionDocument.model_fields))


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

    # PublicSessionDocument intentionally exposes the two /documents/delete identifiers.
    # Every OTHER public schema stays fully ID-free — check them with the full set.
    other_public = public_schema_names - {"PublicSessionDocument"}
    encoded_other = json.dumps({name: schemas[name] for name in other_public})
    assert not any(f'"{field}"' in encoded_other for field in FORBIDDEN_PUBLIC_FIELDS)

    # The documents contract opens exactly the two identifiers and nothing else.
    session_doc_props = schemas["PublicSessionDocument"]["properties"]
    assert "document_id" in session_doc_props
    assert "temp_document_id" in session_doc_props
    assert not any(
        field in session_doc_props
        for field in (FORBIDDEN_PUBLIC_FIELDS - {"document_id", "temp_document_id"})
    )
    # Identifiers stay hidden on the search/file-sql provenance projections.
    assert "document_id" not in schemas["PublicFileSqlSource"]["properties"]
    assert "document_id" not in schemas["PublicFileSource"]["properties"]

    encoded = json.dumps({name: schemas[name] for name in public_schema_names})
    assert '"file_size_bytes"' in encoded
    assert '"logical_name"' in encoded
    assert '"route"' in encoded
    assert '"status"' in encoded
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
