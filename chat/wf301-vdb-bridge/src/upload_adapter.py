"""Upload adapter that preserves the GenOS temp-document contract."""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import pymysql
from fastapi import UploadFile

from . import settings

FILE_UPLOAD_PLUGIN_CODE = "WP01"
LOCAL_PREPROCESSOR_EXTENSIONS = frozenset({"docx"})
GATED_EXTERNAL_PREPROCESSOR_EXTENSIONS = frozenset({"pdf", "pptx"})
PDF_PAGE_MARKER = re.compile(rb"/Type\s*/Page\b")
PPTX_SLIDE_NAME = re.compile(r"^ppt/slides/slide\d+\.xml$")


@dataclass(frozen=True, slots=True)
class FileUploadConfig:
    serving_id: int
    preprocessor_id: int
    batch_size: int
    preprocessor_params: dict[str, Any]
    lifespan_days: int
    allowed_extensions: frozenset[str]


@dataclass(frozen=True, slots=True)
class TempVdbIndex:
    temp_vdb_index_id: int
    temp_vdb_index: str


@dataclass(frozen=True, slots=True)
class SavedTempDocument:
    temp_document_id: int
    file_name: str
    file_path: str


@dataclass(frozen=True, slots=True)
class PreprocessorGateBlock:
    file_name: str
    route: Literal["blocked_oversized"]
    route_reason: str
    file_size_bytes: int


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _int_param(params: dict[str, Any], key: str, fallback: int) -> int:
    raw = params.get(key)
    return int(raw) if raw is not None else fallback


def _find_temp_vdb_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return _find_temp_vdb_payload(parsed)
    if not isinstance(value, dict):
        return None
    if "temp_vdb_index_id" in value and "temp_vdb_index" in value:
        return value
    for key in ("data", "result", "body"):
        nested = value.get(key)
        found = _find_temp_vdb_payload(nested)
        if found is not None:
            return found
    return None


def load_file_upload_config(
    conn: pymysql.connections.Connection,
    *,
    workflow_id: int,
) -> FileUploadConfig:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT `values`
            FROM workflow_plugin_rel_tb
            WHERE workflow_id=%s
              AND plugin_code=%s
              AND is_active=1
            ORDER BY id DESC
            LIMIT 1
            """,
            (workflow_id, FILE_UPLOAD_PLUGIN_CODE),
        )
        plugin_row = cur.fetchone()
    if not plugin_row:
        raise RuntimeError("file upload plugin is not active for workflow")

    params = _as_dict(plugin_row.get("values"))
    preprocessor_id = _int_param(params, "preprocessor", settings.PREPROCESSOR_ID)

    allowed_extensions: frozenset[str] = frozenset()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT exts
            FROM document_preprocessor
            WHERE id=%s
              AND is_active=1
            LIMIT 1
            """,
            (preprocessor_id,),
        )
        preprocessor_row = cur.fetchone()
    if not preprocessor_row:
        raise RuntimeError("configured preprocessor is not active")
    exts = preprocessor_row.get("exts")
    if exts:
        allowed_extensions = frozenset(
            item.strip().lower() for item in str(exts).split(",") if item.strip()
        )

    return FileUploadConfig(
        serving_id=_int_param(params, "serving", settings.EMBEDDING_SERVING_ID),
        preprocessor_id=preprocessor_id,
        batch_size=_int_param(params, "batchSize", settings.BATCH_SIZE),
        preprocessor_params=_as_dict(params.get("preprocessorParams")),
        lifespan_days=_int_param(params, "lifespan", settings.TTL_DAYS),
        allowed_extensions=allowed_extensions,
    )


def upload_file_size(file: UploadFile) -> int:
    stream = file.file
    try:
        position = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(position)
    except OSError:
        size = int(file.size or 0)
    return int(size)


def _extension(file_name: str | None) -> str:
    return Path(file_name or "").suffix.lower().lstrip(".")


def _external_preprocessor_limit_bytes() -> int:
    return settings.EXTERNAL_PREPROCESSOR_MAX_FILE_MB * 1024 * 1024


def _size_gate_block(file_name: str, size_bytes: int) -> PreprocessorGateBlock | None:
    extension = _extension(file_name)
    if extension not in GATED_EXTERNAL_PREPROCESSOR_EXTENSIONS:
        return None
    limit_bytes = _external_preprocessor_limit_bytes()
    if size_bytes <= limit_bytes:
        return None
    reason = (
        f"file_size_bytes={size_bytes} exceeds PDF/PPTX preprocessor limit "
        f"{limit_bytes} bytes ({settings.EXTERNAL_PREPROCESSOR_MAX_FILE_MB} MiB); "
        "preprocessor-64 delegation is blocked to avoid shared-service OOM."
    )
    return PreprocessorGateBlock(
        file_name=file_name,
        route="blocked_oversized",
        route_reason=reason,
        file_size_bytes=size_bytes,
    )


def _pdf_page_count(path: Path) -> int | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return len(PDF_PAGE_MARKER.findall(data))


def _pptx_slide_count(path: Path) -> int | None:
    try:
        with zipfile.ZipFile(path) as archive:
            return sum(1 for name in archive.namelist() if PPTX_SLIDE_NAME.match(name))
    except (OSError, zipfile.BadZipFile):
        return None


def _metadata_gate_block(
    *,
    file_name: str,
    file_path: str,
    file_size_bytes: int,
) -> PreprocessorGateBlock | None:
    extension = _extension(file_name)
    path = Path(file_path)
    if extension == "pdf":
        page_count = _pdf_page_count(path)
        if page_count is not None and page_count > settings.EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES:
            reason = (
                f"page_count={page_count} exceeds PDF preprocessor limit "
                f"{settings.EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES}; "
                "preprocessor-64 delegation is blocked to avoid shared-service OOM."
            )
            return PreprocessorGateBlock(
                file_name=file_name,
                route="blocked_oversized",
                route_reason=reason,
                file_size_bytes=file_size_bytes,
            )
    if extension == "pptx":
        slide_count = _pptx_slide_count(path)
        if slide_count is not None and slide_count > settings.EXTERNAL_PREPROCESSOR_MAX_PPTX_SLIDES:
            reason = (
                f"slide_count={slide_count} exceeds PPTX preprocessor limit "
                f"{settings.EXTERNAL_PREPROCESSOR_MAX_PPTX_SLIDES}; "
                "preprocessor-64 delegation is blocked to avoid shared-service OOM."
            )
            return PreprocessorGateBlock(
                file_name=file_name,
                route="blocked_oversized",
                route_reason=reason,
                file_size_bytes=file_size_bytes,
            )
    return None


def blocked_external_preprocessor_uploads(files: list[UploadFile]) -> list[PreprocessorGateBlock]:
    blocks: list[PreprocessorGateBlock] = []
    for file in files:
        block = _size_gate_block(file.filename or "upload", upload_file_size(file))
        if block is not None:
            blocks.append(block)
    return blocks


def blocked_saved_external_preprocessor_documents(
    documents: list[SavedTempDocument],
) -> list[PreprocessorGateBlock]:
    blocks: list[PreprocessorGateBlock] = []
    for document in documents:
        try:
            size_bytes = Path(document.file_path).stat().st_size
        except OSError:
            continue
        size_block = _size_gate_block(document.file_name, size_bytes)
        if size_block is not None:
            blocks.append(size_block)
            continue
        metadata_block = _metadata_gate_block(
            file_name=document.file_name,
            file_path=document.file_path,
            file_size_bytes=size_bytes,
        )
        if metadata_block is not None:
            blocks.append(metadata_block)
    return blocks


def validate_extensions(files: list[UploadFile], allowed_extensions: frozenset[str]) -> list[str]:
    if not allowed_extensions:
        return []
    errors: list[str] = []
    for file in files:
        extension = _extension(file.filename)
        if extension in LOCAL_PREPROCESSOR_EXTENSIONS:
            continue
        if extension not in allowed_extensions:
            errors.append(f"허용되지 않는 파일 확장자입니다: {file.filename}")
    return errors


def requires_external_preprocessor(item: SavedTempDocument) -> bool:
    extension = _extension(item.file_name)
    return extension not in LOCAL_PREPROCESSOR_EXTENSIONS and extension != "xlsx"


def create_temp_vdb_index(
    client: httpx.Client,
    *,
    app_session_id: str,
    lifespan_days: int,
    user_id: int | None,
) -> TempVdbIndex:
    from datetime import datetime, timedelta

    headers = {"x-user-id": str(user_id)} if user_id is not None else {}
    response = client.post(
        f"{settings.TEMP_VDB_INDEX_API_BASE.rstrip('/')}/create",
        json={
            "app_type": "chat",
            "app_session_id": app_session_id,
            "expiry_date": (datetime.now() + timedelta(days=lifespan_days)).isoformat(),
        },
        headers=headers,
        timeout=settings.HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    payload = response.json()
    data = _find_temp_vdb_payload(payload)
    if not isinstance(data, dict):
        if isinstance(payload, dict):
            shape = {key: type(value).__name__ for key, value in payload.items()}
            code = payload.get("error_code") or payload.get("code")
            message = payload.get("errMsg") or payload.get("message")
        else:
            shape = {"payload": type(payload).__name__}
            code = None
            message = None
        raise RuntimeError(
            "temp-vdb-index-api response did not include data: "
            f"code={code!r} message={message!r} shape={shape}"
        )
    return TempVdbIndex(
        temp_vdb_index_id=int(data["temp_vdb_index_id"]),
        temp_vdb_index=str(data["temp_vdb_index"]),
    )


def insert_temp_documents(
    conn: pymysql.connections.Connection,
    *,
    temp_vdb_index_id: int,
    files: list[UploadFile],
) -> list[int]:
    document_ids: list[int] = []
    with conn.cursor() as cur:
        for file in files:
            cur.execute(
                """
                INSERT INTO temporary_document_tb
                    (temp_vdb_index_id, file_name, is_active, reg_date)
                VALUES
                    (%s, %s, 1, NOW())
                """,
                (temp_vdb_index_id, file.filename),
            )
            document_ids.append(int(cur.lastrowid))
    return document_ids


def save_temp_documents(
    files: list[UploadFile],
    *,
    temp_document_ids: list[int],
) -> list[SavedTempDocument]:
    Path(settings.TEMP_DOCUMENT_DIR).mkdir(parents=True, exist_ok=True)
    saved: list[SavedTempDocument] = []
    for file, temp_document_id in zip(files, temp_document_ids, strict=True):
        extension = Path(file.filename or "").suffix.lstrip(".")
        file_name = file.filename or f"upload-{temp_document_id}"
        temp_path = (
            Path(settings.TEMP_DOCUMENT_DIR) / f"TEMP_DOCUMENT_{temp_document_id}.{extension}"
        )
        file.file.seek(0)
        with temp_path.open("wb") as output:
            shutil.copyfileobj(file.file, output, length=8192)
        saved.append(
            SavedTempDocument(
                temp_document_id=temp_document_id,
                file_name=file_name,
                file_path=str(temp_path).replace("\\", "/"),
            )
        )
    return saved


def cleanup_saved_documents(saved_documents: list[SavedTempDocument]) -> None:
    for item in saved_documents:
        path = Path(item.file_path)
        if path.exists():
            path.unlink()


def deactivate_temp_documents(
    conn: pymysql.connections.Connection,
    *,
    temp_document_ids: list[int],
) -> None:
    if not temp_document_ids:
        return
    placeholders = ",".join(["%s"] * len(temp_document_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE temporary_document_tb
            SET is_active=0
            WHERE id IN ({placeholders})
            """,
            tuple(temp_document_ids),
        )


def run_preprocessor(
    client: httpx.Client,
    *,
    temp_vdb_index: str,
    config: FileUploadConfig,
    saved_documents: list[SavedTempDocument],
    user_id: int | None,
) -> dict[str, Any]:
    headers = {"x-user-id": str(user_id)} if user_id is not None else {}
    body = {
        "temp_vdb_index": temp_vdb_index,
        "serving_id": config.serving_id,
        "preprocessor_id": config.preprocessor_id,
        "batch_size": config.batch_size,
        "params": config.preprocessor_params,
        "files": [
            {"name": item.file_name, "path": item.file_path}
            for item in saved_documents
        ],
    }
    response = client.post(
        f"{settings.PREPROCESSOR_API_BASE.rstrip('/')}/temp/run",
        json=body,
        headers=headers,
        timeout=settings.PREPROCESSOR_TIMEOUT_S,
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {"raw": payload}
