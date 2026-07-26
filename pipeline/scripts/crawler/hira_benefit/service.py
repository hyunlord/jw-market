from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from html.parser import HTMLParser

from .change_detection import ChangePlan, StoredNoticeState, plan_changes
from .contract import HiraRunMetrics, HiraWorkflowInput
from .http_client import CircuitOpenError
from .models import NoticeListItem
from .parser import parse_detail_html, parse_list_html
from .repository import PersistableNotice
from .scope import match_brand_names


class _TagSequenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(f"<{tag.lower()}>")

    def handle_endtag(self, tag: str) -> None:
        self.tags.append(f"</{tag.lower()}>")


def tag_sequence_signature(html: str) -> str:
    parser = _TagSequenceParser()
    parser.feed(html)
    return hashlib.sha256("\x1f".join(parser.tags).encode("utf-8")).hexdigest()


def discover_changes(
    html: str,
    *,
    config: HiraWorkflowInput,
    stored: Mapping[str, StoredNoticeState] | None,
) -> tuple[ChangePlan, str]:
    rows = parse_list_html(html, base_url=config.base_url)
    if not rows:
        raise RuntimeError(
            "HIRA index yielded zero notices; verify URL/parameters before crawling"
        )
    return (
        plan_discovered_items(rows, config=config, stored=stored),
        tag_sequence_signature(html),
    )


def plan_discovered_items(
    rows: Sequence[NoticeListItem],
    *,
    config: HiraWorkflowInput,
    stored: Mapping[str, StoredNoticeState] | None,
) -> ChangePlan:
    return plan_changes(
        rows,
        stored=stored,
        first_run_mode=config.first_run_mode,
        notice_date_boundary=config.notice_date_boundary,
    )


def collect_details(
    items: Sequence[NoticeListItem],
    *,
    fetch_text: Callable[[str], str],
    brand_names: Sequence[str],
) -> tuple[tuple[PersistableNotice, ...], HiraRunMetrics]:
    collected: list[PersistableNotice] = []
    failures = 0
    for item in items:
        try:
            parsed = parse_detail_html(
                fetch_text(item.source_url),
                source_notice_id=item.source_notice_id,
                source_url=item.source_url,
            )
        except CircuitOpenError:
            raise
        except Exception:  # noqa: BLE001 - one bad notice must become an explicit run failure.
            failures += 1
            continue
        collected.append(
            PersistableNotice(
                parsed=parsed,
                listing_fingerprint=item.listing_fingerprint,
                brand_names=match_brand_names(parsed.raw_text, brand_names),
            )
        )
    expected_ids = {item.source_notice_id for item in items}
    actual_ids = {item.parsed.source_notice_id for item in collected}
    metrics = HiraRunMetrics(
        exit_code=0 if failures == 0 else 1,
        failures=failures,
        identity_gap=len(expected_ids.symmetric_difference(actual_ids)),
        pending_gap=len(expected_ids - actual_ids),
        parsed_count=len(collected),
        partial_count=sum(
            item.parsed.parse_status.value == "PARTIAL" for item in collected
        ),
        failed_count=sum(
            item.parsed.parse_status.value == "FAILED" for item in collected
        ),
    )
    return tuple(collected), metrics


def notice_to_json(item: PersistableNotice) -> dict[str, object]:
    payload = asdict(item.parsed)
    payload["notice_date"] = (
        item.parsed.notice_date.isoformat() if item.parsed.notice_date else None
    )
    payload["parse_status"] = item.parsed.parse_status.value
    payload["failed_fields"] = list(item.parsed.failed_fields)
    return {
        "parsed": payload,
        "listing_fingerprint": item.listing_fingerprint,
        "brand_names": list(item.brand_names),
    }
