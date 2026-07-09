from __future__ import annotations

from typing import Any

from .profile_provider import MoleculeRow, build_profile
from .repository import metric_rows_from_general
from .source_loader import Agent3Source
from .strength_candidate_extractor import CandidateFloors, extract_strength_candidates


SOURCE_DB_VALUES: dict[Agent3Source, str] = {
    "iqvia": "iqvia_nsa",
    "ubist": "ubist",
}

SOURCE_ORDER: tuple[Agent3Source, ...] = ("iqvia", "ubist")


def source_db_value(source: Agent3Source) -> str:
    return SOURCE_DB_VALUES[source]


def available_sources_from_general_rows(rows: list[dict[str, Any]]) -> tuple[Agent3Source, ...]:
    available_db_sources = {
        str(row.get("source") or "").lower()
        for row in rows
        if str(row.get("measure") or "").lower() == "sales"
    }
    return tuple(source for source in SOURCE_ORDER if source_db_value(source) in available_db_sources)


def filter_rows_for_source(rows: list[dict[str, Any]], source: Agent3Source) -> list[dict[str, Any]]:
    db_source = source_db_value(source)
    return [row for row in rows if str(row.get("source") or "").lower() == db_source]


def filter_molecule_rows_for_source(rows: list[MoleculeRow], source: Agent3Source) -> list[MoleculeRow]:
    db_source = source_db_value(source)
    return [row for row in rows if row.mart_source.lower() == db_source]


def build_source_profile(
    *,
    brand_name: str,
    source: Agent3Source,
    general_rows: list[dict[str, Any]],
    strategic_rows: list[dict[str, Any]],
    molecule_rows: list[MoleculeRow],
) -> dict[str, Any]:
    return build_profile(
        brand_name=brand_name,
        general_rows=filter_rows_for_source(general_rows, source),
        strategic_rows=filter_rows_for_source(strategic_rows, source),
        molecule_rows=filter_molecule_rows_for_source(molecule_rows, source),
    )


def extract_source_candidates(
    *,
    source: Agent3Source,
    general_rows: list[dict[str, Any]],
    top_n: int,
) -> list[dict[str, Any]]:
    return extract_strength_candidates(
        metric_rows_from_general(filter_rows_for_source(general_rows, source)),
        floors=CandidateFloors(),
        top_n=top_n,
    )


def profile_only_source_summary(
    *,
    brand: str,
    profile: dict[str, Any],
    candidates: list[dict[str, Any]],
    source: Agent3Source,
) -> dict[str, Any]:
    return {
        "brand": brand,
        "source": source,
        "profile_display": profile,
        "strength_items": [],
        "limitations": [f"{source} strength candidate 0건: wf316 호출 없이 source profile-only 저장"],
        "candidate_count": len(candidates),
    }
