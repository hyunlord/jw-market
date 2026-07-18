"""Weaviate HTTP operations used by dry-run and commit."""

from __future__ import annotations

import json
import os
import re
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from typing import Any

import httpx

from . import settings
from .logging_utils import safe_log

Chunk = dict[str, Any]

TEMP_CHUNK_BATCH_SIZE = 100
_COLLECTION_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class TempChunkCompletenessError(RuntimeError):
    """Raised before commit when temporary chunks cannot be recovered in full."""

    def __init__(self, *, temp_document_id: int, expected: int, recovered: int) -> None:
        self.temp_document_id = temp_document_id
        self.expected = expected
        self.recovered = recovered
        super().__init__(
            f"temp chunk completeness mismatch for {temp_document_id}: "
            f"{expected} expected, {recovered} recovered"
        )


def schema_classes(client: httpx.Client) -> list[dict[str, Any]]:
    response = client.get(f"{settings.WEAVIATE_BASE}/v1/schema", timeout=settings.HTTP_TIMEOUT_S)
    response.raise_for_status()
    return response.json().get("classes", []) or []


def candidate_temp_classes(classes: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    for cls in classes:
        props = {prop.get("name") for prop in cls.get("properties") or []}
        if {"temp_doc_id", "text", "file_name"}.issubset(props):
            candidates.append(cls.get("class"))
    return candidates


def resolve_temp_collection(
    client: httpx.Client, candidates: list[str], temp_document_id: int
) -> str | None:
    for cls in candidates:
        query = {
            "query": (
                "{ Get { %s(where:{path:[\"temp_doc_id\"],operator:Equal,"
                "valueNumber:%d}, limit:1){ temp_doc_id } } }"
                % (cls, temp_document_id)
            )
        }
        try:
            response = client.post(
                f"{settings.WEAVIATE_BASE}/v1/graphql",
                json=query,
                timeout=settings.HTTP_TIMEOUT_S,
            )
            response.raise_for_status()
            rows = (response.json().get("data", {}).get("Get", {}) or {}).get(cls) or []
            if rows:
                return cls
        except httpx.HTTPError as exc:
            safe_log("resolve_collection_error", cls=cls, err=str(exc))
    return None


def _temp_chunk_count(client: httpx.Client, collection: str, temp_document_id: int) -> int:
    query = {
        "query": (
            "{ Aggregate { %s(where:{path:[\"temp_doc_id\"],operator:Equal,"
            "valueNumber:%d}){ meta { count } } } }"
            % (collection, temp_document_id)
        )
    }
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/graphql",
        json=query,
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    rows = (response.json().get("data", {}).get("Aggregate", {}) or {}).get(collection) or []
    if not rows:
        return 0
    return int((rows[0].get("meta") or {}).get("count") or 0)


def count_temp_chunks(client: httpx.Client, collection: str, temp_document_id: int) -> int:
    return _temp_chunk_count(client, _validated_collection(collection), temp_document_id)


def _chunk_order(chunk: Chunk) -> tuple[int, int, int, str]:
    def _number(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 2**63 - 1

    return (
        _number(chunk.get("i_chunk_on_doc")),
        _number(chunk.get("i_page")),
        _number(chunk.get("i_chunk_on_page")),
        str((chunk.get("_additional") or {}).get("id") or ""),
    )


def read_temp_chunks(client: httpx.Client, collection: str, temp_document_id: int) -> list[Chunk]:
    expected = _temp_chunk_count(client, collection, temp_document_id)
    if expected == 0:
        return []

    chunks: list[Chunk] = []
    batches = 0
    while len(chunks) < expected:
        offset = len(chunks)
        query = {
            "query": (
                "{ Get { %s(where:{path:[\"temp_doc_id\"],operator:Equal,"
                "valueNumber:%d}, sort:[{path:[\"i_chunk_on_doc\"],order:asc}], "
                "limit:%d, offset:%d){ text temp_doc_id file_name file_path "
                "i_chunk_on_doc i_chunk_on_page i_page file_size "
                "_additional { id vector } } } }"
                % (collection, temp_document_id, TEMP_CHUNK_BATCH_SIZE, offset)
            )
        }
        response = client.post(
            f"{settings.WEAVIATE_BASE}/v1/graphql",
            json=query,
            timeout=settings.HTTP_TIMEOUT_S,
        )
        response.raise_for_status()
        batch = (response.json().get("data", {}).get("Get", {}) or {}).get(collection) or []
        batches += 1
        if not batch:
            break
        chunks.extend(batch)

    ids = [str((chunk.get("_additional") or {}).get("id") or "") for chunk in chunks]
    if len(chunks) != expected or not all(ids) or len(set(ids)) != len(ids):
        safe_log(
            "temp_chunks_incomplete",
            temp_document_id=temp_document_id,
            expected=expected,
            recovered=len(chunks),
            unique_ids=len(set(ids)),
            batches=batches,
        )
        raise TempChunkCompletenessError(
            temp_document_id=temp_document_id,
            expected=expected,
            recovered=len(chunks),
        )

    chunks.sort(key=_chunk_order)
    pages = {chunk.get("i_page") for chunk in chunks if chunk.get("i_page") is not None}
    safe_log(
        "temp_chunks_read_complete",
        temp_document_id=temp_document_id,
        expected=expected,
        recovered=len(chunks),
        pages=len(pages),
        batches=batches,
    )
    return chunks


def _validated_collection(collection: str) -> str:
    if not _COLLECTION_NAME.fullmatch(collection):
        raise ValueError("invalid Weaviate collection name")
    return collection


def _temp_doc_id_where(temp_document_ids: list[int]) -> str:
    if len(temp_document_ids) == 1:
        return '{path:["temp_doc_id"],operator:Equal,valueNumber:%d}' % temp_document_ids[0]
    operands = ",".join(
        '{path:["temp_doc_id"],operator:Equal,valueNumber:%d}' % temp_document_id
        for temp_document_id in temp_document_ids
    )
    return "{operator:Or,operands:[%s]}" % operands


def search_temp_chunks(
    client: httpx.Client,
    *,
    collection: str,
    vector: list[float],
    temp_document_ids: list[int],
    limit: int,
) -> list[dict[str, Any]]:
    if not temp_document_ids:
        return []
    collection = _validated_collection(collection)
    vector_literal = ",".join(str(value) for value in vector)
    where = _temp_doc_id_where(temp_document_ids)
    query = {
        "query": (
            "{ Get { %s(nearVector:{vector:[%s]}, where:%s, limit:%d)"
            "{ text temp_doc_id file_name i_page i_chunk_on_doc "
            "_additional { id distance } } } }"
            % (collection, vector_literal, where, limit)
        )
    }
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/graphql",
        json=query,
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"weaviate temp search errors: {body['errors'][:1]}")
    return (body.get("data", {}).get("Get", {}) or {}).get(collection) or []


def read_temp_page_chunks(
    client: httpx.Client,
    *,
    collection: str,
    temp_document_ids: list[int],
    page_number: int,
    limit: int,
) -> list[dict[str, Any]]:
    if not temp_document_ids:
        return []
    collection = _validated_collection(collection)
    where = (
        "{operator:And,operands:[%s,{path:[\"i_page\"],operator:Equal,valueNumber:%d}]}"
        % (_temp_doc_id_where(temp_document_ids), page_number)
    )
    query = {
        "query": (
            "{ Get { %s(where:%s, limit:%d, "
            "sort:[{path:[\"i_chunk_on_doc\"],order:asc}])"
            "{ text temp_doc_id file_name i_page i_chunk_on_doc "
            "_additional { id } } } }"
            % (collection, where, limit)
        )
    }
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/graphql",
        json=query,
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"weaviate temp page read errors: {body['errors'][:1]}")
    return (body.get("data", {}).get("Get", {}) or {}).get(collection) or []


def _list_temp_object_ids(
    client: httpx.Client,
    *,
    collection: str,
    temp_document_id: int,
    limit: int,
) -> list[str]:
    collection = _validated_collection(collection)
    query = {
        "query": (
            "{ Get { %s(where:{path:[\"temp_doc_id\"],operator:Equal,valueNumber:%d}, "
            "limit:%d){ _additional { id } } } }"
            % (collection, temp_document_id, limit)
        )
    }
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/graphql",
        json=query,
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"weaviate temp object lookup errors: {body['errors'][:1]}")
    rows = (body.get("data", {}).get("Get", {}) or {}).get(collection) or []
    return [
        str((row.get("_additional") or {}).get("id"))
        for row in rows
        if (row.get("_additional") or {}).get("id")
    ]


def delete_temp_objects_for_document(
    client: httpx.Client,
    *,
    collection: str,
    temp_document_id: int,
    page_limit: int = 100,
    max_rounds: int = 1000,
) -> list[str]:
    collection = _validated_collection(collection)
    deleted: list[str] = []
    for _ in range(max_rounds):
        object_ids = _list_temp_object_ids(
            client,
            collection=collection,
            temp_document_id=temp_document_id,
            limit=page_limit,
        )
        if not object_ids:
            return deleted
        for object_id in object_ids:
            response = client.delete(
                f"{settings.WEAVIATE_BASE}/v1/objects/{collection}/{object_id}",
                timeout=settings.HTTP_TIMEOUT_S,
            )
            if response.status_code != 404:
                response.raise_for_status()
            deleted.append(object_id)
    raise RuntimeError(
        f"weaviate temp delete exceeded max_rounds={max_rounds}"
    )


def first_vector_dim(chunks: list[Chunk]) -> int | None:
    if not chunks:
        return None
    vector = (chunks[0].get("_additional") or {}).get("vector")
    return len(vector) if isinstance(vector, list) else None


def max_file_size_bytes(chunks: list[Chunk]) -> int:
    sizes: list[int] = []
    for chunk in chunks:
        try:
            sizes.append(int(chunk.get("file_size") or 0))
        except (TypeError, ValueError):
            sizes.append(0)
    return max(sizes, default=0)


def build_object_id(idempotency_key: str, chunk_index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"wf301-vdb-bridge:{idempotency_key}:{chunk_index}"))


def copy_chunks_to_target(
    client: httpx.Client,
    chunks: list[Chunk],
    *,
    document_id: int,
    file_name: str,
    idempotency_key: str,
) -> list[str]:
    objects: list[dict[str, Any]] = []
    object_ids: list[str] = []
    n_chunks = len(chunks)
    for index, chunk in enumerate(chunks):
        additional = chunk.get("_additional") or {}
        vector = additional.get("vector")
        if not isinstance(vector, list) or not vector:
            raise ValueError(f"chunk {index} does not include a vector")
        object_id = build_object_id(idempotency_key, index)
        object_ids.append(object_id)
        text = str(chunk.get("text") or "")
        props = {
            "doc_id": document_id,
            "text": text,
            "summary": str(chunk.get("summary") or ""),
            "file_name": file_name,
            "file_path": chunk.get("file_path") or "",
            "file_size": int(chunk.get("file_size") or 0),
            "file_ext": file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "",
            "i_page": int(chunk.get("i_page") or 0),
            "i_chunk_on_doc": int(chunk.get("i_chunk_on_doc") or index),
            "i_chunk_on_page": int(chunk.get("i_chunk_on_page") or 0),
            "n_chunk_of_doc": n_chunks,
            "n_chunk_of_pages": n_chunks,
            "n_chars": len(text),
            "n_words": len(text.split()),
            "n_pages": max(int(chunk.get("i_page") or 1), 1),
            "is_encrypted": False,
        }
        objects.append(
            {
                "class": settings.TARGET_VDB_COLLECTION,
                "id": object_id,
                "properties": props,
                "vector": vector,
            }
        )
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/batch/objects",
        json={"objects": objects},
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if isinstance(body, list):
        errors = [row for row in body if row.get("result", {}).get("errors")]
        if errors:
            raise RuntimeError(f"weaviate batch errors: {errors[:1]}")
    return object_ids


def _post_target_objects(client: httpx.Client, objects: list[dict[str, Any]]) -> None:
    if not objects:
        return
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/batch/objects",
        json={"objects": objects},
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if isinstance(body, list):
        errors = [row for row in body if row.get("result", {}).get("errors")]
        if errors:
            raise RuntimeError(f"weaviate batch errors: {errors[:1]}")


def _embedding_batch_size() -> int:
    value = os.environ.get("EMBEDDING_BATCH_SIZE") or str(settings.BATCH_SIZE)
    try:
        requested = max(1, int(value))
    except ValueError:
        requested = max(1, settings.BATCH_SIZE)
    try:
        serving_limit = max(1, int(os.environ.get("EMBEDDING_MAX_BATCH_SIZE", "64")))
    except ValueError:
        serving_limit = 64
    return min(requested, serving_limit)


def _embedding_fallback_batch_size() -> int:
    value = os.environ.get("EMBEDDING_FALLBACK_BATCH_SIZE", "16")
    try:
        return max(1, int(value))
    except ValueError:
        return 16


def _embedding_batch_concurrency() -> int:
    value = os.environ.get("EMBEDDING_BATCH_CONCURRENCY", "1")
    try:
        return max(1, int(value))
    except ValueError:
        return 1


def _parse_embeddings(body: dict[str, Any], expected: int) -> list[list[float]]:
    rows = body.get("data")
    if not isinstance(rows, list) or len(rows) != expected:
        raise RuntimeError(
            f"embedding serving returned {len(rows) if isinstance(rows, list) else 'no'} "
            f"embeddings for {expected} inputs"
        )
    if all(isinstance(row, dict) and isinstance(row.get("index"), int) for row in rows):
        rows = sorted(rows, key=lambda row: row["index"])
    vectors: list[list[float]] = []
    for row in rows:
        embedding = row.get("embedding") if isinstance(row, dict) else None
        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError("embedding serving returned no embedding")
        vectors.append([float(value) for value in embedding])
    return vectors


def _embed_text_batch(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    payload: dict[str, Any] = {"model": "model", "input": texts if len(texts) > 1 else texts[0]}
    try:
        response = client.post(
            f"{settings.EMBEDDING_BASE.rstrip('/')}/v1/embeddings",
            json=payload,
            timeout=settings.EMBEDDING_TIMEOUT_S,
        )
    except httpx.ReadTimeout:
        fallback_size = _embedding_fallback_batch_size()
        if len(texts) <= fallback_size:
            raise
        safe_log(
            "embedding_batch_timeout_split",
            count=len(texts),
            fallback_batch_size=fallback_size,
        )
        vectors: list[list[float]] = []
        for start in range(0, len(texts), fallback_size):
            vectors.extend(_embed_text_batch(client, texts[start : start + fallback_size]))
        return vectors
    response.raise_for_status()
    try:
        return _parse_embeddings(response.json(), len(texts))
    except RuntimeError:
        # 임베딩 serving이 배치 일부만 반환하는 경우(요청당 처리 한도 초과 등):
        # 배치를 반으로 나눠 재시도한다. 1개까지 줄어도 실패하면 그대로 올려
        # 조용한 부분 임베딩 없이 fail-closed로 남긴다.
        if len(texts) <= 1:
            raise
        middle = len(texts) // 2
        safe_log("embedding_batch_incomplete_split", count=len(texts), retry_sizes=[middle, len(texts) - middle])
        return _embed_text_batch(client, texts[:middle]) + _embed_text_batch(client, texts[middle:])


def embed_texts(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    batch_size = _embedding_batch_size()
    batches = [
        (start, texts[start : start + batch_size])
        for start in range(0, len(texts), batch_size)
    ]
    concurrency = min(_embedding_batch_concurrency(), len(batches))
    vectors_by_batch: list[list[list[float]] | None] = [None] * len(batches)
    if concurrency == 1:
        for batch_index, (_, batch) in enumerate(batches):
            vectors_by_batch[batch_index] = _embed_text_batch(client, batch)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            future_to_index = {
                executor.submit(_embed_text_batch, client, batch): batch_index
                for batch_index, (_, batch) in enumerate(batches)
            }
            for future in as_completed(future_to_index):
                vectors_by_batch[future_to_index[future]] = future.result()
    vectors: list[list[float]] = []
    for batch_vectors in vectors_by_batch:
        if batch_vectors is None:
            raise RuntimeError("embedding batch did not complete")
        vectors.extend(batch_vectors)
    if len(vectors) != len(texts):
        raise RuntimeError(f"embedding count mismatch: {len(vectors)} != {len(texts)}")
    safe_log(
        "embedding_batches_done",
        chunks=len(texts),
        batches=len(batches),
        batch_size=batch_size,
        concurrency=concurrency,
    )
    return vectors


def embed_text(client: httpx.Client, text: str) -> list[float]:
    return embed_texts(client, [text])[0]


def _target_object(
    *,
    object_id: str,
    document_id: int,
    file_name: str,
    file_path: str,
    file_size: int,
    text: str,
    index: int,
    n_chunks: int,
    vector: list[float],
) -> dict[str, Any]:
    props = {
        "doc_id": document_id,
        "text": text,
        "summary": "",
        "file_name": file_name,
        "file_path": file_path,
        "file_size": file_size,
        "file_ext": file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "",
        "i_page": 1,
        "i_chunk_on_doc": index,
        "i_chunk_on_page": index,
        "n_chunk_of_doc": n_chunks,
        "n_chunk_of_pages": n_chunks,
        "n_chars": len(text),
        "n_words": len(text.split()),
        "n_pages": 1,
        "is_encrypted": False,
    }
    return {
        "class": settings.TARGET_VDB_COLLECTION,
        "id": object_id,
        "properties": props,
        "vector": vector,
    }


def copy_texts_to_target(
    client: httpx.Client,
    texts: list[str],
    *,
    document_id: int,
    file_name: str,
    file_path: str,
    file_size: int,
    idempotency_key: str,
) -> tuple[list[str], int | None]:
    if not texts:
        return [], None
    batch_size = _embedding_batch_size()
    batches = [
        (start, texts[start : start + batch_size])
        for start in range(0, len(texts), batch_size)
    ]
    concurrency = min(_embedding_batch_concurrency(), len(batches))
    object_ids: list[str | None] = [None] * len(texts)
    vector_dim: int | None = None

    def store_batch(start: int, batch: list[str], vectors: list[list[float]]) -> None:
        nonlocal vector_dim
        objects: list[dict[str, Any]] = []
        for offset, (text, vector) in enumerate(zip(batch, vectors)):
            index = start + offset
            if vector_dim is None:
                vector_dim = len(vector)
            object_id = build_object_id(idempotency_key, index)
            object_ids[index] = object_id
            objects.append(
                _target_object(
                    object_id=object_id,
                    document_id=document_id,
                    file_name=file_name,
                    file_path=file_path,
                    file_size=file_size,
                    text=text,
                    index=index,
                    n_chunks=len(texts),
                    vector=vector,
                )
            )
        _post_target_objects(client, objects)
        safe_log(
            "embedding_copy_batch_done",
            start=start,
            count=len(batch),
            total=len(texts),
            batch_size=batch_size,
            concurrency=concurrency,
        )

    if concurrency == 1:
        for start, batch in batches:
            store_batch(start, batch, _embed_text_batch(client, batch))
    else:
        batch_iter = iter(batches)
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            pending: dict[Any, tuple[int, list[str]]] = {}

            def submit_next() -> None:
                try:
                    start, batch = next(batch_iter)
                except StopIteration:
                    return
                pending[executor.submit(_embed_text_batch, client, batch)] = (start, batch)

            for _ in range(concurrency):
                submit_next()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    start, batch = pending.pop(future)
                    store_batch(start, batch, future.result())
                    submit_next()

    completed_ids = [object_id for object_id in object_ids if object_id is not None]
    if len(completed_ids) != len(texts):
        raise RuntimeError(f"weaviate object id count mismatch: {len(completed_ids)} != {len(texts)}")
    return completed_ids, vector_dim


def _doc_id_where(doc_ids: list[int]) -> str:
    if len(doc_ids) == 1:
        return '{path:["doc_id"],operator:Equal,valueNumber:%d}' % doc_ids[0]
    operands = ",".join(
        '{path:["doc_id"],operator:Equal,valueNumber:%d}' % doc_id for doc_id in doc_ids
    )
    return "{operator:Or,operands:[%s]}" % operands


def search_target_chunks(
    client: httpx.Client,
    *,
    vector: list[float],
    doc_ids: list[int],
    limit: int,
) -> list[dict[str, Any]]:
    if not doc_ids:
        return []
    vector_literal = ",".join(str(value) for value in vector)
    where = _doc_id_where(doc_ids)
    query = {
        "query": (
            "{ Get { %s(nearVector:{vector:[%s]}, where:%s, limit:%d)"
            "{ text summary doc_id file_name i_page i_chunk_on_doc _additional { id distance } } } }"
            % (settings.TARGET_VDB_COLLECTION, vector_literal, where, limit)
        )
    }
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/graphql",
        json=query,
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"weaviate search errors: {body['errors'][:1]}")
    return (body.get("data", {}).get("Get", {}) or {}).get(settings.TARGET_VDB_COLLECTION) or []


def _post_target_search(client: httpx.Client, query: str, error_label: str) -> list[dict[str, Any]]:
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/graphql",
        json={"query": query},
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"weaviate {error_label} errors: {body['errors'][:1]}")
    return (body.get("data", {}).get("Get", {}) or {}).get(settings.TARGET_VDB_COLLECTION) or []


def read_target_page_chunks(
    client: httpx.Client,
    *,
    doc_ids: list[int],
    page_number: int,
    limit: int,
) -> list[dict[str, Any]]:
    if not doc_ids:
        return []
    where = (
        "{operator:And,operands:[%s,{path:[\"i_page\"],operator:Equal,valueNumber:%d}]}"
        % (_doc_id_where(doc_ids), page_number)
    )
    query = (
        "{ Get { %s(where:%s, limit:%d, sort:[{path:[\"i_chunk_on_doc\"],order:asc}])"
        "{ text summary doc_id file_name i_page i_chunk_on_doc _additional { id } } } }"
        % (settings.TARGET_VDB_COLLECTION, where, limit)
    )
    return _post_target_search(client, query, "page read")


def search_target_keyword_chunks(
    client: httpx.Client,
    *,
    query: str,
    doc_ids: list[int],
    limit: int,
) -> list[dict[str, Any]]:
    if not doc_ids:
        return []
    query_literal = json.dumps(query, ensure_ascii=False)
    where = _doc_id_where(doc_ids)
    graphql = (
        "{ Get { %s(bm25:{query:%s,properties:[\"text\"]}, where:%s, limit:%d)"
        "{ text summary doc_id file_name i_page i_chunk_on_doc _additional { id score } } } }"
        % (settings.TARGET_VDB_COLLECTION, query_literal, where, limit)
    )
    return _post_target_search(client, graphql, "keyword search")


def _target_object_ids_query(document_id: int, *, limit: int) -> dict[str, str]:
    return {
        "query": (
            '{ Get { %s(where:{path:["doc_id"],operator:Equal,valueNumber:%d}, limit:%d)'
            "{ _additional { id } } } }"
            % (settings.TARGET_VDB_COLLECTION, document_id, limit)
        )
    }


def list_target_object_ids(
    client: httpx.Client,
    *,
    document_id: int,
    page_limit: int = 100,
) -> list[str]:
    query = _target_object_ids_query(document_id, limit=page_limit)
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/graphql",
        json=query,
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"weaviate object lookup errors: {body['errors'][:1]}")
    rows = (body.get("data", {}).get("Get", {}) or {}).get(settings.TARGET_VDB_COLLECTION) or []
    object_ids: list[str] = []
    for row in rows:
        additional = row.get("_additional") or {}
        object_id = additional.get("id")
        if isinstance(object_id, str) and object_id:
            object_ids.append(object_id)
    if rows and not object_ids:
        raise RuntimeError("weaviate object lookup returned rows without object ids")
    safe_log(
        "delete_object_lookup_page_done",
        document_id=document_id,
        object_count=len(object_ids),
        page_limit=page_limit,
    )
    return object_ids


def delete_target_objects(client: httpx.Client, *, object_ids: list[str]) -> list[str]:
    deleted: list[str] = []
    for object_id in object_ids:
        response = client.delete(
            f"{settings.WEAVIATE_BASE}/v1/objects/{settings.TARGET_VDB_COLLECTION}/{object_id}",
            timeout=settings.HTTP_TIMEOUT_S,
        )
        if response.status_code == 404:
            deleted.append(object_id)
            continue
        response.raise_for_status()
        deleted.append(object_id)
    return deleted


def delete_target_objects_for_document(
    client: httpx.Client,
    *,
    document_id: int,
    page_limit: int = 100,
    max_rounds: int = 1000,
) -> list[str]:
    deleted: list[str] = []
    for round_index in range(max_rounds):
        object_ids = list_target_object_ids(
            client,
            document_id=document_id,
            page_limit=page_limit,
        )
        if not object_ids:
            break
        deleted.extend(delete_target_objects(client, object_ids=object_ids))
        safe_log(
            "delete_object_page_done",
            document_id=document_id,
            round=round_index + 1,
            page_deleted=len(object_ids),
            deleted_total=len(deleted),
        )
        if len(object_ids) < page_limit:
            break
    else:
        raise RuntimeError(
            f"weaviate delete exceeded max_rounds={max_rounds} for document_id={document_id}"
        )
    return deleted



def read_target_chunks(client: httpx.Client, doc_ids: list[int], limit: int) -> list[dict[str, Any]]:
    """Read representative registered chunks for session wiki compilation."""
    if not doc_ids:
        return []
    where = _doc_id_where(doc_ids)
    query = {
        "query": (
            "{ Get { %s(where:%s, limit:%d, "
            "sort:[{path:[\"i_chunk_on_doc\"],order:asc}])"
            "{ text summary doc_id file_name i_page i_chunk_on_doc _additional { id } } } }"
            % (settings.TARGET_VDB_COLLECTION, where, limit)
        )
    }
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/graphql",
        json=query,
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"weaviate chunk read errors: {body['errors'][:1]}")
    rows = (body.get("data", {}).get("Get", {}) or {}).get(settings.TARGET_VDB_COLLECTION) or []
    chunks: list[dict[str, Any]] = []
    for row in rows:
        provenance: dict[str, Any] = {}
        summary = row.get("summary")
        if isinstance(summary, str) and summary:
            try:
                decoded = json.loads(summary)
                if isinstance(decoded, dict):
                    provenance = decoded
            except json.JSONDecodeError:
                pass
        chunks.append({
            "chunk_id": (row.get("_additional") or {}).get("id"),
            "doc_id": row.get("doc_id"),
            "file_name": row.get("file_name"),
            "text": row.get("text"),
            "i_page": row.get("i_page"),
            "i_chunk_on_doc": row.get("i_chunk_on_doc"),
            "source_channel": provenance.get("source_channel") or "native_text",
            "visual_model": provenance.get("visual_model"),
        })
    return chunks
