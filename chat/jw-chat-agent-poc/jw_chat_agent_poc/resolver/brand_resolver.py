from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import threading
import unicodedata
from typing import Any, Protocol

from jw_chat_agent_poc.common.timing import trace_span
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
    requested_market_id: str | None = None
    requested_market_name: str | None = None
    support_source: str = "fixture"

    @property
    def requires_market_clarification(self) -> bool:
        return len(self.market_ids) > 1 and self.market_id is None

    @property
    def has_market_membership_mismatch(self) -> bool:
        return (
            self.requested_market_id is not None
            and self.requested_market_id not in self.market_ids
        )


class UnsupportedBrandError(LookupError):
    """Raised when a brand-required question is outside the supported catalog."""


class BrandMembershipReader(Protocol):
    def brand_memberships(self) -> tuple[dict[str, str], ...]: ...


class BrandMoleculeReader(Protocol):
    def brand_molecules(self) -> tuple[dict[str, str], ...]: ...


class BrandResolver:
    def __init__(
        self,
        fixture_path: Path | None = None,
        mode: str | None = None,
        brand_reader: MetricsCacheReader | None = None,
        membership_reader: BrandMembershipReader | None = None,
        molecule_reader: BrandMoleculeReader | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        path = fixture_path or Path(__file__).resolve().parents[1] / "fixtures" / "brand_catalog.json"
        items = json.loads(path.read_text(encoding="utf-8"))
        self._sidecar_by_brand = {str(item["canonical_brand"]): item for item in items}
        self._fixture_items = items
        self._mode = mode or os.environ.get("CHAT_RESOLVER_MODE") or os.environ.get("CHAT_METRICS_MODE", "fixture")
        self._membership_reader = membership_reader
        self._molecule_reader = molecule_reader
        self._catalog_lock = threading.Lock()
        self._catalog_sources: tuple[object, ...] | None = None
        self._catalog_items: tuple[dict[str, Any], ...] | None = None
        ttl = ttl_seconds or int(os.environ.get("CHAT_RESOLVER_TTL_SECONDS", "300"))
        self._cache = TtlMetricsCache(brand_reader, ttl_seconds=ttl) if brand_reader is not None else shared_metrics_cache(ttl)

    def resolve(self, question_or_brand: str, allow_default: bool = False) -> BrandResolution:
        normalized = self._normalize(question_or_brand)
        with trace_span("brand_catalog_load", f"mode={self._mode}; operation=resolve", category="resolver"):
            raw_items = self._items()
        with trace_span("brand_alias_match_one", f"catalog_size={len(raw_items)}", category="resolver"):
            market_universe = self._market_universe(raw_items, self._fixture_items)
            items = sorted(
                raw_items,
                key=lambda item: max(len(self._normalize(alias)) for alias in [item["canonical_brand"], *item.get("aliases", [])]),
                reverse=True,
            )
            for item in items:
                aliases = [item["canonical_brand"], *item.get("aliases", [])]
                if any(self._normalize(alias) in normalized for alias in aliases):
                    return self._to_resolution(
                        item,
                        question_or_brand,
                        market_universe=market_universe,
                    )
        raise UnsupportedBrandError(f"Unsupported brand: {question_or_brand}")

    def resolve_many(self, question_or_brands: str, allow_default: bool = False) -> tuple[BrandResolution, ...]:
        normalized = self._normalize(question_or_brands)
        with trace_span("brand_catalog_load", f"mode={self._mode}; operation=resolve_many", category="resolver"):
            items = self._items()
        with trace_span("brand_alias_match_many", f"catalog_size={len(items)}", category="resolver"):
            market_universe = self._market_universe(items, self._fixture_items)
            spans: list[tuple[int, int, dict[str, Any]]] = []
            for item in items:
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
                out.append(
                    self._to_resolution(
                        item,
                        question_or_brands,
                        market_universe=market_universe,
                    )
                )
        if out:
            return tuple(out)
        raise UnsupportedBrandError(f"Unsupported brand: {question_or_brands}")

    def explicit_market(self, question: str) -> tuple[str, str] | None:
        runtime_items = self._items()
        return self._explicit_market(
            question,
            self._market_universe(runtime_items, self._fixture_items),
        )

    def supported_brand_count(self) -> int:
        return len(self._items())

    def has_explicit_alias(self, question: str) -> bool:
        if self.has_fixture_alias(question):
            return True
        try:
            self.resolve(question, allow_default=False)
        except (UnsupportedBrandError, OSError):
            return False
        except Exception:
            return False
        return True

    def has_fixture_alias(self, question: str) -> bool:
        normalized = self._normalize(question)
        return any(
            any(
                self._normalize(str(alias)) in normalized
                for alias in (item["canonical_brand"], *item.get("aliases", []))
            )
            for item in self._fixture_items
        )

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
            return self._assembled_items((self._fixture_items,), (), (), ())

        with trace_span("brand_cache_snapshot", "cache_brands snapshot", category="resolver"):
            snapshot = self._cache.snapshot()
            cache_brands = tuple(snapshot.cache_brands)
        memberships: tuple[dict[str, str], ...] = ()
        if self._membership_reader is not None:
            with trace_span("brand_membership_load", "mart and catalog membership load", category="resolver"):
                memberships = self._membership_reader.brand_memberships()
        brand_molecules: tuple[dict[str, str], ...] = ()
        if self._molecule_reader is not None:
            with trace_span("brand_molecule_load", "mart brand molecule load", category="resolver"):
                brand_molecules = self._molecule_reader.brand_molecules()
        sources = (snapshot, memberships, brand_molecules)
        return self._assembled_items(sources, cache_brands, memberships, brand_molecules)

    def _assembled_items(
        self,
        sources: tuple[object, ...],
        cache_brands: tuple[dict[str, Any], ...],
        memberships: tuple[dict[str, str], ...],
        brand_molecules: tuple[dict[str, str], ...],
    ) -> list[dict[str, Any]]:
        cached = self._catalog_items
        if cached is not None and self._same_catalog_sources(sources):
            return list(cached)
        with self._catalog_lock:
            cached = self._catalog_items
            if cached is not None and self._same_catalog_sources(sources):
                return list(cached)
            with trace_span("brand_catalog_assembly", f"mode={self._mode}", category="resolver"):
                items = self._assemble_items(cache_brands, memberships, brand_molecules)
            self._catalog_items = tuple(items)
            self._catalog_sources = sources
            return list(self._catalog_items)

    def _same_catalog_sources(self, sources: tuple[object, ...]) -> bool:
        current = self._catalog_sources
        return current is not None and len(current) == len(sources) and all(
            previous is incoming for previous, incoming in zip(current, sources, strict=True)
        )

    def _assemble_items(
        self,
        cache_brands: tuple[dict[str, Any], ...],
        memberships: tuple[dict[str, str], ...],
        brand_molecules: tuple[dict[str, str], ...],
    ) -> list[dict[str, Any]]:
        if self._mode != "cache":
            return list(self._fixture_items)
        merged: dict[str, dict[str, Any]] = {}
        for brand in cache_brands:
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
        if memberships:
            for membership in memberships:
                name = str(membership.get("brand") or "")
                if not name:
                    continue
                sidecar = self._sidecar_by_brand.get(name, {})
                membership_source = str(membership.get("support_source") or "catalog_membership")
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
                        "support_source": membership_source,
                    },
                )
                if membership_source == "strategic_mart" and item["support_source"] in {
                    "catalog_alias",
                    "catalog_membership",
                }:
                    item["support_source"] = membership_source
                pair = (str(membership.get("market_id") or ""), str(membership.get("market_name") or ""))
                if pair[0] and pair not in item["market_memberships"]:
                    item["market_memberships"].append(pair)
        if brand_molecules:
            molecule_by_brand: dict[str, list[str]] = {}
            for row in brand_molecules:
                molecule = str(row.get("molecule_display") or row.get("molecule_norm") or "").strip()
                if not molecule:
                    continue
                for key in (row.get("brand_name"), row.get("brand_key"), row.get("brand")):
                    normalized_key = self._normalize(str(key or ""))
                    if normalized_key:
                        molecules = molecule_by_brand.setdefault(normalized_key, [])
                        if molecule.casefold() not in {value.casefold() for value in molecules}:
                            molecules.append(molecule)
            for item in merged.values():
                aliases = (item["canonical_brand"], *item.get("aliases", []))
                additions: list[str] = []
                for alias in aliases:
                    additions.extend(molecule_by_brand.get(self._normalize(str(alias)), []))
                existing = item.setdefault("molecule_en", [])
                for molecule in additions:
                    if molecule.casefold() not in {value.casefold() for value in existing}:
                        existing.append(molecule)
                if additions and "mart_brand_molecule" not in item["support_source"]:
                    item["support_source"] = f"{item['support_source']}+mart_brand_molecule"
        return list(merged.values())

    @staticmethod
    def _to_resolution(
        item: dict[str, Any],
        question: str = "",
        *,
        market_universe: tuple[tuple[str, str], ...] = (),
    ) -> BrandResolution:
        molecule_en = tuple(str(value) for value in item.get("molecule_en", []))
        memberships = BrandResolver._item_memberships(item)
        market_ids = tuple(dict.fromkeys(market_id for market_id, _ in memberships))
        requested_market = BrandResolver._explicit_market(
            question,
            market_universe or memberships,
        )
        requested_market_id = requested_market[0] if requested_market is not None else None
        requested_market_name = requested_market[1] if requested_market is not None else None
        explicit_membership = (
            requested_market_id
            if requested_market_id in market_ids
            else None
        )
        has_mismatch = requested_market_id is not None and explicit_membership is None
        selected_market = (
            explicit_membership
            or (market_ids[0] if len(market_ids) == 1 and not has_mismatch else None)
        )
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
            requested_market_id=requested_market_id,
            requested_market_name=requested_market_name,
            support_source=str(item.get("support_source") or "fixture"),
        )

    @staticmethod
    def _item_memberships(item: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        memberships = tuple(
            (str(market_id), str(market_name or market_id))
            for market_id, market_name in item.get("market_memberships", ())
            if market_id
        )
        if memberships or not item.get("market_id"):
            return memberships
        market_id = str(item["market_id"])
        return ((market_id, str(item.get("market_name") or market_id)),)

    @staticmethod
    def _market_universe(
        items: list[dict[str, Any]],
        alias_items: list[dict[str, Any]] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        markets: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            for market_id, market_name in BrandResolver._item_memberships(item):
                pair = (market_id, market_name)
                if pair not in seen:
                    markets.append(pair)
                    seen.add(pair)
        runtime_ids = {market_id for market_id, _ in markets}
        for item in alias_items or ():
            for market_id, market_name in BrandResolver._item_memberships(item):
                pair = (market_id, market_name)
                if market_id in runtime_ids and pair not in seen:
                    markets.append(pair)
                    seen.add(pair)
        return tuple(markets)

    @staticmethod
    def _explicit_market(
        question: str,
        markets: tuple[tuple[str, str], ...],
    ) -> tuple[str, str] | None:
        question_casefold = question.casefold()
        normalized_question = BrandResolver._normalize(question)
        for market_id, market_name in markets:
            if market_id.casefold() in question_casefold:
                return market_id, market_name
        candidates = sorted(
            markets,
            key=lambda item: len(BrandResolver._normalize(item[1])),
            reverse=True,
        )
        for market_id, market_name in candidates:
            normalized_name = BrandResolver._normalize(market_name)
            if normalized_name and normalized_name in normalized_question:
                return market_id, market_name
        alias_matches: dict[str, tuple[str, str]] = {}
        for market_id, market_name in candidates:
            for alias in BrandResolver._market_query_aliases(market_name):
                if alias in normalized_question:
                    alias_matches.setdefault(market_id, (market_id, market_name))
                    break
        if len(alias_matches) == 1:
            return next(iter(alias_matches.values()))
        return None

    @staticmethod
    def _market_query_aliases(market_name: str) -> tuple[str, ...]:
        """Derive only conservative query forms from the canonical market name."""

        normalized_name = BrandResolver._normalize(market_name)
        base_name = re.sub(r"시장$", "", normalized_name)
        base_name = re.sub(r"치료제$", "", base_name)
        if base_name == normalized_name or len(base_name) < 3:
            return ()
        return (f"{base_name}시장",)

    @staticmethod
    def _normalize(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text)
        return re.sub(r"\s+", "", normalized).casefold()
