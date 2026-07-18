from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from pipeline.scripts.utils.ubist_target_channel_mapping import (
    target_facility_abbr_for_raw,
    target_specialty_abbr_for_raw,
)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clean(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def normalize_text(value: Any) -> str:
    text = clean(value)
    if text is None:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", "", text)
    return text.upper()


def normalize_manufacturer(value: Any) -> str:
    normalized = normalize_text(value)
    aliases = {
        normalize_text("제이더블유중외제약"): normalize_text("JW중외제약"),
    }
    return aliases.get(normalized, normalized)


def read_required_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"required CSV not found: {path}")
    return pd.read_csv(path)


def source_file_version_from_skeleton(skeleton: pd.DataFrame) -> str:
    versions = {
        unicodedata.normalize("NFC", str(value))
        for value in skeleton["source_file_version"].dropna().unique().tolist()
    }
    allowed = {
        unicodedata.normalize("NFC", EXPECTED_SOURCE_FILE_VERSION),
        unicodedata.normalize("NFC", LEGACY_SKELETON_SOURCE_FILE_VERSION),
    }
    if not versions or not versions.issubset(allowed):
        raise ValueError(
            f"source_file_version mismatch: expected one of {sorted(allowed)!r}, "
            f"actual={sorted(versions)}"
        )
    return EXPECTED_SOURCE_FILE_VERSION


def spec_by_cd_id() -> dict[str, dict[str, Any]]:
    return {str(spec["competitive_dynamics_id"]): spec for spec in CD_SPECS}


def ubist_customer_label(channel: Any, specialty: Any) -> str:
    channel_text = clean(channel) or ""
    specialty_text = clean(specialty) or ""
    prefix = target_facility_abbr_for_raw(channel_text) or "OT"

    mapped_suffix = target_specialty_abbr_for_raw(specialty_text)
    if mapped_suffix:
        suffix = mapped_suffix
    elif "순환기" in specialty_text:
        suffix = "Cardio"
    elif "내분비" in specialty_text:
        suffix = "Endo"
    elif "신경과" in specialty_text:
        suffix = "Neuro"
    elif "비뇨" in specialty_text:
        suffix = "Uro"
    elif "소화기" in specialty_text:
        suffix = "GI"
    elif "신장" in specialty_text:
        suffix = "Nephro"
    elif "혈액종양" in specialty_text:
        suffix = "Hemato"
    elif "일반의" in specialty_text or "가정의학" in specialty_text:
        suffix = "IGF"
    elif "정형" in specialty_text:
        suffix = "OS"
    elif "소아" in specialty_text:
        suffix = "PED"
    elif "Others" in specialty_text or "unknown" in specialty_text or not specialty_text:
        suffix = "Others"
    else:
        suffix = re.sub(r"\(.+?\)", "", specialty_text).strip()[:20] or "Others"
    return f"{prefix} {suffix}"


def iqvia_customer_label(audit_code: Any) -> str:
    audit = clean(audit_code) or ""
    if audit.startswith("KCPA"):
        return "KCPA"
    if audit.startswith("KHPA"):
        return "KHPA"
    if audit.startswith("KPA"):
        return "KPA"
    return audit or "IQVIA/OTHER"


def customer_compare_key(value: Any) -> str:
    text = clean(value)
    if text is None:
        return ""
    text = unicodedata.normalize("NFC", text).strip()
    upper_text = text.upper()
    for prefix in ("IQVIA/", "UBIST/"):
        if upper_text.startswith(prefix):
            return upper_text[len(prefix):]
    return upper_text
