from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

_DATA_DIR: Final = Path(__file__).with_name("data")
_CATALOG_PATH: Final = _DATA_DIR / "hira_disease_catalog.csv"
_METADATA_PATH: Final = _DATA_DIR / "hira_disease_catalog.meta.json"
_SUBCODE_TOKENS: Final = (
    "세분류",
    "4단",
    "상세 코드",
    "상세코드",
    "세부 코드",
    "세부코드",
    "하위 코드",
    "하위코드",
)

_EXPLICIT_SUBCODE_RE: Final = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z]\d{2}[._]?\d)(?![A-Za-z0-9])"
)
_DISEASE_NAME_CHAPTER_HINTS: Final = {"고혈압": "I"}

PopulationLayer = Literal["parent", "subcode"]


class HiraCatalogIntegrityError(RuntimeError):
    def __init__(self, *, reason: str) -> None:
        super().__init__(f"HIRA catalog integrity check failed: {reason}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class HiraCatalogMetadata:
    fetched_at: str
    total_count: int
    page_count: int
    response_sha256: tuple[str, ...]
    csv_sha256: str
    source: str
    sick_type: str

    @property
    def snapshot_date(self) -> str:
        return self.fetched_at[:10]


@dataclass(frozen=True, slots=True)
class HiraCatalogResolution:
    parent_codes: tuple[str, ...]
    child_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HiraPopulationSelection:
    layer: PopulationLayer
    codes: tuple[str, ...]
    resolution: HiraCatalogResolution
    metadata: HiraCatalogMetadata


@lru_cache(maxsize=1)
def catalog_metadata() -> HiraCatalogMetadata:
    with _METADATA_PATH.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    return HiraCatalogMetadata(
        fetched_at=str(payload["fetched_at"]),
        total_count=int(payload["total_count"]),
        page_count=int(payload["page_count"]),
        response_sha256=tuple(str(value) for value in payload["response_sha256"]),
        csv_sha256=str(payload["csv_sha256"]),
        source=str(payload["source"]),
        sick_type=str(payload["sick_type"]),
    )


@lru_cache(maxsize=1)
def _catalog_rows() -> tuple[tuple[str, str, str], ...]:
    metadata = catalog_metadata()
    digest = hashlib.sha256(_CATALOG_PATH.read_bytes()).hexdigest()
    if digest != metadata.csv_sha256:
        raise HiraCatalogIntegrityError(reason="csv_sha256_mismatch")
    with _CATALOG_PATH.open(encoding="utf-8", newline="") as stream:
        rows = tuple(
            (row[0].strip().upper(), row[1].strip(), row[2].strip())
            for row in csv.reader(stream)
            if row and row[0] != "sickCd"
        )
    if len(rows) != metadata.total_count:
        raise HiraCatalogIntegrityError(reason="row_count_mismatch")
    return rows


def _normalized_name(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", value.casefold())


def catalog_parent_codes_for_name(name: str) -> tuple[str, ...]:
    normalized = _normalized_name(name)
    if len(normalized) < 2:
        return ()
    hinted = next(
        (
            (disease_name, chapter)
            for disease_name, chapter in _DISEASE_NAME_CHAPTER_HINTS.items()
            if disease_name in normalized
        ),
        None,
    )
    needle, chapter = hinted or (normalized, None)
    return tuple(
        code
        for code, korean_name, _english_name in _catalog_rows()
        if len(code) == 3
        and needle in _normalized_name(korean_name)
        and (chapter is None or code.startswith(chapter))
    )


def catalog_resolution(parent_codes: tuple[str, ...]) -> HiraCatalogResolution:
    parents = tuple(dict.fromkeys(code.strip().upper() for code in parent_codes))
    parent_set = set(parents)
    available_parents = {
        code
        for code, _name, _english_name in _catalog_rows()
        if code in parent_set
    }
    resolved_parents = tuple(code for code in parents if code in available_parents)
    children = tuple(
        code
        for code, _name, _english_name in _catalog_rows()
        if len(code) == 4 and code[:3] in available_parents
    )
    return HiraCatalogResolution(
        parent_codes=resolved_parents,
        child_codes=children,
    )


def requested_population_layer(question: str) -> PopulationLayer:
    has_subcode_request = any(token in question for token in _SUBCODE_TOKENS)
    if has_subcode_request or _EXPLICIT_SUBCODE_RE.search(question) is not None:
        return "subcode"
    return "parent"


def select_catalog_population(
    question: str,
    parent_codes: tuple[str, ...],
) -> HiraPopulationSelection:
    resolution = catalog_resolution(parent_codes)
    layer = requested_population_layer(question)
    codes = (
        resolution.child_codes
        if layer == "subcode" and resolution.child_codes
        else resolution.parent_codes
    )
    return HiraPopulationSelection(
        layer=layer,
        codes=codes,
        resolution=resolution,
        metadata=catalog_metadata(),
    )
