from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from pipeline.etl.io.ubist_specialties import (
    aggregate_specialty_labels,
    detail_specialty_labels,
)
OTHERS_SPECIALTY = "Others(병원,보건기관, 그 외 요양기관)"
FACILITY_ONLY_CATCH_ALL_FACILITIES: Final[frozenset[str]] = frozenset({"Semi", "OT"})


class TargetChannelCodeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TargetUbistChannel:
    code: str
    facility_abbr: str
    specialty_abbr: str
    facility_kor: str
    specialty_kor: str
    series_name: str
    display_name: str
    facility_raw_values: tuple[str, ...]
    specialty_raw_values: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "facility_abbr": self.facility_abbr,
            "specialty_abbr": self.specialty_abbr,
            "facility_kor": self.facility_kor,
            "specialty_kor": self.specialty_kor,
            "display_name": self.display_name,
            "facility_raw_values": list(self.facility_raw_values),
            "specialty_raw_values": list(self.specialty_raw_values),
        }


TARGET_FACILITY_MAPPING: dict[str, dict[str, Any]] = {
    "TGH": {
        "korean": "주요고객 종합병원",
        "raw_values": ("상급종합병원", "종합병원"),
    },
    "Semi": {
        "korean": "병원",
        "raw_values": ("병원",),
    },
    "CL": {
        "korean": "의원",
        "raw_values": ("의원",),
    },
    "OT": {
        "korean": "기타",
        "raw_values": ("보건소", "기타(치과의원, 치과병원 등)"),
    },
}
# Target path only: legacy catalog GH slots are read as TGH without changing
# the global UBIST GH parser used by G/raw analysis paths.
LEGACY_TARGET_FACILITY_ALIASES = {"GH": "TGH"}

TARGET_SPECIALTY_MAPPING: dict[str, dict[str, Any]] = {
    "GI": {
        "korean": "소화기",
        "raw_values": ("소화기(Gastroenterology IM)",),
    },
    "Endo": {
        "korean": "내분비",
        "raw_values": ("내분비(Endocrinology IM)",),
    },
    "Cardio": {
        "korean": "순환기",
        "raw_values": ("순환기(Cardiology IM)",),
    },
    "Nephro": {
        "korean": "신장",
        "raw_values": ("신장(Nephrology IM)",),
    },
    "IGF": {
        "korean": "IGF",
        "raw_values": (
            "가정의학과(FM)",
            "일반의(GP)",
            *detail_specialty_labels(),
        ),
    },
    "Neuro": {
        "korean": "신경",
        "raw_values": ("신경과(NR)",),
    },
    "Uro": {
        "korean": "비뇨의학",
        "raw_values": ("비뇨의학과(URO)",),
    },
    "Hemato": {
        "korean": "혈액종양",
        "raw_values": ("혈액종양(Hemoto Oncology IM)",),
    },
    "OS": {
        "korean": "정형",
        "raw_values": ("정형외과(OS)",),
    },
    "PED": {
        "korean": "소아청소년",
        "raw_values": ("소아청소년과(PED)",),
    },
    "Others": {
        "korean": "Others",
        "raw_values": (OTHERS_SPECIALTY,),
    },
}


def canonical_target_facility_abbr(facility_abbr: str) -> str:
    text = str(facility_abbr).strip()
    return LEGACY_TARGET_FACILITY_ALIASES.get(text, text)


def target_facility_abbr_for_raw(facility_raw: Any) -> str | None:
    text = str(facility_raw or "").strip()
    return next(
        (
            abbr
            for abbr, meta in TARGET_FACILITY_MAPPING.items()
            if text in set(meta["raw_values"])
        ),
        None,
    )


def target_specialty_abbr_for_raw(specialty_raw: Any) -> str | None:
    text = str(specialty_raw or "").strip()
    if text in aggregate_specialty_labels():
        return None
    return next(
        (
            abbr
            for abbr, meta in TARGET_SPECIALTY_MAPPING.items()
            if text in set(meta["raw_values"])
        ),
        None,
    )


def target_display_name(
    facility_abbr: str,
    facility_kor: str,
    specialty_abbr: str,
    specialty_kor: str,
) -> str:
    if (
        specialty_abbr == "Others"
        and facility_abbr in FACILITY_ONLY_CATCH_ALL_FACILITIES
    ):
        return facility_kor
    return f"{facility_kor} {specialty_kor}"


def parse_target_channel_code(code: str | None) -> TargetUbistChannel | None:
    if code is None:
        return None
    text = str(code).strip()
    if not text or text.lower() == "nan":
        return None
    parts = text.split()
    if len(parts) != 2:
        raise TargetChannelCodeError(f"Invalid target UBIST channel code: {code!r}")
    facility_abbr = canonical_target_facility_abbr(parts[0])
    specialty_abbr = parts[1]
    facility = TARGET_FACILITY_MAPPING.get(facility_abbr)
    specialty = TARGET_SPECIALTY_MAPPING.get(specialty_abbr)
    if facility is None or specialty is None:
        raise TargetChannelCodeError(f"Unknown target UBIST channel abbreviation: {code!r}")
    facility_kor = str(facility["korean"])
    specialty_kor = str(specialty["korean"])
    series_name = f"{facility_kor} {specialty_kor}"
    return TargetUbistChannel(
        code=f"{facility_abbr} {specialty_abbr}",
        facility_abbr=facility_abbr,
        specialty_abbr=specialty_abbr,
        facility_kor=facility_kor,
        specialty_kor=specialty_kor,
        series_name=series_name,
        display_name=target_display_name(
            facility_abbr=facility_abbr,
            facility_kor=facility_kor,
            specialty_abbr=specialty_abbr,
            specialty_kor=specialty_kor,
        ),
        facility_raw_values=tuple(facility["raw_values"]),
        specialty_raw_values=tuple(specialty["raw_values"]),
    )


def raw_pair_to_target_channel_code(facility_raw: Any, specialty_raw: Any) -> str | None:
    facility_abbr = target_facility_abbr_for_raw(facility_raw)
    specialty_abbr = target_specialty_abbr_for_raw(specialty_raw)
    if not facility_abbr or not specialty_abbr:
        return None
    return f"{facility_abbr} {specialty_abbr}"


def translate_target_channel_to_raw_labels(target_label: str | None) -> list[str]:
    parsed = parse_target_channel_code(target_label)
    if parsed is None:
        return []
    return [
        f"{facility_raw} {specialty_raw}"
        for facility_raw in parsed.facility_raw_values
        for specialty_raw in parsed.specialty_raw_values
    ]
