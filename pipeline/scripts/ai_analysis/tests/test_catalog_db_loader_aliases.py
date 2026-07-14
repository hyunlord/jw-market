"""위너프A+ IndexError 정정 (A 안전 접근 + B-1 alias 해소) 회귀 테스트.

DB 불필요 — _parse_catalog_description / _english_name_from_parsed 순수 함수 검증.
실데이터(docs/crawl/_catalog.json, search_keywords.json) 사용.
"""
from __future__ import annotations

from bundle_builder.catalog_db_loader import (
    _catalog_lookup_keys,
    _english_name_from_parsed,
    _parse_catalog_description,
)

CANONICAL_25 = [
    "라베칸", "라베칸듀오", "제이클", "가드렛", "가드메트", "타발리스", "시그마트",
    "리바로", "리바로젯", "리바로페노", "리바로하이", "리바로브이", "트루패스",
    "피나스타", "제이다트", "뉴트로진", "모빌리아", "악템라", "페린젝트", "베노훼럼",
    "헴리브라", "위너프", "위너프A+", "엔커버", "플라주오피",
]


def test_A_empty_list_no_indexerror():
    # 빈 list 도 IndexError 없이 fallback (정정 전엔 [][0] crash)
    parsed = {"search_keywords": {"약 영문명": []}, "english_name": "FALLBACK"}
    assert _english_name_from_parsed(parsed) == "FALLBACK"


def test_A_missing_key_uses_fallback():
    parsed = {"search_keywords": {}, "english_name": "FALLBACK"}
    assert _english_name_from_parsed(parsed) == "FALLBACK"


def test_A_value_present_wins():
    parsed = {"search_keywords": {"약 영문명": ["WINUF A+"]}, "english_name": "x"}
    assert _english_name_from_parsed(parsed) == "WINUF A+"


def test_A_all_empty_returns_none_no_crash():
    parsed = {"search_keywords": {"약 영문명": []}, "english_name": None}
    assert _english_name_from_parsed(parsed) is None


def test_B1_alias_resolution_for_winuf_a_plus():
    # 위너프A+ -> 위너프에이플러스 alias 해소 후보에 포함
    keys = _catalog_lookup_keys("위너프A+")
    assert keys[0] == "위너프A+"
    assert "위너프에이플러스" in keys


def test_B1_winuf_a_plus_resolves_real_english_name():
    parsed = _parse_catalog_description("위너프A+")
    # alias 해소로 search_keywords 가 위너프에이플러스 entry 로 채워짐
    assert parsed["search_keywords"].get("약 영문명") == ["WINUF A+"]
    assert _english_name_from_parsed(parsed) == "WINUF A+"


def test_no_regression_direct_key_brand_does_not_use_molecule_alias():
    # 가드렛 = 직접 키 존재 + alias 가 molecule(ANAGLIPTIN) → 직접 키로 해소되어야 함
    parsed = _parse_catalog_description("가드렛")
    assert _english_name_from_parsed(parsed) == "GUARDLET"


def test_all_25_brands_no_indexerror_and_have_english_name():
    failures = []
    for brand in CANONICAL_25:
        try:
            parsed = _parse_catalog_description(brand)
            eng = _english_name_from_parsed(parsed)
            if not eng:
                failures.append((brand, "empty english_name"))
        except Exception as exc:  # pragma: no cover
            failures.append((brand, f"{type(exc).__name__}: {exc}"))
    assert not failures, f"brands failed: {failures}"
