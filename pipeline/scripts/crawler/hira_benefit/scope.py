from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BrandScopeEntry:
    brand_key: str
    brand_name: str
    atc4_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MoleculeScopeEntry:
    molecule_norm: str
    brand_key: str
    brand_name: str
    atc4_code: str


@dataclass(frozen=True, slots=True)
class BrandMatch:
    brand_key: str
    brand_name: str
    match_method: str
    confidence: str
    evidence_start: int
    evidence_end: int
    matched_text: str
    evidence_coordinate: str = "normalized_nfc_casefold_whitespace"
    molecule_norm: str | None = None
    atc4_code: str | None = None


def normalize_scope_text(value: str) -> str:
    """Return the canonical coordinate space used by scope evidence spans."""

    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _normalize(value: str) -> str:
    return normalize_scope_text(value)


def _is_word_character(value: str) -> bool:
    return value.isalnum()


def _occurrences(text: str, needle: str) -> Iterable[tuple[int, int]]:
    start = 0
    while True:
        start = text.find(needle, start)
        if start < 0:
            return
        yield start, start + len(needle)
        start += 1


def _product_context_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Locate data-shaped product-name regions without a dosage-form dictionary."""

    ranges: list[tuple[int, int]] = []
    for match in re.finditer(r"품명\s*[:：]?\s*", text):
        boundary = re.search(
            r"(?:\s분류\s+고시|\s관련근거\s|\s게시일\s)",
            text[match.end() :],
        )
        end = (
            match.end() + boundary.start()
            if boundary is not None
            else min(len(text), match.end() + 500)
        )
        ranges.append((match.end(), end))

    for opening, closing in (("(", ")"), ("‘", "’"), ("“", "”")):
        stack: list[int] = []
        for index, character in enumerate(text):
            if character == opening:
                stack.append(index)
            elif character == closing and stack:
                start = stack.pop()
                if index - start <= 200:
                    ranges.append((start + 1, index))
    return tuple(ranges)


def derive_dosage_form_suffixes(
    brands: Sequence[BrandScopeEntry],
    raw_texts: Sequence[str],
    *,
    minimum_distinct_brands: int = 3,
) -> frozenset[str]:
    """Derive recurring product continuations from HIRA product-name regions."""

    keys_by_compact_name: dict[str, set[str]] = defaultdict(set)
    for entry in brands:
        compact_name = _normalize(entry.brand_name).replace(" ", "")
        if compact_name:
            keys_by_compact_name[compact_name].add(entry.brand_key)

    keys_by_suffix: dict[str, set[str]] = defaultdict(set)
    for raw_text in raw_texts:
        text = _normalize(raw_text)
        for range_start, range_end in _product_context_ranges(text):
            for token in re.findall(r"[가-힣a-z0-9+.%/-]+", text[range_start:range_end]):
                for prefix_end in range(1, len(token)):
                    prefix = token[:prefix_end]
                    if prefix in keys_by_compact_name:
                        keys_by_suffix[token[prefix_end:]].update(
                            keys_by_compact_name[prefix]
                        )
    return frozenset(
        suffix
        for suffix, brand_keys in keys_by_suffix.items()
        if len(brand_keys) >= minimum_distinct_brands
    )


def _inside_any_range(
    ranges: Sequence[tuple[int, int]],
    start: int,
    end: int,
) -> bool:
    return any(range_start <= start < end <= range_end for range_start, range_end in ranges)


def _boundary_match_spans(
    text: str,
    needle: str,
    *,
    product_ranges: Sequence[tuple[int, int]],
    dosage_form_suffixes: frozenset[str],
) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    for start, end in _occurrences(text, needle):
        if start > 0 and _is_word_character(text[start - 1]):
            continue
        inside_product_context = _inside_any_range(product_ranges, start, end)
        if len(needle.replace(" ", "")) <= 2 and not inside_product_context:
            continue
        if end < len(text) and _is_word_character(text[end]):
            if not inside_product_context:
                continue
            if not any(
                text.startswith(suffix, end)
                and (
                    end + len(suffix) == len(text)
                    or not _is_word_character(text[end + len(suffix)])
                    or text[end + len(suffix)].isdigit()
                )
                for suffix in dosage_form_suffixes
            ):
                continue
        spans.append((start, end))
    return tuple(spans)


def _direct_brand_matches(
    raw_text: str,
    brands: Sequence[BrandScopeEntry],
    *,
    dosage_form_suffixes: frozenset[str],
) -> tuple[BrandMatch, ...]:
    text = _normalize(raw_text)
    product_ranges = _product_context_ranges(text)
    candidates: list[tuple[BrandScopeEntry, str, int, int]] = []
    normalized_entries = tuple(
        (entry, _normalize(entry.brand_name))
        for entry in brands
        if entry.brand_key.strip() and entry.brand_name.strip()
    )
    for entry, normalized_name in normalized_entries:
        for start, end in _boundary_match_spans(
            text,
            normalized_name,
            product_ranges=product_ranges,
            dosage_form_suffixes=dosage_form_suffixes,
        ):
            candidates.append((entry, normalized_name, start, end))

    longest_by_position: dict[int, int] = {}
    for _entry, normalized_name, start, _end in candidates:
        longest_by_position[start] = max(
            longest_by_position.get(start, 0),
            len(normalized_name),
        )

    by_key: dict[str, BrandMatch] = {}
    for entry, normalized_name, start, end in candidates:
        if len(normalized_name) < longest_by_position[start]:
            continue
        match = BrandMatch(
            brand_key=entry.brand_key,
            brand_name=entry.brand_name,
            match_method="exact_boundary_name",
            confidence="high",
            evidence_start=start,
            evidence_end=end,
            matched_text=text[start:end],
        )
        current = by_key.get(entry.brand_key)
        if current is None or (
            -len(_normalize(match.brand_name)),
            match.evidence_start,
            _normalize(match.brand_name),
        ) < (
            -len(_normalize(current.brand_name)),
            current.evidence_start,
            _normalize(current.brand_name),
        ):
            by_key[entry.brand_key] = match
    return tuple(
        sorted(
            by_key.values(),
            key=lambda match: (_normalize(match.brand_name), match.brand_key),
        )
    )


def _quantile(values: Sequence[int], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _upper_outlier_fence(values: Sequence[int]) -> float:
    q1 = _quantile(values, 0.25)
    q3 = _quantile(values, 0.75)
    return q3 + 3.0 * (q3 - q1)


def _strict_boundary_present(text: str, needle: str) -> bool:
    for start, end in _occurrences(text, needle):
        if start > 0 and _is_word_character(text[start - 1]):
            continue
        if end < len(text) and _is_word_character(text[end]):
            continue
        return True
    return False


def derive_non_specific_molecules(
    molecules: Sequence[MoleculeScopeEntry],
    raw_texts: Sequence[str],
) -> frozenset[str]:
    """Derive ambiguous high-frequency molecules from the observed distribution."""

    brand_keys: dict[str, set[str]] = defaultdict(set)
    atc4_codes: dict[str, set[str]] = defaultdict(set)
    for row in molecules:
        molecule = _normalize(row.molecule_norm)
        if not molecule:
            continue
        brand_keys[molecule].add(row.brand_key)
        if row.atc4_code:
            atc4_codes[molecule].add(row.atc4_code)

    normalized_texts = tuple(_normalize(text) for text in raw_texts)
    document_frequency = {
        molecule: sum(
            _strict_boundary_present(text, molecule) for text in normalized_texts
        )
        for molecule in brand_keys
    }
    brand_counts = [len(values) for values in brand_keys.values()]
    atc_counts = [len(atc4_codes[molecule]) for molecule in brand_keys]
    document_counts = list(document_frequency.values())
    brand_fence = _upper_outlier_fence(brand_counts)
    atc_fence = _upper_outlier_fence(atc_counts)
    document_fence = _upper_outlier_fence(document_counts)

    blocked = {
        molecule
        for molecule in brand_keys
        if len(atc4_codes[molecule]) > 1
        and sum(
            (
                len(brand_keys[molecule]) > brand_fence,
                len(atc4_codes[molecule]) > atc_fence,
                document_frequency[molecule] > document_fence,
            )
        )
        >= 2
    }
    blocked.update(
        molecule
        for molecule in brand_keys
        if not re.fullmatch(r"[a-z][a-z0-9 .+/-]{2,}", molecule)
    )
    return frozenset(blocked)


def _atc4_codes_in_text(text: str) -> frozenset[str]:
    return frozenset(
        value.upper()
        for value in re.findall(r"(?<![A-Za-z0-9])[A-Za-z]\d{2}[A-Za-z0-9]\d?(?![A-Za-z0-9])", text)
    )


def _molecule_matches(
    raw_text: str,
    brands: Sequence[BrandScopeEntry],
    molecules: Sequence[MoleculeScopeEntry],
    *,
    direct_matches: Sequence[BrandMatch],
    blocked_molecules: frozenset[str],
) -> tuple[BrandMatch, ...]:
    text = _normalize(raw_text)
    heading_end = text.find("분류 고시")
    heading = text if heading_end < 0 else text[:heading_end]
    brand_by_key = {entry.brand_key: entry for entry in brands}
    direct_keys = {match.brand_key for match in direct_matches}
    if direct_keys:
        return ()
    explicit_atc4 = _atc4_codes_in_text(text)
    rows_by_molecule: dict[str, list[MoleculeScopeEntry]] = defaultdict(list)
    for row in molecules:
        molecule = _normalize(row.molecule_norm)
        if molecule and molecule not in blocked_molecules:
            rows_by_molecule[molecule].append(row)

    by_key: dict[str, BrandMatch] = {}
    for molecule, rows in rows_by_molecule.items():
        span = next(
            (
                (start, end)
                for start, end in _occurrences(heading, molecule)
                if (start == 0 or not _is_word_character(heading[start - 1]))
                and (end == len(heading) or not _is_word_character(heading[end]))
            ),
            None,
        )
        if span is None:
            continue
        molecule_atc4 = {row.atc4_code for row in rows if row.atc4_code}
        context_atc4 = molecule_atc4 & explicit_atc4
        if not context_atc4:
            continue
        context_rows = tuple(row for row in rows if row.atc4_code in context_atc4)
        context_keys = {row.brand_key for row in context_rows}
        if len(context_keys) != 1:
            continue
        row = min(
            context_rows,
            key=lambda candidate: (
                candidate.brand_key,
                candidate.brand_name,
                candidate.atc4_code,
            ),
        )
        entry = brand_by_key.get(row.brand_key)
        if entry is None:
            continue
        start, end = span
        by_key[row.brand_key] = BrandMatch(
            brand_key=row.brand_key,
            brand_name=entry.brand_name,
            match_method="molecule_via_atc4",
            confidence="medium",
            evidence_start=start,
            evidence_end=end,
            matched_text=text[start:end],
            molecule_norm=molecule,
            atc4_code=row.atc4_code,
        )
    return tuple(
        sorted(
            by_key.values(),
            key=lambda match: (_normalize(match.brand_name), match.brand_key),
        )
    )


def match_brand_scope(
    raw_text: str,
    brands: Sequence[BrandScopeEntry],
    molecules: Sequence[MoleculeScopeEntry],
    *,
    blocked_molecules: frozenset[str] = frozenset(),
    dosage_form_suffixes: frozenset[str] = frozenset(),
) -> tuple[BrandMatch, ...]:
    direct = _direct_brand_matches(
        raw_text,
        brands,
        dosage_form_suffixes=dosage_form_suffixes,
    )
    molecule = _molecule_matches(
        raw_text,
        brands,
        molecules,
        direct_matches=direct,
        blocked_molecules=blocked_molecules,
    )
    matches_by_name: dict[str, list[BrandMatch]] = defaultdict(list)
    for match in (*direct, *molecule):
        matches_by_name[_normalize(match.brand_name)].append(match)
    unambiguous = (
        match
        for matches in matches_by_name.values()
        if len({match.brand_key for match in matches}) == 1
        for match in matches[:1]
    )
    return tuple(
        sorted(
            unambiguous,
            key=lambda match: (_normalize(match.brand_name), match.brand_key),
        )
    )


def brands_from_cache_payload(response_json: str) -> tuple[str, ...]:
    """Compatibility parser retained for historical receipt replay."""

    payload = json.loads(response_json)
    if not isinstance(payload, list):
        raise TypeError("cache_brands.response_json must be a list")
    result = [
        str(row.get("brand") or "").strip()
        for row in payload
        if isinstance(row, dict) and bool(row.get("is_jw"))
    ]
    return tuple(dict.fromkeys(name for name in result if name))


def match_brand_names(raw_text: str, brand_names: Sequence[str]) -> tuple[str, ...]:
    """Compatibility wrapper around the canonical boundary matcher."""

    entries = tuple(
        BrandScopeEntry(brand_key=_normalize(name).replace(" ", ""), brand_name=name)
        for name in brand_names
        if name.strip()
    )
    return tuple(
        match.brand_name
        for match in match_brand_scope(raw_text, entries, ())
    )
