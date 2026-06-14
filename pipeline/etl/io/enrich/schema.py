from __future__ import annotations

from dataclasses import dataclass


ENRICHED_COLUMNS = [
    "ml_id",
    "product_id",
    "source",
    "period_yyyymm",
    "raw_rx_amt",
    "raw_rx_cnt",
    "raw_rx_qty",
    "canonical_value",
    "channel",
    "specialty",
    "match_method",
    "match_confidence",
    "source_table",
    "source_row_id",
    "ingested_at",
]


@dataclass
class EnrichResult:
    ml_id: str
    rows: int
    matched_products: int
    total_products: int
    sources: dict[str, int]
    skipped_sources: list[str]

    @property
    def product_match_rate(self) -> float:
        if self.total_products == 0:
            return 0.0
        return self.matched_products / self.total_products
