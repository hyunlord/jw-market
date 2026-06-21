from __future__ import annotations

from dataclasses import dataclass, field
import hashlib


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def text_sha256(value: str) -> str:
    """Return a stable SHA256 digest for sensitive message text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class MessageRecord:
    source: str
    market: str
    message_id: str
    period_ym: str
    product_name: str
    message_text: str
    frequency: int = 1

    @property
    def message_hash(self) -> str:
        """Return a digest suitable for audit joins without raw text dumps."""
        return text_sha256(self.message_text)


@dataclass(frozen=True, slots=True)
class TopicRule:
    market: str
    label: str
    keywords: tuple[str, ...]
    seed_source: str


@dataclass(frozen=True, slots=True)
class RuleAssignment:
    message_id: str
    market: str
    labels: tuple[str, ...]
    matched_keywords: tuple[str, ...]
    frequency: int


@dataclass(slots=True)
class MarketRuleStats:
    market: str
    total_rows: int = 0
    matched_rows: int = 0
    unmatched_rows: int = 0
    multilabel_rows: int = 0
    total_weight: int = 0
    matched_weight: int = 0
    unmatched_weight: int = 0

    def to_dict(self) -> dict[str, JsonValue]:
        """Serialize rates and counts for report tables."""
        row_rate = self.matched_rows / self.total_rows if self.total_rows else 0.0
        weight_rate = self.matched_weight / self.total_weight if self.total_weight else 0.0
        return {
            "market": self.market,
            "total_rows": self.total_rows,
            "matched_rows": self.matched_rows,
            "unmatched_rows": self.unmatched_rows,
            "multilabel_rows": self.multilabel_rows,
            "matched_row_rate": round(row_rate, 4),
            "matched_weight_rate": round(weight_rate, 4),
            "total_weight": self.total_weight,
            "matched_weight": self.matched_weight,
            "unmatched_weight": self.unmatched_weight,
        }


@dataclass(frozen=True, slots=True)
class RuleMatchResult:
    assignments: dict[str, RuleAssignment]
    market_stats: dict[str, MarketRuleStats]
    label_counts: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClusterSummary:
    market: str
    method: str
    cluster_id: str
    size: int
    weighted_size: int
    top_terms: tuple[str, ...]
    representative_ids: tuple[str, ...]
    representative_sentences: tuple[str, ...]
    suggested_label: str


@dataclass(frozen=True, slots=True)
class AlignmentRow:
    cluster_id: str
    top_rule_label: str
    weighted_label_share: float
    weighted_size: int
    label_weights: dict[str, int]
