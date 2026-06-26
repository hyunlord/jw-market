from __future__ import annotations

from typing import Final


SOURCE_ALIASES: Final[dict[str, str]] = {
    "IQVIA": "IQVIA",
    "아이큐비아": "IQVIA",
    "UBIST": "UBIST",
    "유비스트": "UBIST",
}
CHANNEL_ALIASES: Final[dict[str, str]] = {
    "상급종병": "상급종병",
    "상급 종병": "상급종병",
    "상종": "상급종병",
    "종병": "종병",
    "병원": "병원",
    "의원": "의원",
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


def normalise_level(value: str) -> str:
    return LEVEL_ALIASES.get(value.strip(), value.strip())
