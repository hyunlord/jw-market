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
    source_variance: bool = False
    resolved_via_alias: bool = False

    @property
    def requires_market_clarification(self) -> bool:
        return len(self.market_ids) > 1 and self.market_id is None

    @property
    def has_market_membership_mismatch(self) -> bool:
        return self.requested_market_id is not None and self.market_id is None


class UnsupportedBrandError(LookupError):
    """Raised when a brand-required question is outside the supported catalog."""


class AmbiguousBrandError(LookupError):
    """Raised when one exact alias maps to multiple canonical brands."""

    def __init__(self, message: str | None = None, *, query: str = "", candidates: tuple[str, ...] = ()) -> None:
        self.query = query
        self.candidates = tuple(candidates)
        detail = message or f"Ambiguous brand: {query}; candidates={','.join(self.candidates)}"
        super().__init__(detail)


class BrandMembershipReader(Protocol):
    def brand_memberships(self) -> tuple[dict[str, str], ...]: ...


class BrandMoleculeReader(Protocol):
    def brand_molecules(self) -> tuple[dict[str, str], ...]: ...


class BrandAliasReader(Protocol):
    def brand_aliases(self) -> tuple[dict[str, str], ...]: ...


class BrandResolver:
    def __init__(
        self,
        fixture_path: Path | None = None,
        mode: str | None = None,
        brand_reader: MetricsCacheReader | None = None,
        membership_reader: BrandMembershipReader | None = None,
        molecule_reader: BrandMoleculeReader | None = None,
        alias_reader: BrandAliasReader | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        path = fixture_path or Path(__file__).resolve().parents[1] / "fixtures" / "brand_catalog.json"
        items = json.loads(path.read_text(encoding="utf-8"))
        self._sidecar_by_brand = {str(item["canonical_brand"]): item for item in items}
        self._fixture_items = items
        self._mode = mode or os.environ.get("CHAT_RESOLVER_MODE") or os.environ.get("CHAT_METRICS_MODE", "fixture")
        self._membership_reader = membership_reader
        self._molecule_reader = molecule_reader
        self._runtime_alias_reader = alias_reader
        self._catalog_lock = threading.Lock()
        self._catalog_sources: tuple[object, ...] | None = None
        self._catalog_items: tuple[dict[str, Any], ...] | None = None
        self._alias_index_source: object | None = None
        self._alias_index: dict[str, tuple[dict[str, Any], ...]] = {}
        self._alias_window_size = 1
        ttl = ttl_seconds or int(os.environ.get("CHAT_RESOLVER_TTL_SECONDS", "300"))
        self._cache = TtlMetricsCache(brand_reader, ttl_seconds=ttl) if brand_reader is not None else shared_metrics_cache(ttl)

    def resolve(self, question_or_brand: str, allow_default: bool = False) -> BrandResolution:
        with trace_span("brand_catalog_load", f"mode={self._mode}; operation=resolve", category="resolver"):
            raw_items = self._items()
        with trace_span("brand_alias_match_one", f"catalog_size={len(raw_items)}", category="resolver"):
            market_universe = self._market_universe(raw_items, self._fixture_items)
            matches = self._matching_spans(question_or_brand, raw_items)
            self._raise_family_ambiguity(question_or_brand, raw_items)
            if matches:
                start, end, key, candidates = matches[0]
                selected = self._select_candidate(question_or_brand, start, end, key, candidates)
                return self._to_resolution(
                    selected,
                    question_or_brand,
                    market_universe=market_universe,
                    matched_key=key,
                    matched_literal=question_or_brand[start:end],
                )
        raise UnsupportedBrandError(f"Unsupported brand: {question_or_brand}")

    def resolve_many(self, question_or_brands: str, allow_default: bool = False) -> tuple[BrandResolution, ...]:
        with trace_span("brand_catalog_load", f"mode={self._mode}; operation=resolve_many", category="resolver"):
            items = self._items()
        with trace_span("brand_alias_match_many", f"catalog_size={len(items)}", category="resolver"):
            market_universe = self._market_universe(items, self._fixture_items)
            spans = self._matching_spans(question_or_brands, items)
            self._raise_family_ambiguity(question_or_brands, items)
            selected: list[tuple[int, str, str, dict[str, Any]]] = []
            occupied: list[tuple[int, int]] = []
            for start, end, key, candidates in spans:
                if any(start < used_end and end > used_start for used_start, used_end in occupied):
                    continue
                item = self._select_candidate(question_or_brands, start, end, key, candidates)
                occupied.append((start, end))
                selected.append((start, key, question_or_brands[start:end], item))
            seen: set[str] = set()
            out: list[BrandResolution] = []
            for _, key, literal, item in sorted(selected, key=lambda pair: pair[0]):
                canonical = str(item["canonical_brand"])
                if canonical in seen:
                    continue
                seen.add(canonical)
                out.append(
                    self._to_resolution(
                        item,
                        question_or_brands,
                        market_universe=market_universe,
                        matched_key=key,
                        matched_literal=literal,
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

    def market_members(self, question: str) -> tuple[str, ...]:
        runtime_items = self._items()
        requested_market = self._explicit_market(
            question,
            self._market_universe(runtime_items, self._fixture_items),
        )
        if requested_market is None:
            return ()
        requested_market_id = requested_market[0]
        return tuple(
            sorted(
                {
                    str(item["canonical_brand"])
                    for item in runtime_items
                    if any(
                        self._equivalent_market_id(requested_market_id, {market_id})
                        for market_id, _ in self._item_memberships(item)
                    )
                }
            )
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
        return bool(self._matching_spans(question, self._fixture_items))

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
        aliases: tuple[dict[str, str], ...] = ()
        if self._runtime_alias_reader is not None:
            with trace_span("brand_alias_load", "brand alias load", category="resolver"):
                aliases = self._runtime_alias_reader.brand_aliases()
        sources = (snapshot, memberships, brand_molecules, aliases)
        return self._assembled_items(
            sources,
            cache_brands,
            memberships,
            brand_molecules,
            aliases,
        )

    def _assembled_items(
        self,
        sources: tuple[object, ...],
        cache_brands: tuple[dict[str, Any], ...],
        memberships: tuple[dict[str, str], ...],
        brand_molecules: tuple[dict[str, str], ...],
        aliases: tuple[dict[str, str], ...] = (),
    ) -> list[dict[str, Any]]:
        cached = self._catalog_items
        if cached is not None and self._same_catalog_sources(sources):
            return list(cached)
        with self._catalog_lock:
            cached = self._catalog_items
            if cached is not None and self._same_catalog_sources(sources):
                return list(cached)
            with trace_span("brand_catalog_assembly", f"mode={self._mode}", category="resolver"):
                items = self._assemble_items(
                    cache_brands,
                    memberships,
                    brand_molecules,
                    aliases,
                )
            self._catalog_items = tuple(items)
            self._catalog_sources = sources
            self._alias_index_source = None
            self._alias_index = {}
            return list(self._catalog_items)

    def _same_catalog_sources(self, sources: tuple[object, ...]) -> bool:
        current = self._catalog_sources
        return current is not None and len(current) == len(sources) and all(
            previous is incoming for previous, incoming in zip(current, sources, strict=True)
        )

    def observability(self) -> dict[str, int]:
        with self._catalog_lock:
            return {"row_count": len(self._catalog_items or ())}

    def _assemble_items(
        self,
        cache_brands: tuple[dict[str, Any], ...],
        memberships: tuple[dict[str, str], ...],
        brand_molecules: tuple[dict[str, str], ...],
        aliases: tuple[dict[str, str], ...] = (),
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
                alias = str(membership.get("brand_alias") or "").strip()
                if alias and alias != name and alias not in item["aliases"]:
                    item["aliases"].append(alias)
                if membership_source == "strategic_mart" and not str(item["support_source"]).startswith(
                    "cache_brands"
                ):
                    item["support_source"] = membership_source
                pair = (
                    str(membership.get("market_id") or ""),
                    str(membership.get("market_name") or ""),
                )
                if pair[0] and pair not in item["market_memberships"]:
                    item["market_memberships"].append(pair)
        if aliases:
            by_brand_key: dict[str, list[str]] = {}
            for row in aliases:
                alias_name = str(row.get("alias_name") or "").strip()
                brand_key = self._normalize(str(row.get("brand_key") or ""))
                if not alias_name or not brand_key:
                    continue
                values = by_brand_key.setdefault(brand_key, [])
                if alias_name not in values:
                    values.append(alias_name)
            # The load-time guard compares alias_name against stored brand_KEYs after
            # NFKC+strip. This index folds harder: it also removes every space and
            # casefolds, and it keys on the display name. So an alias can clear the
            # guard and still land on another brand's key here, which makes a name
            # that resolves today ambiguous instead. Skip those; keep the ones that
            # fold onto this item's own key, which add spellings without adding a
            # second owner.
            canonical_keys = {
                self._normalize(str(entry["canonical_brand"])) for entry in merged.values()
            }
            canonical_keys.discard("")
            for item in merged.values():
                brand_key = self._normalize(str(item["canonical_brand"]))
                runtime_alias_keys = item.setdefault("_runtime_alias_keys", [])
                for alias_name in by_brand_key.get(brand_key, ()):
                    alias_key = self._normalize(alias_name)
                    if alias_key and alias_key != brand_key and alias_key in canonical_keys:
                        continue
                    if alias_name not in item["aliases"]:
                        item["aliases"].append(alias_name)
                    if alias_key and alias_key not in runtime_alias_keys:
                        runtime_alias_keys.append(alias_key)
        if brand_molecules:
            molecule_by_brand: dict[str, list[str]] = {}
            source_sets_by_brand: dict[str, dict[str, set[str]]] = {}
            for row in brand_molecules:
                molecule = str(row.get("molecule_display") or row.get("molecule_norm") or "").strip()
                molecule_norm = str(row.get("molecule_norm") or molecule).strip().casefold()
                mart_source = str(row.get("mart_source") or "unknown").strip() or "unknown"
                if not molecule:
                    continue
                for key in (row.get("brand_name"), row.get("brand_key"), row.get("brand")):
                    normalized_key = self._normalize(str(key or ""))
                    if normalized_key:
                        molecules = molecule_by_brand.setdefault(normalized_key, [])
                        if molecule.casefold() not in {value.casefold() for value in molecules}:
                            molecules.append(molecule)
                        source_sets_by_brand.setdefault(normalized_key, {}).setdefault(
                            mart_source,
                            set(),
                        ).add(molecule_norm)
            for item in merged.values():
                aliases = (item["canonical_brand"], *item.get("aliases", []))
                additions: list[str] = []
                source_sets: dict[str, set[str]] = {}
                for alias in aliases:
                    normalized_alias = self._normalize(str(alias))
                    additions.extend(molecule_by_brand.get(normalized_alias, []))
                    for source, molecules in source_sets_by_brand.get(
                        normalized_alias,
                        {},
                    ).items():
                        source_sets.setdefault(source, set()).update(molecules)
                existing = item.setdefault("molecule_en", [])
                for molecule in additions:
                    if molecule.casefold() not in {value.casefold() for value in existing}:
                        existing.append(molecule)
                item["source_variance"] = len(
                    {tuple(sorted(values)) for values in source_sets.values()}
                ) > 1
                if additions and "mart_brand_molecule" not in item["support_source"]:
                    item["support_source"] = f"{item['support_source']}+mart_brand_molecule"
        return list(merged.values())

    def _matching_spans(
        self,
        text: str,
        items: list[dict[str, Any]],
    ) -> list[tuple[int, int, str, tuple[dict[str, Any], ...]]]:
        index, max_window = self._alias_lookup(items)
        normalized_text = unicodedata.normalize("NFKC", text)
        tokens = tuple(re.finditer(r"[0-9A-Za-z가-힣+_.-]+", normalized_text))
        matches: dict[tuple[int, int, str], tuple[dict[str, Any], ...]] = {}
        for start_index, token in enumerate(tokens):
            for end_index in range(start_index, min(len(tokens), start_index + max_window)):
                start = token.start()
                end = tokens[end_index].end()
                candidate = self._normalize(normalized_text[start:end])
                keys = [candidate]
                for particle in _BRAND_PARTICLES:
                    if candidate.endswith(particle) and len(candidate) > len(particle) + 1:
                        keys.append(candidate[: -len(particle)])
                for key in keys:
                    candidates = index.get(key)
                    if candidates:
                        matches[(start, end, key)] = candidates
        return sorted(
            ((start, end, key, candidates) for (start, end, key), candidates in matches.items()),
            key=lambda match: (-len(match[2]), match[0], match[1]),
        )

    def _alias_lookup(
        self,
        items: list[dict[str, Any]],
    ) -> tuple[dict[str, tuple[dict[str, Any], ...]], int]:
        source: object = self._fixture_items if items is self._fixture_items else (self._catalog_items or items)
        if source is self._alias_index_source:
            return self._alias_index, self._alias_window_size
        grouped: dict[str, list[dict[str, Any]]] = {}
        max_window = 1
        for item in items:
            for raw_alias in (item["canonical_brand"], *item.get("aliases", [])):
                alias = str(raw_alias).strip()
                key = self._normalize(alias)
                if not key:
                    continue
                bucket = grouped.setdefault(key, [])
                if item not in bucket:
                    bucket.append(item)
                max_window = max(max_window, len(re.findall(r"[0-9A-Za-z가-힣+_.-]+", alias)))
        self._alias_index = {key: tuple(value) for key, value in grouped.items()}
        self._alias_window_size = max(4, max_window)
        self._alias_index_source = source
        return self._alias_index, self._alias_window_size

    def _raise_family_ambiguity(self, text: str, items: list[dict[str, Any]]) -> None:
        index, _ = self._alias_lookup(items)
        normalized_text = unicodedata.normalize("NFKC", text)
        # 계열 asks for a product family exactly like 패밀리 does, so both markers raise the
        # same ambiguity instead of quietly narrowing to whichever single brand the alias
        # matcher happened to find. The trailing boundary keeps 계열사·계열회사·계열화 out:
        # those continue past the marker into another word and are not family requests.
        for match in re.finditer(
            r"(?P<prefix>[0-9A-Za-z가-힣+_.-]{2,})\s*(?P<marker>패밀리|계열)(?![0-9A-Za-z가-힣+_.-])",
            normalized_text,
        ):
            prefix = self._normalize(match.group("prefix"))
            family_key = f"{prefix}{match.group('marker')}"
            if family_key in index:
                continue
            candidates = sorted(
                {
                    str(item["canonical_brand"])
                    for item in items
                    if self._normalize(str(item["canonical_brand"])).startswith(prefix)
                },
                key=lambda value: (len(self._normalize(value)), self._normalize(value), value),
            )
            if len(candidates) > 1:
                raise AmbiguousBrandError(query=family_key, candidates=tuple(candidates))

    @staticmethod
    def _select_candidate(
        text: str,
        start: int,
        end: int,
        key: str,
        candidates: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        canonical = {str(item["canonical_brand"]) for item in candidates}
        if len(canonical) == 1:
            return candidates[0]

        normalized_text = unicodedata.normalize("NFKC", text)
        literal = normalized_text[start:end].strip().casefold()
        literal_forms = {literal}
        for particle in _BRAND_PARTICLES:
            if literal.endswith(particle) and len(literal) > len(particle) + 1:
                literal_forms.add(literal[: -len(particle)].rstrip())
        exact = [
            item
            for item in candidates
            if unicodedata.normalize("NFKC", str(item["canonical_brand"])).strip().casefold()
            in literal_forms
        ]
        if len(exact) == 1:
            return exact[0]
        raise AmbiguousBrandError(query=key, candidates=tuple(sorted(canonical)))

    @staticmethod
    def _to_resolution(
        item: dict[str, Any],
        question: str = "",
        *,
        market_universe: tuple[tuple[str, str], ...] = (),
        matched_key: str | None = None,
        matched_literal: str = "",
    ) -> BrandResolution:
        molecule_en = tuple(str(value) for value in item.get("molecule_en", []))
        memberships = BrandResolver._item_memberships(item)
        market_ids = tuple(dict.fromkeys(market_id for market_id, _ in memberships))
        requested_market = BrandResolver._explicit_market(
            question,
            market_universe or memberships,
        )
        requested_market_id = (
            BrandResolver._explicit_market_token(question)
            or (requested_market[0] if requested_market is not None else None)
        )
        requested_market_name = requested_market[1] if requested_market is not None else None
        explicit_membership = (
            requested_market[0]
            if requested_market is not None and requested_market[0] in market_ids
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
            source_variance=bool(item.get("source_variance", False)),
            resolved_via_alias=BrandResolver._resolved_via_runtime_alias(
                item,
                matched_key=matched_key,
                matched_literal=matched_literal,
            ),
        )

    @staticmethod
    def _resolved_via_runtime_alias(
        item: dict[str, Any],
        *,
        matched_key: str | None,
        matched_literal: str,
    ) -> bool:
        if not matched_key or matched_key not in item.get("_runtime_alias_keys", ()):
            return False
        literal = unicodedata.normalize("NFKC", matched_literal).strip().casefold()
        literal_forms = {literal}
        for particle in _BRAND_PARTICLES:
            if literal.endswith(particle) and len(literal) > len(particle) + 1:
                literal_forms.add(literal[: -len(particle)].rstrip())
        canonical = (
            unicodedata.normalize("NFKC", str(item["canonical_brand"]))
            .strip()
            .casefold()
        )
        return canonical not in literal_forms

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
                if BrandResolver._equivalent_market_id(market_id, runtime_ids) and pair not in seen:
                    markets.append(pair)
                    seen.add(pair)
        return tuple(markets)

    @staticmethod
    def _equivalent_market_id(market_id: str, candidates: set[str]) -> bool:
        match = re.fullmatch(r"(?:ml|strategy)_(\d+)", market_id, re.IGNORECASE)
        if match is None:
            return market_id in candidates
        number = int(match.group(1))
        return any(
            (candidate_match := re.fullmatch(r"(?:ml|strategy)_(\d+)", candidate, re.IGNORECASE))
            and int(candidate_match.group(1)) == number
            for candidate in candidates
        )

    @staticmethod
    def _explicit_market(
        question: str,
        markets: tuple[tuple[str, str], ...],
    ) -> tuple[str, str] | None:
        requested_id = BrandResolver._explicit_market_token(question)
        if requested_id is not None:
            for market_id, market_name in markets:
                if BrandResolver._equivalent_market_id(requested_id, {market_id}):
                    return market_id, market_name
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
    def _explicit_market_token(question: str) -> str | None:
        match = re.search(
            r"(?<![A-Za-z0-9_])((?:ml|strategy)_\d+)(?![A-Za-z0-9_])",
            question,
            re.IGNORECASE,
        )
        return match.group(1).lower() if match is not None else None

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


_BRAND_PARTICLES = (
    "으로부터",
    "에게서",
    "한테서",
    "이라도",
    "이라면",
    "이랑",
    "부터",
    "까지",
    "처럼",
    "보다",
    "에게",
    "한테",
    "께서",
    "으로",
    "라고",
    "와",
    "과",
    "은",
    "는",
    "이",
    "가",
    "의",
    "을",
    "를",
    "랑",
    "로",
)
