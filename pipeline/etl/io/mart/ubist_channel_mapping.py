"""UBIST facility-specialty channel mapping helpers.

Phase 39.5 moves UBIST channel displays from facility-only buckets to a
single facility-specialty token such as ``GH Endo`` -> ``종합병원 내분비``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class UbistChannel:
    code: str
    facility_abbr: str
    specialty_abbr: str
    facility_kor: str
    specialty_kor: str
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


UBIST_FACILITY_MAPPING: dict[str, dict[str, Any]] = {
    # PL label: GH = 종합병원. Raw UBIST splits this into tertiary/general
    # hospital labels, so the channel sums all three hospital buckets.
    "GH": {
        "korean": "종합병원",
        "raw_values": ("상급종합병원", "종합병원", "병원"),
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

UBIST_SPECIALTY_MAPPING: dict[str, dict[str, Any]] = {
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
        # 무엇: PL 결정에 따라 target customer specialty의 IGF는
        # 가정의학과/일반의에 내과 세부 10개를 더한 12개 raw specialty를
        # 포함한다. 왜: standalone 내과(IM)는 중복 원천이라 제거하지만,
        # 화면 breakdown에서는 세부 내과가 IGF에도, 세부 atomic 채널에도
        # 함께 보일 수 있어야 한다. 도메인 근거: headline 시장총합/MS/순위/HHI는
        # raw brand history로 계산되고 specialty breakdown 합을 분모로 쓰지 않는다.
        # 기각 대안: 별도 IM 채널을 유지하면 PL이 제거하라고 한 standalone 내과
        # 슬롯이 다시 노출된다. IGF를 FM/GP 2개로만 두면 세부 내과 포함 요청을
        # 만족하지 못한다.
        "raw_values": ("가정의학과(FM)", "일반의(GP)", *INTERNAL_MEDICINE_DETAIL_SPECIALTIES),
    },
    # Existing catalog target slots still contain these UBIST specialties.
    "Neuro": {
        "korean": "신경",
        "raw_values": ("신경과(NR)",),
    },
    "Uro": {
        "korean": "비뇨의학",
        "raw_values": ("비뇨의학과(URO)",),
    },
}


def parse_channel_code(code: str | None) -> UbistChannel | None:
    if code is None:
        return None
    text = str(code).strip()
    if not text or text.lower() == "nan":
        return None
    parts = text.split()
    if len(parts) != 2:
        raise ValueError(f"Invalid UBIST channel code: {code!r}")
    facility_abbr, specialty_abbr = parts
    facility = UBIST_FACILITY_MAPPING.get(facility_abbr)
    specialty = UBIST_SPECIALTY_MAPPING.get(specialty_abbr)
    if facility is None or specialty is None:
        raise ValueError(f"Unknown UBIST channel abbreviation: {code!r}")
    return UbistChannel(
        code=f"{facility_abbr} {specialty_abbr}",
        facility_abbr=facility_abbr,
        specialty_abbr=specialty_abbr,
        facility_kor=str(facility["korean"]),
        specialty_kor=str(specialty["korean"]),
        display_name=f"{facility['korean']} {specialty['korean']}",
        facility_raw_values=tuple(facility["raw_values"]),
        specialty_raw_values=tuple(specialty["raw_values"]),
    )


def raw_pair_to_channel_code(facility_raw: Any, specialty_raw: Any) -> str | None:
    facility_text = str(facility_raw or "").strip()
    specialty_text = str(specialty_raw or "").strip()
    if specialty_text == STANDALONE_INTERNAL_MEDICINE_SPECIALTY:
        return None
    facility_abbr = next(
        (
            abbr
            for abbr, meta in UBIST_FACILITY_MAPPING.items()
            if facility_text in set(meta["raw_values"])
        ),
        None,
    )
    specialty_abbr = next(
        (
            abbr
            for abbr, meta in UBIST_SPECIALTY_MAPPING.items()
            if specialty_text in set(meta["raw_values"])
        ),
        None,
    )
    if not facility_abbr or not specialty_abbr:
        return None
    return f"{facility_abbr} {specialty_abbr}"
