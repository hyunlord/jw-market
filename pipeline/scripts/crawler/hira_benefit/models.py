from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from enum import Enum


class ParseStatus(str, Enum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class NoticeListItem:
    source_notice_id: str
    title: str
    notice_date: date
    source_url: str
    listing_fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        source_notice_id: str,
        title: str,
        notice_date: date,
        source_url: str,
    ) -> NoticeListItem:
        normalized = "\x1f".join(
            (
                source_notice_id.strip(),
                " ".join(title.split()),
                notice_date.isoformat(),
                source_url.strip(),
            )
        )
        return cls(
            source_notice_id=source_notice_id.strip(),
            title=" ".join(title.split()),
            notice_date=notice_date,
            source_url=source_url.strip(),
            listing_fingerprint=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class ParsedNotice:
    source_notice_id: str
    source_url: str
    title: str | None
    notice_no: str | None
    notice_date: date | None
    target_condition: str | None
    exclusion_rule: str | None
    dosage_limit: str | None
    raw_text: str
    raw_html_sha256: str
    parse_status: ParseStatus
    failed_fields: tuple[str, ...]
