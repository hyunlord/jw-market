from __future__ import annotations

from fastapi import HTTPException


VALID_VIEWS = {"market_landscape", "competitive_dynamics"}
VALID_SOURCES = {"UBIST", "IQVIA"}
VALID_MEASURES_BY_SOURCE = {
    "UBIST": {"sales", "volume"},
    "IQVIA": {"sales", "unit", "dosage_unit", "counting_unit"},
}
UNIT_LABELS = {
    ("UBIST", "sales"): "KRW",
    ("UBIST", "volume"): "Rx",
    ("IQVIA", "sales"): "KRW",
    ("IQVIA", "unit"): "Unit",
    ("IQVIA", "dosage_unit"): "Dosage Unit",
    ("IQVIA", "counting_unit"): "Counting Unit",
}


def validate_cause_query(view: str | None, source: str | None, measure: str | None) -> tuple[str, str, str]:
    view = view or "market_landscape"
    source = (source or "UBIST").upper()
    measure = measure or "sales"

    if view not in VALID_VIEWS:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_view", "view": view, "valid_values": sorted(VALID_VIEWS)},
        )
    if source not in VALID_SOURCES:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_source", "source": source, "valid_values": sorted(VALID_SOURCES)},
        )
    valid_measures = VALID_MEASURES_BY_SOURCE[source]
    if measure not in valid_measures:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_measure_for_source",
                "source": source,
                "measure": measure,
                "valid_measures": sorted(valid_measures),
            },
        )
    return view, source, measure
