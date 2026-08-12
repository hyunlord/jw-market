from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Final, TypedDict, cast

from pipeline.scripts.etl.brand_activity.km_core import normalize_spaces, normalize_text


class SemanticEventFields(TypedDict):
    """Meaning-bearing Keyword fields, excluding workbook provenance."""

    period_ym: str
    visit_location: str
    specialty: str
    representing_company: str
    product_name: str
    therapeutic_class: str
    keyword_text: str
    interest: str
    prescription_frequency: str
    prescription_evolution: str
    abstract_lit: str
    patient_lit: str
    promotional_lit: str
    samples_left: str
    other_materials_left: str
    what_other_materials: str
    other_comments: str


SEMANTIC_FIELD_NAMES: Final[tuple[str, ...]] = (
    "period_ym",
    "visit_location",
    "specialty",
    "representing_company",
    "product_name",
    "therapeutic_class",
    "keyword_text",
    "interest",
    "prescription_frequency",
    "prescription_evolution",
    "abstract_lit",
    "patient_lit",
    "promotional_lit",
    "samples_left",
    "other_materials_left",
    "what_other_materials",
    "other_comments",
)


def canonical_semantic_json_v1(fields: Mapping[str, object]) -> str:
    """Serialize the frozen v1 semantic contract independently of mapping order."""
    missing = tuple(name for name in SEMANTIC_FIELD_NAMES if name not in fields)
    unexpected = tuple(sorted(set(fields) - set(SEMANTIC_FIELD_NAMES)))
    if missing or unexpected:
        raise ValueError(f"semantic_event_key_v1 fields mismatch: missing={missing}, unexpected={unexpected}")
    normalized = {
        name: normalize_spaces(normalize_text(cast(str | int | float | bool | None, fields[name])))
        for name in SEMANTIC_FIELD_NAMES
    }
    period_ym = normalized["period_ym"]
    if len(period_ym) != 7 or period_ym[4] != "-" or not period_ym[:4].isdigit() or not period_ym[5:].isdigit():
        raise ValueError(f"semantic_event_key_v1 period_ym must be YYYY-MM: {period_ym!r}")
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def semantic_event_key_v1(fields: Mapping[str, object]) -> str:
    """Hash the canonical semantic payload without provenance fields."""
    payload = canonical_semantic_json_v1(fields)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StageOccurrenceInput:
    stage_row_id: int
    semantic_fields: SemanticEventFields
    stage_row_sha256: str
    source_file: str
    source_sheet: str
    source_row_no: int
    source_file_sha256: str

    @property
    def semantic_event_key(self) -> str:
        return semantic_event_key_v1(self.semantic_fields)


@dataclass(frozen=True, slots=True)
class BridgeRow:
    stage_row_id: int
    semantic_event_key_v1: str
    stage_row_sha256: str
    source_file: str
    source_sheet: str
    source_row_no: int
    source_file_sha256: str


def bridge_rows(occurrences: Iterable[StageOccurrenceInput]) -> tuple[BridgeRow, ...]:
    """Map every occurrence without deduplicating equal semantic keys."""
    return tuple(
        BridgeRow(
            stage_row_id=row.stage_row_id,
            semantic_event_key_v1=row.semantic_event_key,
            stage_row_sha256=row.stage_row_sha256,
            source_file=row.source_file,
            source_sheet=row.source_sheet,
            source_row_no=row.source_row_no,
            source_file_sha256=row.source_file_sha256,
        )
        for row in occurrences
    )


def stage_generation_id(rows: Iterable[tuple[int, str]]) -> str:
    """Fingerprint an exact stage snapshot streamed in strictly increasing id order."""
    digest = hashlib.sha256()
    previous_id = 0
    for row_id, row_sha256 in rows:
        if row_id <= previous_id:
            raise ValueError("stage generation rows must be ordered by strictly increasing id")
        if len(row_sha256) != 64:
            raise ValueError(f"invalid stage_row_sha256 for row_id={row_id}")
        digest.update(f"{row_id}|{row_sha256}\n".encode("ascii"))
        previous_id = row_id
    return digest.hexdigest()

