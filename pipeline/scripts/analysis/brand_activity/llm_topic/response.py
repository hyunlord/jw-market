from __future__ import annotations

from collections.abc import Sequence

from .models import BrandSharePayload, TopicShare, TopicShareItem


def normalized_share_payload(
    *,
    brand: str,
    atc4: str,
    axis_version: str,
    row_count: int,
    shares: Sequence[TopicShare],
    evidence_note: str,
) -> BrandSharePayload:
    items = [_share_item(share) for share in shares if share.share_pct > 0]
    total = round(sum(item["share_pct"] for item in items), 1)
    etc_pct = round(max(0.0, 100.0 - total), 1)
    if total > 100.0:
        scale = 100.0 / total
        items = [
            {
                "topic_id": item["topic_id"],
                "label": item["label"],
                "share_pct": round(item["share_pct"] * scale, 1),
                "row_count": item["row_count"],
            }
            for item in items
        ]
        etc_pct = round(max(0.0, 100.0 - sum(item["share_pct"] for item in items)), 1)
    return {
        "brand": brand,
        "atc4": atc4,
        "axis_version": axis_version,
        "denominator": "brand_row_count_primary_topic",
        "row_count": row_count,
        "topic_shares": items,
        "etc_pct": etc_pct,
        "evidence_note": evidence_note,
    }


def _share_item(share: TopicShare) -> TopicShareItem:
    return {
        "topic_id": share.topic_id,
        "label": share.label,
        "share_pct": round(share.share_pct, 1),
        "row_count": share.row_count,
    }
