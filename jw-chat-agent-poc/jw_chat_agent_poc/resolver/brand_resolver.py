from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re


@dataclass(frozen=True)
class BrandResolution:
    canonical_brand: str
    audit_code: str
    molecule_en: tuple[str, ...]
    atc: tuple[str, ...]
    edi_code: str | None
    item_seq: str | None
    is_combo: bool


class BrandResolver:
    def __init__(self, fixture_path: Path | None = None, default_brand: str = "리바로") -> None:
        path = fixture_path or Path(__file__).resolve().parents[1] / "fixtures" / "brand_catalog.json"
        self._items = json.loads(path.read_text(encoding="utf-8"))
        self._default_brand = default_brand

    def resolve(self, question_or_brand: str) -> BrandResolution:
        normalized = self._normalize(question_or_brand)
        items = sorted(
            self._items,
            key=lambda item: max(len(self._normalize(alias)) for alias in [item["canonical_brand"], *item.get("aliases", [])]),
            reverse=True,
        )
        for item in items:
            aliases = [item["canonical_brand"], *item.get("aliases", [])]
            if any(self._normalize(alias) in normalized for alias in aliases):
                return self._to_resolution(item)
        for item in self._items:
            if item["canonical_brand"] == self._default_brand:
                return self._to_resolution(item)
        raise LookupError(f"No fixture brand matched and default is missing: {question_or_brand}")

    @staticmethod
    def _to_resolution(item: dict) -> BrandResolution:
        return BrandResolution(
            canonical_brand=item["canonical_brand"],
            audit_code=item["audit_code"],
            molecule_en=tuple(item["molecule_en"]),
            atc=tuple(item["atc"]),
            edi_code=item.get("edi_code"),
            item_seq=item.get("item_seq"),
            is_combo=len(item["molecule_en"]) > 1,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\\s+", "", text).lower()
