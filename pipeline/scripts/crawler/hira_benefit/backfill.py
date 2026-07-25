from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date

from .change_detection import StoredNoticeState
from .models import NoticeListItem


@dataclass(frozen=True, slots=True)
class BackfillChunk:
    index: int
    items: tuple[NoticeListItem, ...]


@dataclass(frozen=True, slots=True)
class BackfillManifest:
    total_count: int
    chunk_size: int
    chunks: tuple[BackfillChunk, ...]
    manifest_sha256: str

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def to_json(self) -> str:
        payload = {
            "total_count": self.total_count,
            "chunk_size": self.chunk_size,
            "chunks": [
                {
                    "index": chunk.index,
                    "items": [
                        {
                            **asdict(item),
                            "notice_date": item.notice_date.isoformat(),
                        }
                        for item in chunk.items
                    ],
                }
                for chunk in self.chunks
            ],
            "manifest_sha256": self.manifest_sha256,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> BackfillManifest:
        payload = json.loads(value)
        chunks = tuple(
            BackfillChunk(
                index=int(chunk["index"]),
                items=tuple(
                    NoticeListItem(
                        source_notice_id=str(item["source_notice_id"]),
                        title=str(item["title"]),
                        notice_date=date.fromisoformat(str(item["notice_date"])),
                        source_url=str(item["source_url"]),
                        listing_fingerprint=str(item["listing_fingerprint"]),
                    )
                    for item in chunk["items"]
                ),
            )
            for chunk in payload["chunks"]
        )
        return cls(
            total_count=int(payload["total_count"]),
            chunk_size=int(payload["chunk_size"]),
            chunks=chunks,
            manifest_sha256=str(payload["manifest_sha256"]),
        )


@dataclass(frozen=True, slots=True)
class ManifestStateGate:
    expected_count: int
    matched_count: int
    missing_ids: tuple[str, ...]
    hash_mismatch_ids: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return (
            self.matched_count == self.expected_count
            and not self.missing_ids
            and not self.hash_mismatch_ids
        )


def build_backfill_manifest(
    items: tuple[NoticeListItem, ...],
    *,
    chunk_size: int,
) -> BackfillManifest:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    identity_rows = [
        (
            item.source_notice_id,
            item.listing_fingerprint,
            item.notice_date.isoformat(),
        )
        for item in items
    ]
    digest = hashlib.sha256(
        json.dumps(
            identity_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    chunks = tuple(
        BackfillChunk(index=index // chunk_size, items=items[index : index + chunk_size])
        for index in range(0, len(items), chunk_size)
    )
    return BackfillManifest(
        total_count=len(items),
        chunk_size=chunk_size,
        chunks=chunks,
        manifest_sha256=digest,
    )


def compare_manifest_state(
    manifest: BackfillManifest,
    stored: Mapping[str, StoredNoticeState],
) -> ManifestStateGate:
    """Require every manifest identity to exist with its exact listing hash."""

    missing_ids: list[str] = []
    hash_mismatch_ids: list[str] = []
    matched_count = 0
    for chunk in manifest.chunks:
        for item in chunk.items:
            current = stored.get(item.source_notice_id)
            if current is None:
                missing_ids.append(item.source_notice_id)
            elif current.listing_fingerprint != item.listing_fingerprint:
                hash_mismatch_ids.append(item.source_notice_id)
            else:
                matched_count += 1
    return ManifestStateGate(
        expected_count=manifest.total_count,
        matched_count=matched_count,
        missing_ids=tuple(missing_ids),
        hash_mismatch_ids=tuple(hash_mismatch_ids),
    )


def next_pending_chunk(
    manifest: BackfillManifest,
    *,
    completed_chunk_indexes: set[int],
) -> BackfillChunk | None:
    return next(
        (
            chunk
            for chunk in manifest.chunks
            if chunk.index not in completed_chunk_indexes
        ),
        None,
    )
