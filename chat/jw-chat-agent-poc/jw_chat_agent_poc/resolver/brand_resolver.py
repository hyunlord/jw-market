from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import unicodedata
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
    market_ids: tuple[str, ...] = ()
    market_names: tuple[str, ...] = ()
    support_source: str = "fixture"

    @property
    def requires_market_clarification(self) -> bool:
        return len(self.market_ids) > 1 and self.market_id is None


class UnsupportedBrandError(LookupError):
    """Raised when a brand-required question is outside the supported catalog."""


class BrandMembershipReader(Protocol):
    def brand_memberships(self) -> tuple[dict[str, str], ...]: ...


class BrandResolver:
    def __init__(
        self,
        fixture_path: Path | None = None,
        mode: str | None = None,
        brand_reader: MetricsCacheReader | None = None,
        membership_reader: BrandMembershipReader | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        path = fixture_path or Path(__file__).resolve().parents[1] / "fixtures" / "brand_catalog.json"
        items = json.loads(path.read_text(encoding="utf-8"))
        self._sidecar_by_brand = {str(item["canonical_brand"]): item for item in items}
        self._fixture_items = items
        self._mode = mode or os.environ.get("CHAT_RESOLVER_MODE") or os.environ.get("CHAT_METRICS_MODE", "fixture")
        self._membership_reader = membership_reader
        ttl = ttl_seconds or int(os.environ.get("CHAT_RESOLVER_TTL_SECONDS", "300"))
        self._cache = TtlMetricsCache(brand_reader, ttl_seconds=ttl) if brand_reader is not None else shared_metrics_cache(ttl)

    def resolve(self, question_or_brand: str, allow_default: bool = False) -> BrandResolution:
        normalized = self._normalize(question_or_brand)
        items = sorted(
            self._items(),
            key=lambda item: max(len(self._normalize(alias)) for alias in [item["canonical_brand"], *item.get("aliases", [])]),
            reverse=True,
        )
        for item in items:
            aliases = [item["canonical_brand"], *item.get("aliases", [])]
            if any(self._normalize(alias) in normalized for alias in aliases):
                return self._to_resolution(item, question_or_brand)
        raise UnsupportedBrandError(f"Unsupported brand: {question_or_brand}")

    def resolve_many(self, question_or_brands: str, allow_default: bool = False) -> tuple[BrandResolution, ...]:
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
            out.append(self._to_resolution(item, question_or_brands))
        if out:
            return tuple(out)
        raise UnsupportedBrandError(f"Unsupported brand: {question_or_brands}")

    def supported_brand_count(self) -> int:
        return len(self._items())

    def portfolio_brands(self) -> tuple[BrandResolution, ...]:
        """Return only the JW sidecar portfolio, not the global resolver universe."""

        if self._mode != "cache":
            return tuple(self._to_resolution(item) for item in self._fixture_items)
        portfolio_names = {
            str(item.get("brand") or "")
            for item in self._cache.snapshot().cache_brands
            if item.get("brand")
        }
        return tuple(
            self._to_resolution(item)
            for item in self._items()
            if str(item["canonical_brand"]) in portfolio_names
        )

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
                "market_memberships": [],
                "support_source": "cache_brands+fixture_sidecar" if sidecar else "cache_brands",
            }
        if self._membership_reader is not None:
            for membership in self._membership_reader.brand_memberships():
                name = str(membership.get("brand") or "")
                if not name:
                    continue
                sidecar = self._sidecar_by_brand.get(name, {})
                item = merged.setdefault(
                    name,
                    {
                        "canonical_brand": name,
                        "aliases": list(sidecar.get("aliases", [])),
                        "audit_code": sidecar.get("audit_code", ""),
                        "molecule_en": list(sidecar.get("molecule_en", [])),
                        "atc": list(sidecar.get("atc", [])),
                        "edi_code": sidecar.get("edi_code"),
                        "item_seq": sidecar.get("item_seq"),
                        "market_id": None,
                        "market_name": None,
                        "market_memberships": [],
                        "support_source": "catalog_membership",
                    },
                )
                pair = (str(membership.get("market_id") or ""), str(membership.get("market_name") or ""))
                if pair[0] and pair not in item["market_memberships"]:
                    item["market_memberships"].append(pair)
        return list(merged.values())

    @staticmethod
    def _to_resolution(item: dict[str, Any], question: str = "") -> BrandResolution:
        molecule_en = tuple(str(value) for value in item.get("molecule_en", []))
        memberships = tuple(
            (str(market_id), str(market_name or market_id))
            for market_id, market_name in item.get("market_memberships", ())
            if market_id
        )
        if not memberships and item.get("market_id"):
            memberships = ((str(item["market_id"]), str(item.get("market_name") or item["market_id"])),)
        market_ids = tuple(dict.fromkeys(market_id for market_id, _ in memberships))
        normalized_question = BrandResolver._normalize(question)
        explicit_market = next(
            (
                market_id
                for market_id, market_name in memberships
                if market_id.casefold() in question.casefold()
                or (market_name and BrandResolver._normalize(market_name) in normalized_question)
            ),
            None,
        )
        selected_market = explicit_market or (market_ids[0] if len(market_ids) == 1 else None)
        selected_name = next((name for market_id, name in memberships if market_id == selected_market), None)
        return BrandResolution(
            canonical_brand=str(item["canonical_brand"]),
            audit_code=str(item.get("audit_code") or ""),
            molecule_en=molecule_en,
            atc=tuple(str(value) for value in item.get("atc", [])),
            edi_code=item.get("edi_code"),
            item_seq=item.get("item_seq"),
            is_combo=len(molecule_en) > 1,
            market_id=selected_market,
            market_name=selected_name,
            market_ids=market_ids,
            market_names=tuple(name for _, name in memberships),
            support_source=str(item.get("support_source") or "fixture"),
        )

    @staticmethod
    def _normalize(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text)
        return re.sub(r"\s+", "", normalized).casefold()
