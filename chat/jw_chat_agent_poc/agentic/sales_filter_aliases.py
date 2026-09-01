from __future__ import annotations

from collections.abc import Iterable
from typing import Final


SOURCE_ALIASES: Final[dict[str, str]] = {
    "IQVIA": "IQVIA",
    "아이큐비아": "IQVIA",
    "UBIST": "UBIST",
    "유비스트": "UBIST",
}
_CHANNEL_ALIAS_PAIRS: Final[tuple[tuple[str, str], ...]] = (
    ("상급종합병원", "상급종병"),
    ("상급 종합병원", "상급종병"),
    ("상급 종합 병원", "상급종병"),
    ("상급병원", "상급종병"),
    ("상급 병원", "상급종병"),
    ("상급종병", "상급종병"),
    ("상급 종병", "상급종병"),
    ("상종", "상급종병"),
    ("종합병원", "종병"),
    ("종합 병원", "종병"),
    ("종병", "종병"),
    ("치과의원", "기타"),
    ("치과 의원", "기타"),
    ("치과병원", "기타"),
    ("치과 병원", "기타"),
    ("기타(치과의원, 치과병원 등)", "기타"),
    ("보건소", "보건소"),
    ("병원", "병원"),
    ("의원", "의원"),
)


def _build_alias_map(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for alias, canonical in pairs:
        previous = aliases.get(alias)
        if previous is not None and previous != canonical:
            raise ValueError(f"conflicting channel alias: {alias!r} -> {previous!r}/{canonical!r}")
        aliases[alias] = canonical
    return aliases


CHANNEL_ALIASES: Final[dict[str, str]] = _build_alias_map(_CHANNEL_ALIAS_PAIRS)
_CHANNEL_STORAGE_KEYS: Final[dict[str, str]] = {
    "상급종합병원": "상급종병",
    "종합병원": "종병",
    "기타(치과의원, 치과병원 등)": "기타",
}
LEVEL_ALIASES: Final[dict[str, str]] = {
    "제형": "제형",
    "제형별": "제형",
    "투여경로": "제형",
    "class": "Class",
    "클래스": "Class",
    "성분": "Molecule",
    "성분별": "Molecule",
    "molecule": "Molecule",
    "용량": "용량",
    "용량별": "용량",
    "strength": "용량",
    "브랜드": "Brand",
    "브랜드별": "Brand",
}


def normalise_source(value: str) -> str | None:
    clean = value.strip()
    return SOURCE_ALIASES.get(clean) or SOURCE_ALIASES.get(clean.upper())


def normalise_measure(value: str) -> str | None:
    return value if value in {"sales", "volume"} else None


def normalise_channel(value: str) -> str:
    return CHANNEL_ALIASES.get(value.strip(), value.strip())


def match_channel_in_text(text: str) -> str | None:
    matches = (
        (alias, canonical, text.find(alias))
        for alias, canonical in CHANNEL_ALIASES.items()
        if alias in text
    )
    selected = min(matches, key=lambda item: (-len(item[0]), item[2], item[0]), default=None)
    return selected[1] if selected is not None else None


def normalise_channel_data(data: dict[str, object]) -> dict[str, object]:
    normalised: dict[str, object] = {}
    for key, value in data.items():
        canonical = _CHANNEL_STORAGE_KEYS.get(key, key)
        if canonical in normalised and normalised[canonical] != value:
            raise ValueError(f"conflicting channel data after normalisation: {canonical!r}")
        normalised[canonical] = value
    return normalised


def normalise_level(value: str) -> str:
    return LEVEL_ALIASES.get(value.strip(), value.strip())
