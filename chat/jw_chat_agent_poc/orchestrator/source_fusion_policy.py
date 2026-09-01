from __future__ import annotations


def source_family(source: str) -> str:
    """Return the stable evidence family used by cross-source fusion guards."""

    normalized = source.casefold().replace(" ", "_")
    if normalized.startswith("iqvia"):
        return "iqvia"
    if normalized.startswith("ubist"):
        return "ubist"
    if "event_brand_scores" in normalized or "deep_analysis" in normalized:
        return "news"
    if normalized.startswith("csd"):
        return "csd"
    if normalized.startswith("hira"):
        return "hira"
    if "file" in normalized or "업로드" in normalized:
        return "file"
    return normalized
