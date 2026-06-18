"""Typed contracts for market-scope option resolution.

The HTTP endpoints are intentionally out of scope for Stage 0-2.  These
dataclasses mirror the future wire contract while staying easy to exercise in
unit tests and local scripts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum, unique
from typing import Any, Final, Literal


CONTRACT_VERSION: Final[str] = "market-scope-v1"
ALGORITHM_VERSION: Final[str] = "strategy-union-recalc-v1"
DEDUP_KEY_VERSION: Final[str] = "raw_fact_identity_v1"
MemberStatus = Literal["present", "absent_in_csd"]


class MarketScopeValidationError(Exception):
    """Raised when a market-scope request violates the Stage 1 contract."""

    def __init__(self, message: str) -> None:
        """Store a stable validation message for API/log boundaries."""

        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        """Return the stored validation message."""

        return self.message


@unique
class OptionType(StrEnum):
    """Closed option kinds from the market-scope contract."""

    SOURCE_MARKET = "source_market"
    GROUP_UNION = "group_union"
    GENERAL_ATC4 = "general_atc4"
    GENERAL_MOLECULE = "general_molecule"


@unique
class ViewFamily(StrEnum):
    """Supported market-scope view families."""

    STRATEGY = "strategy"
    GENERAL = "general"


@dataclass(frozen=True, slots=True)
class MarketScopeMember:
    """One member row in a source-market or group-union option."""

    brand_name: str
    source_market: str | None
    atc4_set: tuple[str, ...]
    member_status: MemberStatus
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-ready member metadata."""

        payload = asdict(self)
        payload["atc4_set"] = list(self.atc4_set)
        return payload


@dataclass(frozen=True, slots=True)
class MarketScopeOption:
    """Selectable market-scope option exposed to the caller."""

    option_id: str
    label: str
    option_type: OptionType
    view_family: ViewFamily
    source_markets: tuple[str, ...]
    atc4_set: tuple[str, ...]
    members: tuple[MarketScopeMember, ...]
    member_status: MemberStatus
    available_sources: tuple[str, ...]
    catalog_version: str

    def to_dict(self) -> dict[str, Any]:
        """Return a contract-shaped option dictionary."""

        return {
            "option_id": self.option_id,
            "label": self.label,
            "option_type": self.option_type.value,
            "view_family": self.view_family.value,
            "source_markets": list(self.source_markets),
            "atc4_set": list(self.atc4_set),
            "members": [member.to_dict() for member in self.members],
            "member_status": self.member_status,
            "available_sources": list(self.available_sources),
            "catalog_version": self.catalog_version,
        }


@dataclass(frozen=True, slots=True)
class MarketScopeRequest:
    """Engine/test request for resolving a market-scope selection."""

    brand: str
    view_family: ViewFamily
    source: str
    measure: str
    option_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DedupDiagnostics:
    """Fact-dedup evidence echoed in ``ResolvedScope``."""

    dedup_strategy: str
    dedup_key_version: str
    candidate_fact_count: int
    deduped_fact_count: int
    dropped_duplicate_count: int

    @classmethod
    def empty(cls) -> "DedupDiagnostics":
        """Return diagnostics before facts have been collected."""

        return cls(
            dedup_strategy="pending_fact_collection",
            dedup_key_version=DEDUP_KEY_VERSION,
            candidate_fact_count=0,
            deduped_fact_count=0,
            dropped_duplicate_count=0,
        )

    def to_dict(self) -> dict[str, int | str]:
        """Return JSON-ready diagnostic counters."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    """Canonical resolved scope plus fact-dedup diagnostics."""

    scope_hash: str
    view_family: ViewFamily
    selected_option_ids: tuple[str, ...]
    resolved_source_markets: tuple[str, ...]
    resolved_atc4_set: tuple[str, ...]
    excluded_members: tuple[MarketScopeMember, ...]
    dedup: DedupDiagnostics
    catalog_version: str
    algorithm_version: str = ALGORITHM_VERSION

    def with_dedup(self, diagnostics: DedupDiagnostics) -> "ResolvedScope":
        """Return a copy carrying final fact-dedup evidence."""

        return replace(self, dedup=diagnostics)

    def to_dict(self) -> dict[str, Any]:
        """Return the future response-contract shape."""

        return {
            "scope_hash": self.scope_hash,
            "view_family": self.view_family.value,
            "selected_option_ids": list(self.selected_option_ids),
            "resolved_source_markets": list(self.resolved_source_markets),
            "resolved_atc4_set": list(self.resolved_atc4_set),
            "excluded_members": [member.to_dict() for member in self.excluded_members],
            "dedup": self.dedup.to_dict(),
            "catalog_version": self.catalog_version,
            "algorithm_version": self.algorithm_version,
        }
