"""Session wiki orchestration for wf301 file search."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

import httpx

from . import ledger, settings, weaviate_ops
from .models import FileSource

LOGGER = logging.getLogger(__name__)
_READY_STATUS: Final = "ready"
_ACTIVE_COMPILES: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class WikiPage:
    page_type: str
    title: str
    md: str
    citations: tuple[dict[str, Any], ...]
    cost_krw: float = 0.0


def scope_key(workflow_id: int, session_id: str) -> str:
    return f"workflow:{workflow_id}:session:{session_id}"


def ensure_schema(conn: Any) -> None:
    if not settings.WIKI_AUTO_CREATE_SCHEMA:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS session_wiki_page (
                id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                scope_key VARCHAR(512) NOT NULL,
                workflow_id INT NOT NULL,
                session_id VARCHAR(255) NOT NULL,
                page_type VARCHAR(64) NOT NULL,
                title VARCHAR(512) NOT NULL,
                md MEDIUMTEXT NOT NULL,
                citations JSON NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'ready',
                source_fingerprint CHAR(64) NOT NULL,
                compile_model VARCHAR(64) NOT NULL DEFAULT 'serving-190',
                cost_krw DECIMAL(10,4) NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                expires_at DATETIME NOT NULL,
                UNIQUE KEY uq_session_wiki_scope_page (scope_key, page_type),
                KEY idx_session_wiki_expiry (expires_at),
                KEY idx_session_wiki_workflow_session (workflow_id, session_id),
                CONSTRAINT chk_session_wiki_status CHECK (status IN ('ready', 'skipped', 'failed', 'expired'))
            )
            """
        )
    conn.commit()


def read_ready_pages(conn: Any, workflow_id: int, session_id: str) -> list[WikiPage]:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT page_type, title, md, citations, cost_krw
            FROM session_wiki_page
            WHERE workflow_id=%s AND session_id=%s AND status=%s AND expires_at > NOW()
            ORDER BY updated_at DESC
            """,
            (workflow_id, session_id, _READY_STATUS),
        )
        rows = cur.fetchall()
    return [page for row in rows if (page := _row_to_page(row)).md.strip()]


def context_from_pages(pages: list[WikiPage], char_limit: int) -> tuple[str, list[FileSource]]:
    blocks: list[str] = []
    sources: list[FileSource] = []
    remaining = char_limit
    for page in pages:
        block = f"[Session Wiki: {page.title}]\n{page.md.strip()}"
        if len(block) > remaining:
            block = block[:remaining].rstrip()
        if block:
            blocks.append(block)
            remaining -= len(block)
        sources.extend(_citation_sources(page))
        if remaining <= 0:
            break
    return "\n\n".join(blocks), sources


def should_trigger(documents: list[dict[str, Any]]) -> bool:
    if not settings.WIKI_ENABLED or len(documents) < settings.WIKI_MIN_DOCUMENTS:
        return False
    narrative_exts = (".pdf", ".txt", ".md", ".docx")
    return any(str(doc.get("file_name") or "").lower().endswith(narrative_exts) for doc in documents)


def trigger_compile_async(workflow_id: int, session_id: str) -> bool:
    key = scope_key(workflow_id, session_id)
    with _ACTIVE_LOCK:
        if key in _ACTIVE_COMPILES:
            return False
        _ACTIVE_COMPILES.add(key)
    threading.Thread(
        target=_compile_background,
        args=(workflow_id, session_id, key),
        name=f"session-wiki-{hashlib.sha1(key.encode()).hexdigest()[:8]}",
        daemon=True,
    ).start()
    return True


def compile_scope(workflow_id: int, session_id: str) -> None:
    key = scope_key(workflow_id, session_id)
    with ledger.ledger_connection() as conn:
        ensure_schema(conn)
        if not _acquire_lock(conn, key):
            return
        try:
            documents = ledger.list_session_documents(conn, workflow_id=workflow_id, session_id=session_id)
            if not should_trigger(documents) or read_ready_pages(conn, workflow_id, session_id):
                return
            doc_ids = [int(doc["document_id"]) for doc in documents if doc.get("document_id")]
            chunks = _load_chunks(doc_ids)
            pages = _compile_pages(documents, chunks) if chunks else []
            if pages:
                _upsert_pages(conn, workflow_id, session_id, documents, pages)
        finally:
            _release_lock(conn, key)


def source_fingerprint(documents: list[dict[str, Any]]) -> str:
    material = "|".join(
        f"{doc.get('document_id')}:{doc.get('file_name')}:{doc.get('chunk_count')}:{doc.get('uploaded_at')}"
        for doc in sorted(documents, key=lambda item: int(item.get("document_id") or 0))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_to_page(row: dict[str, Any]) -> WikiPage:
    raw = row.get("citations") or "[]"
    try:
        citations = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        citations = []
    return WikiPage(
        page_type=str(row.get("page_type") or "overview"),
        title=str(row.get("title") or "Session Wiki"),
        md=str(row.get("md") or ""),
        citations=tuple(citations if isinstance(citations, list) else []),
        cost_krw=float(row.get("cost_krw") or 0),
    )


def _citation_sources(page: WikiPage) -> list[FileSource]:
    rows: list[FileSource] = []
    for citation in page.citations:
        alias = str(citation.get("alias") or page.page_type)
        rows.append(
            FileSource(
                document_id=int(citation.get("document_id") or 0),
                file_name=str(citation.get("file_name") or page.title),
                chunk_id=f"wiki:{alias}",
                i_page=citation.get("i_page"),
                i_chunk_on_doc=citation.get("i_chunk_on_doc"),
            )
        )
    return rows


def _compile_background(workflow_id: int, session_id: str, key: str) -> None:
    try:
        compile_scope(workflow_id, session_id)
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        LOGGER.warning("session wiki compile failed for %s: %s", key, exc)
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_COMPILES.discard(key)


def _acquire_lock(conn: Any, key: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, %s) AS acquired", (f"session_wiki:{key}", settings.WIKI_LOCK_TIMEOUT_S))
        row = cur.fetchone() or {}
    return int(row.get("acquired") or 0) == 1


def _release_lock(conn: Any, key: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT RELEASE_LOCK(%s)", (f"session_wiki:{key}",))


def _load_chunks(document_ids: list[int]) -> list[dict[str, Any]]:
    with httpx.Client() as client:
        return weaviate_ops.read_target_chunks(client, document_ids, settings.WIKI_MAX_CHUNKS)


def _compile_pages(documents: list[dict[str, Any]], chunks: list[dict[str, Any]]) -> list[WikiPage]:
    aliases = _alias_chunks(chunks)
    prompt = _compile_prompt(documents, aliases)
    payload = {
        "model": settings.WIKI_SERVING_MODEL,
        "messages": [
            {"role": "system", "content": "Compile a Korean session wiki using only cited aliases."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "stream": False,
    }
    with httpx.Client(timeout=settings.WIKI_COMPILE_TIMEOUT_S) as client:
        response = client.post(f"{settings.WIKI_SERVING_BASE}/chat/completions", json=payload)
        response.raise_for_status()
    return _parse_pages(_message_content(response.json()), aliases)


def _alias_chunks(chunks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    aliases: dict[str, dict[str, Any]] = {}
    for index, chunk in enumerate(chunks, start=1):
        text = str(chunk.get("text") or "").strip()
        if text:
            alias = f"C{index}"
            aliases[alias] = {**chunk, "alias": alias, "text": text[:1800]}
    return aliases


def _compile_prompt(documents: list[dict[str, Any]], aliases: dict[str, dict[str, Any]]) -> str:
    docs = "\n".join(f"- {doc.get('document_id')}: {doc.get('file_name')}" for doc in documents)
    excerpts = "\n\n".join(f"[{alias}] {row['text']}" for alias, row in aliases.items())
    return (
        "Return strict JSON only: {\"pages\":[{\"page_type\":\"overview\",\"title\":\"...\","
        "\"md\":\"...\",\"citations\":[\"C1\"]}]}\n"
        "Use citation aliases exactly. Do not expose UUIDs or invent citations.\n\n"
        f"Documents:\n{docs}\n\nExcerpts:\n{excerpts}"
    )


def _message_content(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    message = (choices[0] if choices else {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or part) for part in content)
    return ""


def _parse_pages(content: str, aliases: dict[str, dict[str, Any]]) -> list[WikiPage]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    parsed = json.loads(text)
    pages = parsed.get("pages") if isinstance(parsed, dict) else parsed
    if not isinstance(pages, list):
        return []
    return [_page_from_model(row, aliases) for row in pages if isinstance(row, dict) and row.get("md")]


def _page_from_model(row: dict[str, Any], aliases: dict[str, dict[str, Any]]) -> WikiPage:
    citations = []
    for raw_alias in row.get("citations") or []:
        alias = str(raw_alias.get("alias") if isinstance(raw_alias, dict) else raw_alias)
        if alias in aliases:
            hit = aliases[alias]
            citations.append({
                "alias": alias,
                "document_id": hit.get("doc_id"),
                "file_name": hit.get("file_name"),
                "i_page": hit.get("i_page"),
                "i_chunk_on_doc": hit.get("i_chunk_on_doc"),
            })
    return WikiPage(str(row.get("page_type") or "overview")[:64], str(row.get("title") or "Session Wiki")[:512], str(row["md"]), tuple(citations))


def _upsert_pages(conn: Any, workflow_id: int, session_id: str, documents: list[dict[str, Any]], pages: list[WikiPage]) -> None:
    key = scope_key(workflow_id, session_id)
    expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=settings.TTL_DAYS)
    with conn.cursor() as cur:
        for page in pages:
            cur.execute(
                """
                INSERT INTO session_wiki_page
                    (scope_key, workflow_id, session_id, page_type, title, md, citations, status,
                     source_fingerprint, compile_model, cost_krw, expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE title=VALUES(title), md=VALUES(md), citations=VALUES(citations),
                    status=VALUES(status), source_fingerprint=VALUES(source_fingerprint),
                    compile_model=VALUES(compile_model), cost_krw=VALUES(cost_krw), expires_at=VALUES(expires_at)
                """,
                (key, workflow_id, session_id, page.page_type, page.title, page.md, json.dumps(list(page.citations), ensure_ascii=False), _READY_STATUS, source_fingerprint(documents), settings.WIKI_SERVING_MODEL, page.cost_krw, expires),
            )
    conn.commit()
