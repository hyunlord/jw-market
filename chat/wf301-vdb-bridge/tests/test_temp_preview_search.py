from __future__ import annotations

from typing import Any

from src import weaviate_ops


class _Response:
    def __init__(self, body: Any, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self) -> Any:
        return self._body


class _SearchClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> _Response:
        self.posts.append(json)
        return _Response(
            {
                "data": {
                    "Get": {
                        "TempPreview": [
                            {
                                "text": "first pages",
                                "temp_doc_id": 1_812_345_678,
                                "file_name": "report.pdf",
                                "i_page": 3,
                                "i_chunk_on_doc": 2,
                                "_additional": {"id": "preview-1", "distance": 0.1},
                            }
                        ]
                    }
                }
            }
        )


def test_search_temp_preview_is_scoped_to_owned_preview_ids() -> None:
    client = _SearchClient()

    hits = weaviate_ops.search_temp_chunks(
        client,
        collection="TempPreview",
        vector=[0.1, 0.2],
        temp_document_ids=[1_812_345_678],
        limit=5,
    )

    assert hits[0]["file_name"] == "report.pdf"
    query = client.posts[0]["query"]
    assert "TempPreview" in query
    assert "1812345678" in query
    assert "nearVector" in query


def test_read_temp_preview_page_is_exact_and_scoped() -> None:
    client = _SearchClient()

    hits = weaviate_ops.read_temp_page_chunks(
        client,
        collection="TempPreview",
        temp_document_ids=[1_812_345_678],
        page_number=3,
        limit=100,
    )

    assert hits[0]["i_page"] == 3
    query = client.posts[0]["query"]
    assert "1812345678" in query
    assert 'path:["i_page"]' in query
    assert "valueNumber:3" in query
    assert "nearVector" not in query


class _DeleteClient:
    def __init__(self) -> None:
        self.lookup_count = 0
        self.deleted: list[str] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> _Response:
        self.lookup_count += 1
        rows = (
            [{"_additional": {"id": "preview-1"}}, {"_additional": {"id": "preview-2"}}]
            if self.lookup_count == 1
            else []
        )
        return _Response({"data": {"Get": {"TempPreview": rows}}})

    def delete(self, url: str, *, timeout: float) -> _Response:
        self.deleted.append(url.rsplit("/", 1)[-1])
        return _Response({})


def test_delete_temp_preview_removes_only_preview_collection_objects() -> None:
    client = _DeleteClient()

    deleted = weaviate_ops.delete_temp_objects_for_document(
        client,
        collection="TempPreview",
        temp_document_id=1_812_345_678,
        page_limit=100,
    )

    assert deleted == ["preview-1", "preview-2"]
    assert client.deleted == deleted
    assert client.lookup_count == 2
