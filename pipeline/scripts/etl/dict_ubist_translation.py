"""UBIST raw Korean labels and MI-team shorthand translation helpers.

The dictionaries are a reference for catalog labels such as ``GH Cardio``.
They are not used to normalize raw UBIST labels for storage. Mart JSON keeps
raw Korean channel/specialty labels whenever raw data provides them.
"""

from __future__ import annotations

try:
    from pipeline.scripts.utils.ubist_channel_mapping import INTERNAL_MEDICINE_DETAIL_SPECIALTIES
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from utils.ubist_channel_mapping import INTERNAL_MEDICINE_DETAIL_SPECIALTIES


CHANNEL_CODE_TO_RAW = {
    "TH": "상급종합병원",
    "GH": "종합병원",
    "Semi": "병원",
    "CL": "의원",
}

CHANNEL_RAW_TO_CODE = {v: k for k, v in CHANNEL_CODE_TO_RAW.items()}
CHANNEL_RAW_TO_CODE.update(
    {
        "보건소": "기타",
        "기타": "기타",
        "기타(치과의원, 치과병원 등)": "기타",
    }
)

SPECIALTY_CODE_TO_RAW = {
    # 무엇: MI target code ``IGF``를 FM/GP 2개에서 FM/GP+내과 세부10
    # 12개로 확장한다. 왜: PL 결정상 standalone 내과(IM)는 중복 원천이라
    # 버리되, target_customer breakdown의 IGF에는 세부 내과가 포함되어야 한다.
    # 도메인 근거: 이 번역 테이블은 target_ubist_* code를 raw specialty 후보로
    # 풀 때만 쓰이며 headline 시장총합/MS/순위/HHI 계산식에는 들어가지 않는다.
    # 기각 대안: 별도 ``IM`` target code를 유지하면 화면에 독립 내과 채널이
    # 되살아나 PL 결정과 충돌한다.
    "IGF": ["가정의학과(FM)", "일반의(GP)", *INTERNAL_MEDICINE_DETAIL_SPECIALTIES],
    "Cardio": ["순환기(Cardiology IM)"],
    "GI": ["소화기(Gastroenterology IM)"],
    "Endo": ["내분비(Endocrinology IM)"],
    "Nephro": ["신장(Nephrology IM)"],
    "Neuro": ["신경과(NR)"],
    "Uro": ["비뇨의학과(URO)"],
}


def translate_target_ubist(target_label: str | None) -> list[str]:
    """Translate catalog ``target_ubist`` shorthand to raw Korean labels."""

    if not target_label or " " not in str(target_label).strip():
        return []
    channel_code, specialty_code = str(target_label).strip().split(" ", 1)
    channel_raw = CHANNEL_CODE_TO_RAW.get(channel_code)
    specialty_raws = SPECIALTY_CODE_TO_RAW.get(specialty_code, [])
    if not channel_raw or not specialty_raws:
        return []
    return [f"{channel_raw} {specialty}" for specialty in specialty_raws]


def reverse_translate_raw(channel_raw: str | None, specialty_raw: str | None) -> str | None:
    """Translate raw Korean ``channel + specialty`` to MI shorthand if known."""

    if not channel_raw or not specialty_raw:
        return None
    channel_code = CHANNEL_RAW_TO_CODE.get(str(channel_raw).strip())
    if not channel_code:
        return None
    specialty_code = None
    for code, raws in SPECIALTY_CODE_TO_RAW.items():
        if str(specialty_raw).strip() in raws:
            specialty_code = code
            break
    if not specialty_code:
        return None
    return f"{channel_code} {specialty_code}"
