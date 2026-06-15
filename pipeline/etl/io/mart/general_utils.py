from __future__ import annotations

import math
import re
from typing import Any

import pandas as pd

from .brand_key_normalize import normalize_brand_name
from .dict_ubist_translation import CHANNEL_CODE_TO_RAW, SPECIALTY_CODE_TO_RAW
from .ubist_channel_mapping import STANDALONE_INTERNAL_MEDICINE_SPECIALTY

def safe_float(value: Any) -> float:
    try:
        if value is None or pd.isna(value):
            return 0.0
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if math.isnan(number) or math.isinf(number):
                return 0.0
            return number
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number

def extract_atc4(value: Any) -> tuple[str, str | None]:
    if value is None or pd.isna(value):
        return "UNKNOWN", None
    text = str(value).strip()
    if not text:
        return "UNKNOWN", None
    match = re.search(r"\[?([A-Z][0-9A-Z]{2,5})\]?", text.upper())
    code = match.group(1) if match else text.split("_", 1)[0].split()[0].strip("[]").upper()
    return code or "UNKNOWN", text

def normalise_iqvia_channel(audit_code: Any) -> str | None:
    text = str(audit_code or "").upper()
    if text.startswith("KHPA"):
        return "KHPA"
    if text.startswith("KCPA"):
        return "KCPA"
    if text.startswith("KPA"):
        return "KPA"
    return None

def ubist_channel_to_raw(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "분리되지 않은 종별"
    return CHANNEL_CODE_TO_RAW.get(text, text if any("\uac00" <= ch <= "\ud7a3" for ch in text) else "분리되지 않은 종별")

def ubist_specialty_to_raw(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "분리되지 않은 진료과"
    if any("\uac00" <= ch <= "\ud7a3" for ch in text):
        return text
    for code, raws in SPECIALTY_CODE_TO_RAW.items():
        if text == code:
            return raws[0]
    return "분리되지 않은 진료과"

def deduplicate_ubist_internal_medicine_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop standalone 내과(IM) rows before UBIST aggregation.

    무엇: UBIST raw의 standalone ``내과(IM)`` 행만 제거하고 세부 10개 내과
    specialty는 보존한다.
    왜: PL 검증에서 standalone 내과가 세부 10개 합과 등가라 같이 더하면
    시장 총합이 약 40% 과대 집계된다.
    도메인 근거: 내과 표시는 세부 10개 합으로 재구성하고, standalone은
    중복 원천 행이다.
    기각 대안: cache 화면에서만 감추면 mart 시장 총합/MS/HHI 과대가 남는다.
    """
    if frame.empty or "specialty" not in frame.columns:
        return frame
    mask = frame["specialty"].astype(str).str.strip() == STANDALONE_INTERNAL_MEDICINE_SPECIALTY
    if not mask.any():
        return frame
    return frame.loc[~mask].copy()

def normalize_period_label(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    match = re.match(r"^(\d{4})Q([1-4])$", text)
    if match:
        return f"{match.group(1)}-Q{match.group(2)}"
    return text

def iqvia_source_priority(source_file: Any) -> int:
    """Prefer the newest overlapping NSA extract for duplicated period rows."""
    text = str(source_file or "")
    match = re.search(r"(20\d{2})\s*(?:_| )?([1-4])Q", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"([1-4])Q\s*(20\d{2})", text, flags=re.IGNORECASE)
        if match:
            quarter, year = int(match.group(1)), int(match.group(2))
            return year * 10 + quarter
        return 0
    year, quarter = int(match.group(1)), int(match.group(2))
    return year * 10 + quarter
