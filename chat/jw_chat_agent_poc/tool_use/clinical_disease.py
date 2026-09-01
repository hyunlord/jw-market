from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final


@dataclass(frozen=True, slots=True)
class ClinicalDisease:
    query: str
    clinicaltrials_condition: str


_CLINICAL_DISEASES: Final[tuple[tuple[str, ClinicalDisease], ...]] = (
    ("고지혈증", ClinicalDisease("고지혈증", "hyperlipidemia")),
    ("뇌경색", ClinicalDisease("뇌경색", "cerebral infarction")),
    ("당뇨황반부종", ClinicalDisease("당뇨황반부종", "diabetic macular edema")),
    ("당뇨병성황반부종", ClinicalDisease("당뇨병성 황반부종", "diabetic macular edema")),
    ("dme", ClinicalDisease("당뇨황반부종", "diabetic macular edema")),
)


def clinical_disease_for_text(text: str) -> ClinicalDisease | None:
    compact = re.sub(r"\s+", "", text.casefold())
    return next(
        (disease for token, disease in _CLINICAL_DISEASES if token in compact),
        None,
    )


def clinical_disease_for_query(query: str) -> ClinicalDisease | None:
    normalized = re.sub(r"\s+", "", query.casefold())
    return next(
        (
            disease
            for _token, disease in _CLINICAL_DISEASES
            if normalized
            in {
                re.sub(r"\s+", "", disease.query.casefold()),
                disease.clinicaltrials_condition.casefold(),
            }
        ),
        None,
    )


def clinicaltrials_condition_query(query: str) -> str:
    disease = clinical_disease_for_query(query)
    return disease.clinicaltrials_condition if disease is not None else query
