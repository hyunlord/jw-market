"""Versioned GROUP_01 market-scope catalog loader."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from pipeline.scripts.api.market_scope.types import (
    MarketScopeMember,
    MarketScopeOption,
    OptionType,
    ViewFamily,
)


DEFAULT_CATALOG_PATH = Path(__file__).with_name("catalogs") / "group_01_market_model.json"


@dataclass(frozen=True, slots=True)
class SourceMarketDefinition:
    """Runtime metadata for one strategy source market."""

    source_market: str
    label: str
    atc4_set: tuple[str, ...]
    available_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketScopeCatalog:
    """Loaded GROUP_01 catalog plus source-market metadata."""

    catalog_version: str
    source_markets: dict[str, SourceMarketDefinition]
    group_options: tuple[MarketScopeOption, ...]

    @classmethod
    def load_default(cls) -> "MarketScopeCatalog":
        """Load the bundled GROUP_01 catalog JSON."""

        return cls.from_path(DEFAULT_CATALOG_PATH)

    @classmethod
    def from_path(cls, path: Path) -> "MarketScopeCatalog":
        """Load a market-scope catalog from a JSON file."""

        payload = json.loads(path.read_text(encoding="utf-8"))
        catalog_version = str(payload["catalog_version"])
        source_markets = {
            str(row["source_market"]): SourceMarketDefinition(
                source_market=str(row["source_market"]),
                label=str(row["label"]),
                atc4_set=_tuple(row.get("atc4_set")),
                available_sources=_tuple(row.get("available_sources")),
            )
            for row in payload.get("source_markets", [])
        }
        group_options = tuple(
            _group_option(row, source_markets=source_markets, catalog_version=catalog_version)
            for row in payload.get("groups", [])
        )
        return cls(
            catalog_version=catalog_version,
            source_markets=source_markets,
            group_options=group_options,
        )

    def options_for_brand(self, brand: str, *, view_family: ViewFamily) -> tuple[MarketScopeOption, ...]:
        """Return source-market and group options relevant to ``brand``."""

        if view_family is not ViewFamily.STRATEGY:
            return ()
        brand_name = brand.strip()
        found: dict[str, MarketScopeOption] = {}
        for group in self.group_options:
            if not any(member.brand_name == brand_name for member in group.members):
                continue
            for source_market in group.source_markets:
                found[f"source:{source_market}"] = self._source_option(source_market)
            found[group.option_id] = group
        return tuple(found[key] for key in sorted(found))

    def _source_option(self, source_market: str) -> MarketScopeOption:
        """Build a source-market option from source metadata."""

        meta = self.source_markets[source_market]
        member = MarketScopeMember(
            brand_name=meta.label,
            source_market=meta.source_market,
            atc4_set=meta.atc4_set,
            member_status="present",
            reason=None,
        )
        return MarketScopeOption(
            option_id=f"source:{meta.source_market}",
            label=meta.label,
            option_type=OptionType.SOURCE_MARKET,
            view_family=ViewFamily.STRATEGY,
            source_markets=(meta.source_market,),
            atc4_set=meta.atc4_set,
            members=(member,),
            member_status="present",
            available_sources=meta.available_sources,
            catalog_version=self.catalog_version,
        )


def _group_option(
    row: dict[str, Any],
    *,
    source_markets: dict[str, SourceMarketDefinition],
    catalog_version: str,
) -> MarketScopeOption:
    """Convert a GROUP_01 JSON row to a contract option."""

    members = tuple(
        MarketScopeMember(
            brand_name=str(member["brand_name"]),
            source_market=str(member["source_market"]) if member.get("source_market") else None,
            atc4_set=_tuple(member.get("atc4_set")),
            member_status=member["member_status"],
            reason=member.get("reason"),
        )
        for member in row.get("members", [])
    )
    present_source_markets = _dedupe_sorted(
        member.source_market
        for member in members
        if member.member_status == "present" and member.source_market
    )
    available_sources = _dedupe_sorted(
        source
        for source_market in present_source_markets
        for source in source_markets[source_market].available_sources
    )
    atc4_set = _dedupe_sorted(
        atc4
        for member in members
        if member.member_status == "present"
        for atc4 in member.atc4_set
    )
    return MarketScopeOption(
        option_id=f"group:{row['group_id']}",
        label=str(row["label"]),
        option_type=OptionType.GROUP_UNION,
        view_family=ViewFamily.STRATEGY,
        source_markets=present_source_markets,
        atc4_set=atc4_set,
        members=members,
        member_status="present",
        available_sources=available_sources,
        catalog_version=catalog_version,
    )


def _tuple(value: Any) -> tuple[str, ...]:
    """Normalize a JSON list to a tuple of non-empty strings."""

    return tuple(str(item).strip() for item in value or () if str(item).strip())


def _dedupe_sorted(values: Any) -> tuple[str, ...]:
    """Return stable sorted unique strings from an iterable."""

    return tuple(sorted({str(value).strip() for value in values if str(value or "").strip()}))

