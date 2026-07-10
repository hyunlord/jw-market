from __future__ import annotations

from typing import Any

import pytest

from pipeline.etl.io.mart.brand_alias_resolver import (
    MANUAL_BRAND_ALIASES,
    AliasReverseCollisionError,
    AliasShadowingError,
    BrandAliasResolver,
    resolve_brand_key,
)
from pipeline.etl.io.catalog.brand.strategic_product_text import clean_text
from pipeline.etl.io.catalog.postfix.text import normalize_brand_name


class _FakeCursor:
    def __init__(self) -> None:
        self._rows: list[dict[str, str]] = []

    def execute(self, sql: str) -> None:
        if "DISTINCT brand_key" in sql:
            self._rows = [{"brand_key": "리바로브이"}]
        elif "alias_name, brand_key" in sql:
            self._rows = [{"alias_name": "리바로 브이", "brand_key": "리바로브이"}]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self) -> list[dict[str, str]]:
        return self._rows


class _FakeConnection:
    def cursor(self) -> _FakeCursor:
        return _FakeCursor()


def test_static_resolver_rejects_reverse_collisions() -> None:
    with pytest.raises(AliasReverseCollisionError):
        BrandAliasResolver.from_static(
            (("같은표기", "브랜드a"), ("같은표기", "브랜드b")),
            canonical_keys=("브랜드a", "브랜드b"),
        )


def test_static_resolver_rejects_shadowing_another_canonical_key() -> None:
    with pytest.raises(AliasShadowingError):
        BrandAliasResolver.from_static(
            (("브랜드a", "브랜드b"),),
            canonical_keys=("브랜드a", "브랜드b"),
        )


def test_static_resolver_maps_manual_winnerf_alias() -> None:
    resolver = BrandAliasResolver.from_static(
        MANUAL_BRAND_ALIASES.items(),
        canonical_keys=("위너프에이플러스",),
    )

    assert resolver.resolve("위너프A+") == "위너프에이플러스"
    assert resolver.resolve_alias("위너프A+") == "위너프에이플러스"


def test_catalog_product_join_uses_central_manual_alias() -> None:
    assert clean_text("위너프A+") == "위너프에이플러스"
    assert clean_text("리바로") == "리바로"


def test_static_resolver_maps_display_name_to_existing_canonical_key() -> None:
    resolver = BrandAliasResolver.from_static(
        (("리바로 브이", "리바로브이"),),
        canonical_keys=("리바로브이",),
    )

    assert resolver.resolve("리바로 브이") == "리바로브이"
    assert resolver.resolve_alias("알수없는 표시") == "알수없는 표시"


def test_connection_resolver_loads_canonical_keys_and_aliases() -> None:
    assert resolve_brand_key("리바로 브이", _FakeConnection()) == "리바로브이"


@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        (None, ""),
        (float("nan"), ""),
        (" 리바로 브이 ", "리바로브이"),
        ("위너프A+", "위너프a"),
        ("제품명(50mg)", "제품명"),
        ("제품명 50 mg 정", "제품명"),
        ("A_B-C·D", "a_bcd"),
        ("테스트 주", "테스트"),
        ("테스트주", "테스트주"),
    ],
)
def test_catalog_postfix_uses_common_normalizer_without_behavior_change(
    raw_name: Any,
    expected: str,
) -> None:
    assert normalize_brand_name(raw_name) == expected
