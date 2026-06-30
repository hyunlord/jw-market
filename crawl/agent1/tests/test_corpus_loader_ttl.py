from __future__ import annotations

import sys
from pathlib import Path

AGENT1_DIR = Path(__file__).resolve().parents[1]
if str(AGENT1_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT1_DIR))

from corpus_loader_v2 import BuiltRecords, expire_at_for_tier


def test_expire_at_for_tier_when_tier1() -> None:
    # Given: a Tier1 collection timestamp.
    collected_at = "2026-07-01 12:34:56"

    # When: the loader computes retention metadata.
    expires_at = expire_at_for_tier(1, collected_at)

    # Then: Tier1 keeps the row for five years.
    assert expires_at == "2031-07-01 12:34:56"


def test_expire_at_for_tier_when_tier2() -> None:
    # Given: a Tier2 collection timestamp.
    collected_at = "2026-07-01 12:34:56"

    # When: the loader computes retention metadata.
    expires_at = expire_at_for_tier(2, collected_at)

    # Then: Tier2 keeps the row for one year.
    assert expires_at == "2027-07-01 12:34:56"


def test_expire_at_for_tier_when_leap_day() -> None:
    # Given: a leap-day collection timestamp.
    collected_at = "2024-02-29 00:00:00"

    # When: the loader computes retention metadata for a non-leap target year.
    expires_at = expire_at_for_tier(2, collected_at)

    # Then: the expiry clamps to the last valid February date.
    assert expires_at == "2025-02-28 00:00:00"


def test_built_records_when_tiered() -> None:
    # Given: tiered rows that mimic one built article record.
    collected_at = "2026-07-01 12:34:56"
    expire_at = expire_at_for_tier(2, collected_at)

    # When: records are assembled for loader insertion.
    records = BuiltRecords(
        news={"tier": 2, "collected_at": collected_at, "expire_at": expire_at},
        event={"tier": 2, "collected_at": collected_at, "expire_at": expire_at},
        scores=[{"tier": 2, "collected_at": collected_at, "expire_at": expire_at}],
        scored_path=Path("/tmp/article_scored.json"),
        source_path=Path("/tmp/article.json"),
    )

    # Then: every target table row carries the same explicit expiry timestamp.
    assert records.news["expire_at"] == "2027-07-01 12:34:56"
    assert records.event["expire_at"] == "2027-07-01 12:34:56"
    assert records.scores[0]["expire_at"] == "2027-07-01 12:34:56"
