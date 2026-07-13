"""Bounded suspect-page visual extraction for PDF documents."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import fitz
import httpx

from . import settings
from .logging_utils import safe_log

SOURCE_CHANNEL = "vlm_image_extraction"
DETECTOR_VERSION = "tier-a-v1"
PROMPT_VERSION = "visual-facts-v1"


class VisualConfigurationError(RuntimeError):
    """A non-retryable serving route or model configuration failure."""


@dataclass(frozen=True)
class PageDecision:
    page_number: int
    decision: str
    reason: str
    native_nonspace_chars: int
    image_count: int
    largest_image_coverage: float
    summed_image_coverage: float
    drawing_count: int


@dataclass(frozen=True)
class PdfScan:
    file_sha256: str
    page_count: int
    pages: list[PageDecision]


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, payload: dict[str, Any]) -> None:
        self.prompt_tokens += int(payload.get("prompt_tokens") or 0)
        self.completion_tokens += int(payload.get("completion_tokens") or 0)
        self.total_tokens += int(payload.get("total_tokens") or 0)


@dataclass
class VisualEnrichment:
    chunks: list[dict[str, Any]]
    status: str
    suspect_pages: list[int]
    selected_pages: list[int]
    failed_pages: list[int] = field(default_factory=list)
    skipped_cap_pages: list[int] = field(default_factory=list)
    # 네이티브 텍스트가 전혀 없는 페이지 번호. 외부 전처리기는 이런 페이지를 색인에서
    # 조용히 누락시키므로, 호출자가 시각 콘텐츠 처리 상태를 명시 고지하는 데 쓴다.
    no_native_text_pages: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    elapsed_s: float = 0.0


def _clipped_area(bbox: tuple[float, float, float, float], page_rect: fitz.Rect) -> float:
    rect = fitz.Rect(bbox) & page_rect
    return max(0.0, rect.width) * max(0.0, rect.height)


def _page_decision(page: fitz.Page, index: int, page_count: int) -> PageDecision:
    page_area = max(1.0, page.rect.width * page.rect.height)
    text = page.get_text("text") or ""
    nonspace = sum(not char.isspace() for char in text)
    image_info = page.get_image_info(xrefs=False)
    image_areas = [_clipped_area(tuple(item["bbox"]), page.rect) for item in image_info]
    largest = max(image_areas, default=0.0) / page_area
    summed = min(page_area, sum(image_areas)) / page_area
    drawings = len(page.get_drawings())
    is_empty = not nonspace and not image_info and not drawings
    tier_a = (
        bool(image_info)
        and largest >= settings.PDF_VLM_IMAGE_COVERAGE_MIN
        and nonspace < settings.PDF_VLM_NATIVE_CHAR_MAX
    )
    normalized_text = " ".join(text.lower().split())
    is_cover = index == 0 and any(
        marker in normalized_text
        for marker in ("cover", "disease analysis", "kol insights", "standards of care")
    )
    is_end_template = index == page_count - 1 and any(
        marker in normalized_text for marker in ("citeline powers", "listen now")
    )
    if is_empty:
        decision, reason = "native_only", "empty_page"
    elif tier_a and (is_cover or is_end_template):
        decision, reason = "native_only", "cover_suppressed"
    elif tier_a:
        decision, reason = "visual_required", "tier_a_mixed_large_raster"
    else:
        decision, reason = "native_only", "native_text"
    return PageDecision(
        page_number=index + 1,
        decision=decision,
        reason=reason,
        native_nonspace_chars=nonspace,
        image_count=len(image_info),
        largest_image_coverage=round(largest, 6),
        summed_image_coverage=round(summed, 6),
        drawing_count=drawings,
    )


def _scan_page_range(path: Path, start: int, stop: int, page_count: int) -> list[PageDecision]:
    with fitz.open(path) as document:
        return [_page_decision(document[index], index, page_count) for index in range(start, stop)]


def _page_ranges(page_count: int, worker_count: int) -> list[tuple[int, int]]:
    workers = min(max(worker_count, 1), page_count)
    width, remainder = divmod(page_count, workers)
    ranges: list[tuple[int, int]] = []
    start = 0
    for worker_index in range(workers):
        stop = start + width + (1 if worker_index < remainder else 0)
        ranges.append((start, stop))
        start = stop
    return ranges


def scan_pdf(path: Path) -> PdfScan:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    digest = hasher.hexdigest()
    with fitz.open(path) as document:
        page_count = len(document)
    ranges = _page_ranges(page_count, settings.PDF_VLM_SCAN_WORKERS) if page_count else []
    if len(ranges) <= 1:
        pages = _scan_page_range(path, 0, page_count, page_count)
    else:
        with ThreadPoolExecutor(max_workers=len(ranges)) as executor:
            futures = [
                executor.submit(_scan_page_range, path, start, stop, page_count)
                for start, stop in ranges
            ]
            pages = [page for future in futures for page in future.result()]
        pages.sort(key=lambda page: page.page_number)
    safe_log(
        "pdf_vlm_scan_done",
        file_sha256=digest,
        pages=page_count,
        scan_workers=len(ranges),
        detector_version=DETECTOR_VERSION,
        decisions=[
            {
                "page": page.page_number,
                "decision": page.decision,
                "reason": page.reason,
                "native_nonspace_chars": page.native_nonspace_chars,
                "image_count": page.image_count,
                "largest_image_coverage": page.largest_image_coverage,
                "summed_image_coverage": page.summed_image_coverage,
                "drawing_count": page.drawing_count,
            }
            for page in pages
            if page.decision != "native_only" or page.reason != "native_text"
        ],
    )
    return PdfScan(digest, page_count, pages)


def render_page_png(path: Path, page_number: int, *, dpi: int | None = None) -> bytes:
    render_dpi = dpi or settings.PDF_VLM_RENDER_DPI
    with fitz.open(path) as document:
        pixmap = document[page_number - 1].get_pixmap(dpi=render_dpi, alpha=False)
        payload = pixmap.tobytes("png")
    if len(payload) > settings.PDF_VLM_MAX_IMAGE_BYTES:
        raise ValueError(
            f"rendered page exceeds PDF_VLM_MAX_IMAGE_BYTES: {len(payload)} > "
            f"{settings.PDF_VLM_MAX_IMAGE_BYTES}"
        )
    return payload


def _request_payload(image_png: bytes) -> dict[str, Any]:
    prompt = (
        "Extract only facts visibly present in this page image. Return one JSON object with "
        "page_type, title, facts, uncertainty, extraction_complete. facts must be a list of "
        "objects with label, value, unit, relationship. Use an empty facts list when no visual "
        "fact is present. Never infer missing values."
    )
    image_url = "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")
    return {
        "model": settings.PDF_VLM_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
    }


def _parse_content(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("VLM response has no choices")
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        raise ValueError("VLM response content is not text")
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    payload = json.loads(stripped)
    if not isinstance(payload, dict) or not isinstance(payload.get("facts"), list):
        raise ValueError("VLM response does not match visual fact schema")
    for fact in payload["facts"]:
        if not isinstance(fact, dict) or "label" not in fact or "value" not in fact:
            raise ValueError("VLM fact is missing label/value")
    return payload


def _extract_page(client: httpx.Client, image_png: bytes, retries: int) -> tuple[dict[str, Any], dict[str, Any]]:
    url = f"{settings.PDF_VLM_BASE.rstrip('/')}/chat/completions"
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = client.post(
                url,
                json=_request_payload(image_png),
                timeout=settings.PDF_VLM_TIMEOUT_S,
            )
            response.raise_for_status()
            body = response.json()
            body_model = str(body.get("model") or "")
            if body_model and settings.PDF_VLM_MODEL_HEADER not in body_model:
                raise VisualConfigurationError(f"unexpected serving model: {body_model}")
            return _parse_content(body), body.get("usage") or {}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in {429} and status < 500:
                raise VisualConfigurationError(f"serving returned non-retryable HTTP {status}") from exc
            last_error = exc
            if attempt < retries:
                time.sleep(settings.PDF_VLM_RETRY_BACKOFF_S * (2**attempt))
        except (httpx.TimeoutException, httpx.TransportError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(settings.PDF_VLM_RETRY_BACKOFF_S * (2**attempt))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid VLM response: {exc}") from exc
    raise RuntimeError(f"VLM request failed after {retries + 1} attempt(s): {last_error}")


def _render_extract_text(page_number: int, payload: dict[str, Any]) -> str:
    lines: list[str] = []
    title = payload.get("title")
    if title:
        lines.append(f"Page {page_number}, {payload.get('page_type') or 'visual'} title: {title}.")
    for fact in payload["facts"]:
        label = str(fact.get("label") or "visible fact").strip()
        value = fact.get("value")
        unit = str(fact.get("unit") or "").strip()
        relationship = str(fact.get("relationship") or "").strip()
        suffix = f" {unit}" if unit else ""
        relation = f" ({relationship})" if relationship else ""
        lines.append(f"Page {page_number}, {label}: {value}{suffix}{relation}.")
    return "\n".join(lines)


def enrich_pdf_chunks(
    client: httpx.Client,
    *,
    path: Path,
    temp_document_id: int,
    file_name: str,
    native_chunks: list[dict[str, Any]],
    embed_texts: Callable[[list[str]], list[list[float]]],
    page_cap: int | None = None,
    retries: int | None = None,
) -> VisualEnrichment:
    started = time.monotonic()
    scan = scan_pdf(path)
    suspects = [page for page in scan.pages if page.decision == "visual_required"]
    cap = settings.PDF_VLM_MAX_PAGES_PER_DOCUMENT if page_cap is None else max(0, page_cap)
    selected = suspects[:cap]
    skipped = suspects[cap:]
    notes: list[str] = []
    if skipped:
        notes.append("visual_skipped_cap pages=" + ",".join(str(page.page_number) for page in skipped))
    visual_records: list[tuple[str, dict[str, Any]]] = []
    failed: list[int] = []
    usage = Usage()
    retry_count = settings.PDF_VLM_RETRIES if retries is None else max(0, retries)
    for selected_index, page in enumerate(selected):
        try:
            image = render_page_png(path, page.page_number)
            payload, page_usage = _extract_page(client, image, retry_count)
            usage.add(page_usage)
            text = _render_extract_text(page.page_number, payload)
            if text:
                metadata = {
                    "source_channel": SOURCE_CHANNEL,
                    "visual_model": settings.PDF_VLM_MODEL_HEADER,
                    "page_number": page.page_number,
                    "detector_version": DETECTOR_VERSION,
                    "detector_reason": page.reason,
                    "visual_prompt_version": PROMPT_VERSION,
                    "visual_status": "ok",
                    "file_sha256": scan.file_sha256,
                }
                visual_records.append((text, metadata))
        except VisualConfigurationError as exc:
            remaining = selected[selected_index:]
            failed.extend(item.page_number for item in remaining)
            notes.append(
                f"visual_failed circuit_breaker pages={[item.page_number for item in remaining]} "
                f"error={type(exc).__name__}: {exc}"
            )
            break
        except Exception as exc:
            failed.append(page.page_number)
            notes.append(f"visual_failed page={page.page_number} error={type(exc).__name__}: {exc}")
    chunks = [dict(chunk) for chunk in native_chunks]
    if visual_records:
        vectors = embed_texts([record[0] for record in visual_records])
        start_index = max((int(chunk.get("i_chunk_on_doc") or 0) for chunk in chunks), default=-1) + 1
        file_size = path.stat().st_size if path.is_file() else 0
        for offset, ((text, metadata), vector) in enumerate(zip(visual_records, vectors, strict=True)):
            page_number = int(metadata["page_number"])
            chunks.append(
                {
                    "text": text,
                    "summary": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    "source_channel": SOURCE_CHANNEL,
                    "temp_doc_id": temp_document_id,
                    "file_name": file_name,
                    "file_path": str(path),
                    "file_size": file_size,
                    "i_page": page_number,
                    "i_chunk_on_doc": start_index + offset,
                    "i_chunk_on_page": 0,
                    "_additional": {
                        "id": f"local-pdf-vlm-{temp_document_id}-{page_number}",
                        "vector": vector,
                    },
                }
            )
    status = "partial_visual" if failed or skipped else ("complete_visual" if suspects else "complete_text_only")
    elapsed = time.monotonic() - started
    safe_log(
        "pdf_vlm_enrichment_done",
        pages=scan.page_count,
        suspects=len(suspects),
        selected=len(selected),
        failed=len(failed),
        skipped_cap=len(skipped),
        visual_chunks=len(visual_records),
        total_tokens=usage.total_tokens,
        elapsed_s=round(elapsed, 3),
        status=status,
    )
    return VisualEnrichment(
        chunks=chunks,
        status=status,
        suspect_pages=[page.page_number for page in suspects],
        selected_pages=[page.page_number for page in selected],
        failed_pages=failed,
        skipped_cap_pages=[page.page_number for page in skipped],
        no_native_text_pages=[
            page.page_number for page in scan.pages if page.native_nonspace_chars == 0
        ],
        notes=notes,
        usage=usage,
        elapsed_s=elapsed,
    )
