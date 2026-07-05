"""Raw staging keys and window helpers for brand-activity source rows."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
import hashlib
from typing import Final
from typing import Literal

from pipeline.scripts.etl.brand_activity.csd_core import CsdRow
from pipeline.scripts.etl.brand_activity.km_core import KeywordEvent


CSD_METRIC: Final[str] = "product_details"
StageDataset = Literal["csd", "keyword"]
StageScope = Literal["all", "csd", "keyword"]
STAGE_SCOPE_CHOICES: Final[tuple[StageScope, ...]] = ("all", "csd", "keyword")


def datasets_for_stage_scope(scope: StageScope) -> tuple[StageDataset, ...]:
    """Return the source/stage datasets that a rebuild scope may touch."""
    match scope:
        case "all":
            return ("csd", "keyword")
        case "csd":
            return ("csd",)
        case "keyword":
            return ("keyword",)
        case _:
            raise ValueError(f"unsupported stage scope: {scope!r}")


def _hash_key(parts: tuple[str, ...]) -> str:
    """Hash a typed natural key into a fixed-width DB key."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def csd_dedup_key(row: CsdRow) -> str:
    """Return the CSD datapoint key, excluding source-file provenance."""
    return _hash_key(
        (
            "csd",
            row.period_ym,
            row.market,
            row.jw_channel,
            row.master_product,
            row.representing_company,
            CSD_METRIC,
        )
    )


def keyword_dedup_key(event: KeywordEvent) -> str:
    """Return the Keyword event key that preserves duplicate source rows."""
    return _hash_key(("keyword", event.source_file, str(event.source_row_no)))


def recent_month_window(max_period_ym: str, months: int = 36) -> tuple[str, str]:
    """Return the inclusive month window ending at `max_period_ym`."""
    end = _parse_period_month(max_period_ym)
    start_index = end.year * 12 + end.month - (months - 1)
    start_year = (start_index - 1) // 12
    start_month = (start_index - 1) % 12 + 1
    return f"{start_year:04d}-{start_month:02d}", max_period_ym


def _parse_period_month(period_ym: str) -> date:
    """Parse `YYYY-MM` into a date at the final day of that month."""
    year_text, month_text = period_ym.split("-", 1)
    year = int(year_text)
    month = int(month_text)
    return date(year, month, monthrange(year, month)[1])
