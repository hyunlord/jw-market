"""AM07-FIX: 계열 asks for a product family just like 패밀리 does.

"리바로 계열 매출" used to resolve quietly to the single brand 리바로 while
"리바로패밀리" raised an ambiguity listing every family member. These tests pin the
new parity and, just as importantly, pin the words that must NOT be read as a
family marker: 계열사·계열회사·계열화 continue past the marker into another word,
and a drug-class prefix such as 스타틴 has no sibling brands to be ambiguous
between.
"""

from __future__ import annotations

import pytest
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.resolver.brand_resolver import AmbiguousBrandError
from jw_chat_agent_poc.resolver.brand_resolver import UnsupportedBrandError

LIVARO_FAMILY = ("리바로", "리바로젯", "리바로브이", "리바로페노", "리바로하이")


@pytest.fixture(name="resolver")
def _resolver() -> BrandResolver:
    return BrandResolver(mode="fixture")


@pytest.mark.parametrize(
    ("question", "expected_key"),
    (
        ("리바로 계열 매출 알려줘", "리바로계열"),
        ("리바로계열 매출 알려줘", "리바로계열"),
        ("리바로패밀리 매출 알려줘", "리바로패밀리"),
        ("리바로 패밀리 매출 알려줘", "리바로패밀리"),
    ),
)
def test_family_marker_raises_ambiguity_with_its_own_key(
    resolver: BrandResolver, question: str, expected_key: str
) -> None:
    with pytest.raises(AmbiguousBrandError) as excinfo:
        resolver.resolve_many(question, allow_default=False)

    assert excinfo.value.query == expected_key
    assert set(excinfo.value.candidates) == set(LIVARO_FAMILY)


@pytest.mark.parametrize(
    "question",
    (
        "리바로 계열 매출 알려줘",
        "리바로패밀리 매출 알려줘",
    ),
)
def test_both_resolve_paths_raise(resolver: BrandResolver, question: str) -> None:
    """resolve() and resolve_many() both call the family check."""

    with pytest.raises(AmbiguousBrandError):
        resolver.resolve(question, allow_default=False)
    with pytest.raises(AmbiguousBrandError):
        resolver.resolve_many(question, allow_default=False)


@pytest.mark.parametrize(
    "question",
    (
        "리바로 계열사 매출 알려줘",
        "리바로 계열회사 알려줘",
        "리바로 계열화 전략 알려줘",
    ),
)
def test_marker_must_end_the_word(resolver: BrandResolver, question: str) -> None:
    """계열사·계열회사·계열화 are different words, not a family request."""

    resolved = resolver.resolve_many(question, allow_default=False)

    assert tuple(item.canonical_brand for item in resolved) == ("리바로",)


@pytest.mark.parametrize(
    "question",
    (
        "리바로 시계열 매출 알려줘",
        "리바로 매출 시계열 알려줘",
    ),
)
def test_time_series_word_is_not_a_family_marker(
    resolver: BrandResolver, question: str
) -> None:
    """시계열 contains 계열 but the prefix boundary keeps it out."""

    resolved = resolver.resolve_many(question, allow_default=False)

    assert tuple(item.canonical_brand for item in resolved) == ("리바로",)


@pytest.mark.parametrize(
    "question",
    (
        "스타틴 계열 매출 알려줘",
        "ARB 계열 시장 알려줘",
        "동일 계열 브랜드 비교해줘",
    ),
)
def test_drug_class_prefix_has_no_family_to_be_ambiguous_between(
    resolver: BrandResolver, question: str
) -> None:
    """A class name is not a brand prefix, so no ambiguity is raised for it."""

    with pytest.raises(UnsupportedBrandError):
        resolver.resolve_many(question, allow_default=False)


def test_plain_brand_is_untouched(resolver: BrandResolver) -> None:
    resolved = resolver.resolve_many("리바로 매출 알려줘", allow_default=False)

    assert tuple(item.canonical_brand for item in resolved) == ("리바로",)


def test_multiple_brands_still_resolve_to_all_of_them(resolver: BrandResolver) -> None:
    """brands[0] narrowing lives in the planner and stays out of scope here."""

    resolved = resolver.resolve_many("리바로와 리바로젯 매출 알려줘", allow_default=False)

    assert tuple(item.canonical_brand for item in resolved) == ("리바로", "리바로젯")
