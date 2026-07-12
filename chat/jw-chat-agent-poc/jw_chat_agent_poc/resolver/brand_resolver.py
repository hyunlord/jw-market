from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
from typing import Any, Protocol

from jw_chat_agent_poc.tools.metrics.cache_live import (
    MetricsCacheReader,
    TtlMetricsCache,
    shared_metrics_cache,
)


@dataclass(frozen=True)
class BrandResolution:
    canonical_brand: str
    audit_code: str
    molecule_en: tuple[str, ...]
    atc: tuple[str, ...]
    edi_code: str | None
    item_seq: str | None
    is_combo: bool
    market_id: str | None = None
    market_name: str | None = None
    support_source: str = "fixture"


class UnsupportedBrandError(LookupError):
    """Raised when a brand-required question is outside the supported catalog."""


class BrandMembershipReader(Protocol):
    def brand_memberships(self) -> tuple[dict[str, str], ...]: ...


class BrandResolver:
    def __init__(
        self,
        fixture_path: Path | None = None,
        default_brand: str = "리바로",
        mode: str | None = None,
        brand_reader: MetricsCacheReader | None = None,
        membership_reader: BrandMembershipReader | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        path = fixture_path or Path(__file__).resolve().parents[1] / "fixtures" / "brand_catalog.json"
        items = json.loads(path.read_text(encoding="utf-8"))
        self._sidecar_by_brand = {str(item["canonical_brand"]): item for item in items}
        self._fixture_items = items
        self._default_brand = default_brand
        self._mode = mode or os.environ.get("CHAT_RESOLVER_MODE") or os.environ.get("CHAT_METRICS_MODE", "fixture")
        self._membership_reader = membership_reader
        ttl = ttl_seconds or int(os.environ.get("CHAT_RESOLVER_TTL_SECONDS", "300"))
        self._cache = TtlMetricsCache(brand_reader, ttl_seconds=ttl) if brand_reader is not None else shared_metrics_cache(ttl)

    def resolve(self, question_or_brand: str, allow_default: bool = True) -> BrandResolution:
        normalized = self._normalize(question_or_brand)
        items = sorted(
            self._items(),
            key=lambda item: max(len(self._normalize(alias)) for alias in [item["canonical_brand"], *item.get("aliases", [])]),
            reverse=True,
        )
        for item in items:
            aliases = [item["canonical_brand"], *item.get("aliases", [])]
            if any(self._normalize(alias) in normalized for alias in aliases):
                return self._to_resolution(item)
        if not allow_default:
            raise UnsupportedBrandError(f"Unsupported brand: {question_or_brand}")
        for item in self._items():
            if item["canonical_brand"] == self._default_brand:
                return self._to_resolution(item)
        raise LookupError(f"No fixture brand matched and default is missing: {question_or_brand}")

    def resolve_many(self, question_or_brands: str, allow_default: bool = True) -> tuple[BrandResolution, ...]:
        normalized = self._normalize(question_or_brands)
        spans: list[tuple[int, int, dict[str, Any]]] = []
        for item in self._items():
            aliases = [item["canonical_brand"], *item.get("aliases", [])]
            for alias in aliases:
                normalized_alias = self._normalize(str(alias))
                start = normalized.find(normalized_alias)
                while normalized_alias and start >= 0:
                    spans.append((start, start + len(normalized_alias), item))
                    start = normalized.find(normalized_alias, start + 1)
        selected: list[tuple[int, dict[str, Any]]] = []
        occupied: list[tuple[int, int]] = []
        for start, end, item in sorted(spans, key=lambda span: (-(span[1] - span[0]), span[0])):
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            occupied.append((start, end))
            selected.append((start, item))
        seen: set[str] = set()
        out: list[BrandResolution] = []
        for _, item in sorted(selected, key=lambda pair: pair[0]):
            canonical = str(item["canonical_brand"])
            if canonical in seen:
                continue
            seen.add(canonical)
            out.append(self._to_resolution(item))
        if out:
            return tuple(out)
        if not allow_default:
            raise UnsupportedBrandError(f"Unsupported brand: {question_or_brands}")
        return (self.resolve(question_or_brands, allow_default=True),)

    def supported_brand_count(self) -> int:
        return len(self._items())

    def portfolio_brands(self) -> tuple[BrandResolution, ...]:
        """Return the supported strategic brand catalog for company-scope analysis."""

        return tuple(self._to_resolution(item) for item in self._items())

    def _items(self) -> list[dict[str, Any]]:
        if self._mode != "cache":
            return list(self._fixture_items)
        merged: dict[str, dict[str, Any]] = {}
        for brand in self._cache.snapshot().cache_brands:
            name = str(brand.get("brand") or "")
            if not name:
                continue
            sidecar = self._sidecar_by_brand.get(name, {})
            merged[name] = {
                "canonical_brand": name,
                "aliases": list(sidecar.get("aliases", [])),
                "audit_code": sidecar.get("audit_code", ""),
                "molecule_en": list(sidecar.get("molecule_en", [])),
                "atc": list(sidecar.get("atc", [])),
                "edi_code": sidecar.get("edi_code"),
                "item_seq": sidecar.get("item_seq"),
                "market_id": brand.get("market_id"),
                "market_name": brand.get("market_name"),
                "support_source": "cache_brands+fixture_sidecar" if sidecar else "cache_brands",
            }
        if self._membership_reader is not None:
            for membership in self._membership_reader.brand_memberships():
                name = str(membership.get("brand") or "")
                if not name or name in merged:
                    continue
                merged[name] = {
                    "canonical_brand": name,
                    "aliases": [],
                    "audit_code": "",
                    "molecule_en": [],
                    "atc": [],
                    "edi_code": None,
                    "item_seq": None,
                    "market_id": membership.get("market_id"),
                    "market_name": membership.get("market_name"),
                    "support_source": "mart_membership",
                }
        return list(merged.values())

    @staticmethod
    def _to_resolution(item: dict[str, Any]) -> BrandResolution:
        molecule_en = tuple(str(value) for value in item.get("molecule_en", []))
        return BrandResolution(
            canonical_brand=str(item["canonical_brand"]),
            audit_code=str(item.get("audit_code") or ""),
            molecule_en=molecule_en,
            atc=tuple(str(value) for value in item.get("atc", [])),
            edi_code=item.get("edi_code"),
            item_seq=item.get("item_seq"),
            is_combo=len(molecule_en) > 1,
            market_id=item.get("market_id"),
            market_name=item.get("market_name"),
            support_source=str(item.get("support_source") or "fixture"),
        )

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\\s+", "", text).lower()
