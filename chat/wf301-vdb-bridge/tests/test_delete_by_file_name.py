from __future__ import annotations

import json
from unittest.mock import MagicMock

from src import delete_ops
from src.models import DeleteDocumentRequest


def _request(**overrides: object) -> DeleteDocumentRequest:
    payload: dict[str, object] = {
        "workflow_id": 301,
        "vdb_id": 139,
        "app_session_id": "session-a",
        "chat_id": "session-a",
    }
    payload.update(overrides)
    return DeleteDocumentRequest.model_validate(payload)


def test_delete_request_accepts_file_name_without_exposing_internal_ids() -> None:
    request = _request(file_name="report.xlsx")

    assert request.file_name == "report.xlsx"
    assert request.document_id is None
    assert request.temp_document_id is None


def test_find_delete_target_matches_file_name_inside_the_session() -> None:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = [
        {
            "document_id": 41,
            "file_name": "report.xlsx",
            "description": json.dumps(
                {
                    "workflow_id": 301,
                    "app_session_id": "session-b",
                    "temp_document_id": 501,
                }
            ),
            "is_active": 1,
        },
        {
            "document_id": 42,
            "file_name": "report.xlsx",
            "description": json.dumps(
                {
                    "workflow_id": 301,
                    "app_session_id": "session-a",
                    "temp_document_id": 502,
                }
            ),
            "is_active": 1,
        },
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor

    target = delete_ops.find_delete_target(
        connection,
        workflow_id=301,
        session_id="session-a",
        document_id=None,
        temp_document_id=None,
        file_name="report.xlsx",
    )

    assert target is not None
    assert target.document_id == 42
    assert target.temp_document_id == 502
    assert target.authorized is True


def test_find_delete_target_does_not_cross_session_for_same_file_name() -> None:
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchall.return_value = [
        {
            "document_id": 41,
            "file_name": "report.xlsx",
            "description": json.dumps(
                {
                    "workflow_id": 301,
                    "app_session_id": "session-b",
                    "temp_document_id": 501,
                }
            ),
            "is_active": 1,
        }
    ]
    connection = MagicMock()
    connection.cursor.return_value = cursor

    target = delete_ops.find_delete_target(
        connection,
        workflow_id=301,
        session_id="session-a",
        document_id=None,
        temp_document_id=None,
        file_name="report.xlsx",
    )

    assert target is None
