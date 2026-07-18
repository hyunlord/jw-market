from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


REQUIRED_CD_BRANDS = ("악템라", "가드렛")
REQUIRED_GROUP_SCOPES = (
    ("group:livalo_family", "리바로"),
    ("group:gardlet_family", "가드렛"),
)
REQUIRED_GROUP_ATC4 = {
    "group:livalo_family": ("C10A1", "C10C0"),
    "group:gardlet_family": ("A10N1", "A10N3"),
}
GENERAL_ATC4_SCOPE = (
    "C10A1",
    "C10C0",
    "C10C1",
    "C10C2",
    "C10C3",
    "C10C4",
    "C10C5",
    "C10C6",
    "C10C7",
    "C10C8",
)


@dataclass(frozen=True, slots=True)
class RequiredScenario:
    identifier: str
    body: Mapping[str, Any]


def _body(
    *,
    source: str,
    measure: str,
    atc4: tuple[str, ...],
    analysis_level: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "atc4": list(atc4),
        "focus_brand_key": "리바로",
    }
    if analysis_level is not None:
        filters["analysis_level"] = dict(analysis_level)
    return {
        "filters": filters,
        "measure": measure,
        "options": {"period_range": {"start": "2021-06", "end": "2026-05"}},
        "source": source,
        "view": "general",
    }


def required_general_scenarios() -> tuple[RequiredScenario, ...]:
    scenarios = [
        RequiredScenario(
            identifier=f"required_general:atc4_{count}:ubist:sales",
            body=_body(
                source="ubist",
                measure="sales",
                atc4=GENERAL_ATC4_SCOPE[:count],
            ),
        )
        for count in (1, 2, 5, 10)
    ]
    scenarios.extend(
        (
            RequiredScenario(
                identifier="required_general:atc4_1:ubist:volume",
                body=_body(source="ubist", measure="volume", atc4=GENERAL_ATC4_SCOPE[:1]),
            ),
            RequiredScenario(
                identifier="required_general:atc4_1:iqvia:sales",
                body=_body(source="iqvia", measure="sales", atc4=GENERAL_ATC4_SCOPE[:1]),
            ),
            RequiredScenario(
                identifier="required_general:atc4_1:iqvia:unit",
                body=_body(source="iqvia", measure="unit", atc4=GENERAL_ATC4_SCOPE[:1]),
            ),
            RequiredScenario(
                identifier="required_general:iqvia:molecule_type:single:sales",
                body=_body(
                    source="iqvia",
                    measure="sales",
                    atc4=GENERAL_ATC4_SCOPE[:1],
                    analysis_level={"iqvia": {"molecule_type": ["SINGLE"]}},
                ),
            ),
            RequiredScenario(
                identifier="required_general:iqvia:molecule_desc:pitavastatin:sales",
                body=_body(
                    source="iqvia",
                    measure="sales",
                    atc4=GENERAL_ATC4_SCOPE[:1],
                    analysis_level={"iqvia": {"molecule_desc": ["PITAVASTATIN"]}},
                ),
            ),
        )
    )
    return tuple(scenarios)


def missing_required_case_contract(case_ids: set[str]) -> tuple[str, ...]:
    exact = {scenario.identifier for scenario in required_general_scenarios()}
    for option_id, member in REQUIRED_GROUP_SCOPES:
        exact.update(
            {
                f"brand_activity_group:topics:{option_id}:{member}",
                f"brand_activity_group:csd_timeseries:{option_id}:{member}",
                f"brand_activity_group:csd_activity:{option_id}:{member}",
                f"brand_activity_group:interest_rx:{option_id}:{member}",
            }
        )
    prefixes = {
        "brand_activity:presence:",
        "brand_activity:topics:",
        "brand_activity:csd_timeseries:",
        "brand_activity:csd_activity:",
        "brand_activity:interest_rx:",
    }
    for brand in REQUIRED_CD_BRANDS:
        prefixes.update(
            {
                f"dynamic:{brand}:strategic_cd:",
                f"deep:{brand}:strategic_cd:",
                f"cause:{brand}:competitive_dynamics:",
            }
        )
    missing = {f"exact:{identifier}" for identifier in exact - case_ids}
    missing.update(f"prefix:{prefix}" for prefix in prefixes if not any(item.startswith(prefix) for item in case_ids))
    return tuple(sorted(missing))
