from __future__ import annotations

import json
from pathlib import Path

from scripts.multiturn_matrix_gate import (
    DocumentIdentity,
    PortalClient,
    evaluate_turn,
    load_matrix,
    make_conversation_id,
    portal_tokens,
    run_matrix,
)


FIXTURE = Path(__file__).parent / "fixtures" / "multiturn_matrix.json"


def test_permanent_multiturn_matrix_has_all_nine_required_scenarios() -> None:
    matrix = load_matrix(FIXTURE)

    assert [item["id"] for item in matrix["scenarios"]] == [f"MT-{index:02d}" for index in range(1, 10)]
    assert matrix["repeats"] == 3
    assert all(2 <= len(item["turns"]) <= 3 for item in matrix["scenarios"])
    assert matrix["scenarios"][0]["turns"][1]["question"] == "2024년은?"
    assert matrix["scenarios"][0]["turns"][2]["question"] == "그 전 해는?"


def test_file_inheritance_scenario_matches_the_pinned_channel_fixture() -> None:
    matrix = load_matrix(FIXTURE)
    scenario = next(item for item in matrix["scenarios"] if item["id"] == "MT-06")

    assert scenario["turns"] == [
        {
            "question": "이 파일의 채널별 건수를 알려줘",
            "expected": {
                "slots": {"file_name": ["small_channel.xlsx"]},
                "answer_contains": ["192", "100", "92"],
                "disposition": "answered",
            },
        },
        {
            "question": "그중 1번 채널은 몇 건이야?",
            "expected": {
                "slots": {"file_name": ["small_channel.xlsx"]},
                "answer_contains": ["92"],
                "answer_excludes": ["파일을 업로드"],
                "disposition": "answered",
            },
        },
    ]


def test_matrix_turn_gate_checks_trace_slots_and_answer_contract() -> None:
    turn = {
        "question": "2024년은?",
        "expected": {
            "resolved_question_contains": ["리바로", "2024년", "매출"],
            "slots": {
                "anchor_brand": ["리바로"],
                "metric": ["매출", "sales"],
                "period": ["2024"],
            },
            "answer_contains": ["리바로", "2024"],
            "answer_excludes": ["알츠하이머", "IPO"],
            "disposition": "answered",
        },
    }
    capture = {
        "answer": "리바로 2024년 매출을 확인했습니다.",
        "trace": {
            "qa_trace": {
                "conversation": {
                    "resolved_question": "리바로 2024년 매출은?",
                    "resolved_slots": {
                        "anchor_brand": "리바로",
                        "metric": "sales",
                        "period": "2024",
                    },
                },
                "final": {"disposition": "answered", "body_empty": False},
            }
        },
    }

    assert evaluate_turn(turn, capture) == []


def test_matrix_fixture_is_json_serializable_without_runtime_secrets() -> None:
    matrix = load_matrix(FIXTURE)

    encoded = json.dumps(matrix, ensure_ascii=False)

    assert "token" not in encoded.casefold()
    assert "password" not in encoded.casefold()


def test_portal_tokens_are_loaded_in_memory_from_v2_login(monkeypatch) -> None:
    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"portalToken": "portal-secret", "accessToken": "access-secret"}

    seen: list[tuple[str, object, float]] = []

    def post(url: str, *, json: object, timeout: float):
        seen.append((url, json, timeout))
        return Response()

    monkeypatch.setattr("scripts.multiturn_matrix_gate.requests.post", post)

    assert portal_tokens("https://portal.example/test2-api/api/v1/auth/test-login", 17.0) == (
        "portal-secret",
        "access-secret",
    )
    assert seen == [("https://portal.example/test2-api/api/v1/auth/test-login", {}, 17.0)]


def test_upload_failure_is_recorded_and_session_is_still_cleaned(tmp_path: Path) -> None:
    class Client:
        def __init__(self) -> None:
            self.cleaned: list[str] = []

        def upload(self, _conversation_id: str, _fixture: Path) -> list[int]:
            raise RuntimeError("fixture upload failed")

        def cleanup(self, conversation_id: str, _document_ids: list[int]) -> list[str]:
            self.cleaned.append(conversation_id)
            return []

    client = Client()
    matrix = {
        "scenarios": [
            {
                "id": "MT-06",
                "name": "file_inheritance",
                "file_fixture": True,
                "turns": [{"question": "이 파일은?", "expected": {}}, {"question": "제조사별로는?", "expected": {}}],
            }
        ]
    }
    fixture = tmp_path / "fixture.xlsx"
    fixture.write_bytes(b"fixture")

    summary = run_matrix(
        matrix,
        client,  # type: ignore[arg-type]
        tmp_path / "out",
        repeats=1,
        mode="baseline",
        fixture=fixture,
        expected_commit="",
        expected_digest="",
    )

    assert len(summary["capture_failures"]) == 1
    assert "upload:RuntimeError:fixture upload failed" in summary["capture_failures"][0]
    assert len(client.cleaned) == 1


def test_conversation_ids_fit_the_portal_session_contract() -> None:
    identifiers = {
        make_conversation_id("baseline", "MT-09", repeat)
        for repeat in range(1, 4)
    }

    assert len(identifiers) == 3
    assert all(len(identifier) <= 36 for identifier in identifiers)
    assert all(identifier.startswith("mt-b-09-r") for identifier in identifiers)


def test_file_bridge_upload_uses_committed_document_identity(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "small_channel.xlsx"
    fixture.write_bytes(b"xlsx")
    calls: list[tuple[str, object]] = []

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "errors": [],
                "commit": {
                    "file_only_ready": True,
                    "errors": [],
                    "documents": [
                        {
                            "document_id": 114321,
                            "file_name": "small_channel.xlsx",
                            "chunk_count": 38,
                            "status": "committed",
                        }
                    ],
                },
            }

    def post(url: str, **kwargs: object) -> Response:
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("scripts.multiturn_matrix_gate.requests.post", post)
    client = PortalClient(
        stream_url="http://stream/stream",
        portal_base="http://portal/test2-api",
        file_bridge_base="http://bridge",
        timeout_s=10,
        portal_token="portal-secret",
        access_token="access-secret",
    )

    assert client.upload("mt-b-06-r1-abcdef", fixture) == [
        DocumentIdentity(field="document_id", value=114321)
    ]
    assert calls[0][0] == "http://bridge/upload"
    request = calls[0][1]
    assert request["data"] == {
        "workflow_id": "301",
        "app_session_id": "mt-b-06-r1-abcdef",
        "vdb_id": "139",
        "return_when": "complete",
    }
    assert "headers" not in request


def test_file_bridge_upload_recovers_public_temp_identity_from_sql_tables(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "small_channel.xlsx"
    fixture.write_bytes(b"xlsx")

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "errors": [],
                "commit": {
                    "file_only_ready": True,
                    "errors": [],
                    "documents": [
                        {
                            "file_name": "small_channel.xlsx",
                            "chunk_count": 38,
                            "route": "hybrid",
                            "status": "committed",
                            "sql_tables": [
                                {
                                    "logical_name": "doc_2768_raw_data",
                                    "sheet_name": "RAW DATA",
                                    "row_count": 194,
                                    "column_count": 267,
                                }
                            ],
                        }
                    ],
                },
            }

    monkeypatch.setattr(
        "scripts.multiturn_matrix_gate.requests.post",
        lambda *_args, **_kwargs: Response(),
    )
    client = PortalClient(
        stream_url="http://stream/stream",
        portal_base="http://portal/test2-api",
        file_bridge_base="http://bridge",
        timeout_s=10,
        portal_token="portal-secret",
        access_token="access-secret",
    )

    assert client.upload("mt-b-06-r1-abcdef", fixture) == [
        DocumentIdentity(field="temp_document_id", value=2768)
    ]
    assert client.upload_evidence("mt-b-06-r1-abcdef") == {
        "fixture": "small_channel.xlsx",
        "upload": {
            "keys": ["commit", "errors"],
            "error_count": 0,
            "temp_document_count": 0,
        },
        "commit": {
            "keys": ["documents", "errors", "file_only_ready"],
            "error_count": 0,
            "file_only_ready": True,
            "documents": [
                {
                    "file_name": "small_channel.xlsx",
                    "status": "committed",
                    "route": "hybrid",
                    "chunk_count": 38,
                    "logical_names": ["doc_2768_raw_data"],
                }
            ],
        },
        "identities": [{"field": "temp_document_id", "value": 2768}],
    }


def test_file_bridge_upload_rejects_ambiguous_public_temp_identity(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "small_channel.xlsx"
    fixture.write_bytes(b"xlsx")

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "errors": [],
                "commit": {
                    "file_only_ready": True,
                    "errors": [],
                    "documents": [
                        {
                            "file_name": "small_channel.xlsx",
                            "sql_tables": [
                                {"logical_name": "doc_2768_raw_data"},
                                {"logical_name": "doc_9999_other"},
                            ],
                        }
                    ],
                },
            }

    monkeypatch.setattr(
        "scripts.multiturn_matrix_gate.requests.post",
        lambda *_args, **_kwargs: Response(),
    )
    client = PortalClient(
        stream_url="http://stream/stream",
        portal_base="http://portal/test2-api",
        file_bridge_base="http://bridge",
        timeout_s=10,
        portal_token="portal-secret",
        access_token="access-secret",
    )

    try:
        client.upload("mt-b-06-r1-abcdef", fixture)
    except RuntimeError as exc:
        assert "ambiguous temp document identity" in str(exc)
    else:
        raise AssertionError("ambiguous public identity must fail closed")


def test_file_bridge_cleanup_discovers_residuals_and_verifies_zero(
    monkeypatch,
) -> None:
    posts: list[tuple[str, object]] = []
    document_reads = iter(
        [
            {
                "documents": [
                    {"document_id": 114321, "file_name": "small_channel.xlsx"},
                    {
                        "file_name": "orphan.xlsx",
                        "sql_tables": [{"logical_name": "doc_2768_raw_data"}],
                    },
                ]
            },
            {"documents": []},
        ]
    )

    class Response:
        def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

        def json(self) -> dict[str, object]:
            return self._payload

    def get(url: str, **kwargs: object) -> Response:
        assert url == "http://bridge/documents"
        assert kwargs["params"] == {
            "workflow_id": 301,
            "app_session_id": "mt-b-06-r1-abcdef",
            "vdb_id": 139,
        }
        return Response(next(document_reads))

    def post(url: str, **kwargs: object) -> Response:
        posts.append((url, kwargs))
        return Response({"status": "deleted", "errors": []})

    def put(url: str, **kwargs: object) -> Response:
        assert url == "http://portal/test2-api/api/v1/rnd/chat/session/delete"
        return Response({"status": "deleted"})

    monkeypatch.setattr("scripts.multiturn_matrix_gate.requests.get", get)
    monkeypatch.setattr("scripts.multiturn_matrix_gate.requests.post", post)
    monkeypatch.setattr("scripts.multiturn_matrix_gate.requests.put", put)
    client = PortalClient(
        stream_url="http://stream/stream",
        portal_base="http://portal/test2-api",
        file_bridge_base="http://bridge",
        timeout_s=10,
        portal_token="portal-secret",
        access_token="access-secret",
    )

    assert client.cleanup(
        "mt-b-06-r1-abcdef",
        [DocumentIdentity(field="document_id", value=114321)],
    ) == []
    assert [item[1]["json"] for item in posts] == [
        {
            "workflow_id": 301,
            "vdb_id": 139,
            "app_session_id": "mt-b-06-r1-abcdef",
            "document_id": 114321,
        },
        {
            "workflow_id": 301,
            "vdb_id": 139,
            "app_session_id": "mt-b-06-r1-abcdef",
            "temp_document_id": 2768,
        },
    ]
    assert all(item[0] == "http://bridge/documents/delete" for item in posts)
