"""Phase 7 v3 regression tests for ml_003 Korean brand names."""

from __future__ import annotations

import re
import urllib.parse

import requests


BASE_URL = "http://127.0.0.1:8013"


def _cause(brand: str = "가드메트") -> dict:
    encoded = urllib.parse.quote(brand)
    url = f"{BASE_URL}/api/cause/{encoded}?view=market_landscape&source=UBIST&measure=sales"
    response = requests.get(url, timeout=30)
    assert response.status_code == 200, response.text[:500]
    return response.json()


def _looks_like_molecule(value: str) -> bool:
    text = str(value or "").strip()
    if text in {"", "기타", "Others"}:
        return False
    if "+" in text:
        return True
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9 /().-]*", text))


def test_ml_003_brand_ranking_uses_raw_korean_brand_names() -> None:
    data = _cause()
    rankings = data["data"]["brand_ranking_stacked"]["yearly"][-1]["rankings"]
    names = [row["brand"] for row in rankings]
    assert names
    assert not [name for name in names if _looks_like_molecule(name)]


def test_ml_003_ei_ms_matrix_uses_raw_korean_brand_names() -> None:
    data = _cause()
    matrix = data["data"]["ei_ms_matrix"]
    rows = matrix["data"] if isinstance(matrix, dict) else matrix
    names = [row.get("brand") or row.get("brand_key") for row in rows[:100]]
    assert names
    assert not [name for name in names if _looks_like_molecule(name)]
