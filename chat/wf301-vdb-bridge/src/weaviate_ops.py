"""Weaviate HTTP operations used by dry-run and commit."""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from . import settings
from .logging_utils import safe_log

Chunk = dict[str, Any]


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


def read_temp_chunks(client: httpx.Client, collection: str, temp_document_id: int) -> list[Chunk]:
    query = {
        "query": (
            "{ Get { %s(where:{path:[\"temp_doc_id\"],operator:Equal,"
            "valueNumber:%d}){ text temp_doc_id file_name file_path "
            "i_chunk_on_doc i_chunk_on_page i_page file_size _additional { id vector } } } }"
            % (collection, temp_document_id)
        )
    }
    response = client.post(
        f"{settings.WEAVIATE_BASE}/v1/graphql",
        json=query,
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    return (response.json().get("data", {}).get("Get", {}) or {}).get(collection) or []


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
            "summary": "",
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


def embed_text(client: httpx.Client, text: str) -> list[float]:
    payload = {"model": "model", "input": text}
    response = client.post(
        f"{settings.EMBEDDING_BASE.rstrip('/')}/v1/embeddings",
        json=payload,
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    embedding = body.get("data", [{}])[0].get("embedding")
    if not isinstance(embedding, list) or not embedding:
        raise RuntimeError("embedding serving returned no embedding")
    return [float(value) for value in embedding]


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
            "{ text doc_id file_name i_page i_chunk_on_doc _additional { id distance } } } }"
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
