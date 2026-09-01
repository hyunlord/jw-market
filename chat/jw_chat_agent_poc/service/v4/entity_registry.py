from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jw_chat_agent_poc.hira_catalog import catalog_metadata


@dataclass(frozen=True, slots=True)
class DiseaseEntity:
    entity_id: str
    canonical_name: str
    condition: str
    aliases: tuple[str, ...]
    kcd_prefixes: tuple[str, ...]
    treatments: tuple[str, ...]
    mart_axes: tuple[str, ...]
    source: str
    fetched_at: str
    expansion_grade: str = "composition_component"


_ASH_ITP_SOURCE = (
    "American Society of Hematology 2019 guidelines for immune thrombocytopenia; "
    "Blood Advances 3(23):3829-3866"
)


def _entities() -> tuple[DiseaseEntity, ...]:
    metadata = catalog_metadata()
    return (
        DiseaseEntity(
            entity_id="disease:immune_thrombocytopenia",
            canonical_name="면역혈소판감소증",
            condition="immune thrombocytopenia",
            aliases=(
                "면역혈소판감소증",
                "면역 혈소판 감소증",
                "특발성혈소판감소성자반증",
                "itp",
                "d69.3",
                "d693",
                "d69_3",
            ),
            kcd_prefixes=("D69.3",),
            treatments=("prednisone", "dexamethasone", "eltrombopag", "romiplostim"),
            mart_axes=(),
            source=f"{metadata.source}; {_ASH_ITP_SOURCE}",
            fetched_at=metadata.fetched_at,
        ),
        DiseaseEntity(
            entity_id="disease:hypertension",
            canonical_name="고혈압",
            condition="hypertension",
            aliases=("고혈압", "hypertension", "i10", "i11", "i12", "i13", "i15"),
            kcd_prefixes=("I10", "I11", "I12", "I13", "I15"),
            treatments=(),
            mart_axes=(),
            source=metadata.source,
            fetched_at=metadata.fetched_at,
            expansion_grade="notation_variant",
        ),
    )


def resolve_disease_entity(text: str) -> DiseaseEntity | None:
    normalized = _key(text)
    for entity in _entities():
        if any(_contains_alias(normalized, alias) for alias in entity.aliases):
            return entity
    return None


def registry_snapshot() -> tuple[DiseaseEntity, ...]:
    """Expose the read-only registry view without migrating source dictionaries."""
    return _entities()


def brand_molecule_records(reader: Any | None) -> tuple[Mapping[str, Any], ...]:
    """Read the existing mart brand-molecule dictionary without copying it."""
    if reader is None:
        return ()
    return tuple(reader.brand_molecules())


def _key(value: str) -> str:
    return re.sub(r"[\s._-]+", "", value).casefold()


def _contains_alias(normalized_text: str, alias: str) -> bool:
    return _key(alias) in normalized_text
