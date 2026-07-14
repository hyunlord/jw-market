"""Shared raw UBIST specialty hierarchy definitions."""

from __future__ import annotations


STANDALONE_INTERNAL_MEDICINE_SPECIALTY = "내과(IM)"
INTERNAL_MEDICINE_DETAIL_SPECIALTIES = (
    "알레르기(Allergy IM)",
    "내분비(Endocrinology IM)",
    "순환기(Cardiology IM)",
    "신장(Nephrology IM)",
    "류마티스(Rheumatology IM)",
    "소화기(Gastroenterology IM)",
    "감염(Infection Disease IM)",
    "혈액종양(Hemoto Oncology IM)",
    "호흡기(Pulmonology IM)",
    "분리되지 않은 내과",
)

UBIST_SPECIALTY_HIERARCHIES = {
    STANDALONE_INTERNAL_MEDICINE_SPECIALTY: INTERNAL_MEDICINE_DETAIL_SPECIALTIES,
}


def aggregate_specialty_labels() -> frozenset[str]:
    """Return raw specialty labels that duplicate their catalogued children."""
    return frozenset(UBIST_SPECIALTY_HIERARCHIES)
