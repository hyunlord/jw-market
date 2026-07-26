from __future__ import annotations

from pipeline.scripts.crawler.hira_benefit.scope import (
    BrandScopeEntry,
    MoleculeScopeEntry,
    derive_dosage_form_suffixes,
    derive_non_specific_molecules,
    match_brand_scope,
    normalize_scope_text,
)


def _brand(
    key: str,
    name: str,
    *atc4_codes: str,
) -> BrandScopeEntry:
    return BrandScopeEntry(
        brand_key=key,
        brand_name=name,
        atc4_codes=tuple(atc4_codes),
    )


def test_longest_boundary_match_blocks_all_known_short_name_collisions() -> None:
    scope = (
        _brand("라베칸", "라베칸", "C09D0"),
        _brand("라베칸듀오", "라베칸듀오", "C09D0"),
        _brand("리바로", "리바로", "C10A1"),
        _brand("리바로브이", "리바로브이", "C10C0"),
        _brand("리바로젯", "리바로젯", "C10C0"),
        _brand("리바로페노", "리바로페노", "C10C0"),
        _brand("리바로하이", "리바로하이", "C10C0"),
        _brand("위너프", "위너프", "B05B0"),
        _brand("위너프에이플러스", "위너프A+", "B05B0"),
        _brand("위너프에이플러스", "위너프에이플러스", "B05B0"),
    )

    cases = (
        ("품명: 라베칸듀오정", "라베칸듀오"),
        ("품명: 리바로브이정", "리바로브이"),
        ("품명: 리바로젯정", "리바로젯"),
        ("품명: 리바로페노정", "리바로페노"),
        ("품명: 리바로하이정", "리바로하이"),
        ("품명: 위너프A+", "위너프에이플러스"),
        ("품명: 위너프에이플러스", "위너프에이플러스"),
    )
    suffixes = derive_dosage_form_suffixes(
        scope
        + (
            _brand("첫째", "첫째"),
            _brand("둘째", "둘째"),
            _brand("셋째", "셋째"),
        ),
        ("품명: 첫째정", "품명: 둘째정", "품명: 셋째정"),
    )

    for raw_text, expected_key in cases:
        matches = match_brand_scope(
            raw_text,
            scope,
            (),
            dosage_form_suffixes=suffixes,
        )
        assert tuple(match.brand_key for match in matches) == (expected_key,)


def test_longest_match_wins_when_aliases_share_a_punctuation_boundary() -> None:
    scope = (
        _brand("위너프", "위너프", "B05B0"),
        _brand("위너프에이플러스", "위너프+", "B05B0"),
    )

    matches = match_brand_scope("품명: 위너프+", scope, ())

    assert tuple(match.brand_key for match in matches) == ("위너프에이플러스",)


def test_dosage_form_continuations_keep_actemra_and_eylea_matches() -> None:
    scope = (
        _brand("악템라", "악템라", "L04A0"),
        _brand("아일리아", "아일리아", "S01P0"),
    )

    source_texts = (
        "품명: 첫째주, 둘째주, 셋째주 분류 고시",
        "품명: 첫째피하주사, 둘째피하주사, 셋째피하주사 분류 고시",
        "품명: 첫째프리필드시린지, 둘째프리필드시린지, 셋째프리필드시린지 분류 고시",
    )
    derivation_scope = scope + tuple(
        _brand(name, name, "L04A0")
        for name in ("첫째", "둘째", "셋째")
    )
    suffixes = derive_dosage_form_suffixes(derivation_scope, source_texts)
    assert {"주", "피하주사", "프리필드시린지"} <= suffixes
    raw_text = (
        "Tocilizumab(악템라주),  품명: 악템라피하주사162밀리그램, "
        "아일리아프리필드시린지"
    )
    matches = match_brand_scope(
        raw_text,
        scope,
        (),
        dosage_form_suffixes=suffixes,
    )

    assert tuple(match.brand_key for match in matches) == ("아일리아", "악템라")
    assert all(match.match_method == "exact_boundary_name" for match in matches)
    assert all(match.confidence == "high" for match in matches)
    assert all(match.evidence_end > match.evidence_start for match in matches)
    normalized = normalize_scope_text(raw_text)
    assert all(
        normalized[match.evidence_start : match.evidence_end] == match.matched_text
        for match in matches
    )
    assert {
        match.evidence_coordinate for match in matches
    } == {"normalized_nfc_casefold_whitespace"}


def test_left_boundary_blocks_short_names_inside_general_words() -> None:
    scope = (
        _brand("그린", "그린", "A01A0"),
        _brand("에피", "에피", "A01A0"),
        _brand("마이신", "마이신", "J01A0"),
    )

    assert match_brand_scope(
        "integrin 억제제와 에피소드, 반코마이신 기준",
        scope,
        (),
    ) == ()


def test_right_boundary_blocks_brand_prefix_inside_longer_word() -> None:
    scope = (_brand("코르티", "코르티", "H02A0"),)

    assert match_brand_scope("코르티코이드 치료 기준", scope, ()) == ()


def test_short_names_require_product_context_even_at_word_boundaries() -> None:
    scope = (_brand("호의", "호의", "B02D0"),)

    assert match_brand_scope("각 호의 기준을 검토한다", scope, ()) == ()
    assert {
        match.brand_key
        for match in match_brand_scope("품명: 호의", scope, ())
    } == {"호의"}


def test_duplicate_display_name_with_different_keys_fails_closed() -> None:
    scope = (
        _brand("canonical-a", "중복브랜드"),
        _brand("canonical-b", "중복브랜드"),
    )

    assert match_brand_scope("품명: 중복브랜드", scope, ()) == ()


def test_nfd_and_case_are_normalized_for_matching() -> None:
    scope = (
        _brand("actemra", "actemra", "L04A0"),
        _brand("리바로", "리바로", "C10A1"),
    )

    assert tuple(
        match.brand_key
        for match in match_brand_scope(
            "ACTEMRA 리바로",
            scope,
            (),
        )
    ) == ("actemra", "리바로")


def test_molecule_candidates_require_one_brand_within_atc4_context() -> None:
    brands = (
        _brand("아일리아", "아일리아", "S01P0"),
        _brand("경쟁안과", "경쟁안과", "S01P0"),
        _brand("단일브랜드", "단일브랜드", "S01P0"),
        _brand("타시장", "타시장", "L01X0"),
    )
    molecules = (
        MoleculeScopeEntry("aflibercept", "아일리아", "아일리아", "S01P0"),
        MoleculeScopeEntry("aflibercept", "경쟁안과", "경쟁안과", "S01P0"),
        MoleculeScopeEntry("unique-mol", "단일브랜드", "단일브랜드", "S01P0"),
        MoleculeScopeEntry("sharedmol", "아일리아", "아일리아", "S01P0"),
        MoleculeScopeEntry("sharedmol", "타시장", "타시장", "L01X0"),
    )

    ambiguous_within_atc = match_brand_scope(
        "aflibercept 주사제 분류 고시 급여기준",
        brands,
        molecules,
    )
    unique_brand_without_context = match_brand_scope(
        "unique-mol 주사제 분류 고시 급여기준",
        brands,
        molecules,
    )
    unique_brand_with_context = match_brand_scope(
        "S01P0 unique-mol 주사제 분류 고시 급여기준",
        brands,
        molecules,
    )
    ambiguous_without_context = match_brand_scope(
        "sharedmol 주사제 분류 고시 급여기준",
        brands,
        molecules,
    )
    narrowed_by_direct_brand = match_brand_scope(
        "품명: 아일리아 등. sharedmol 기준",
        brands,
        molecules,
    )

    assert ambiguous_within_atc == ()
    assert unique_brand_without_context == ()
    assert {match.brand_key for match in unique_brand_with_context} == {
        "단일브랜드"
    }
    assert all(
        match.match_method == "molecule_via_atc4"
        for match in unique_brand_with_context
    )
    assert ambiguous_without_context == ()
    assert {match.brand_key for match in narrowed_by_direct_brand} == {"아일리아"}


def test_molecule_candidates_are_limited_to_the_notice_heading() -> None:
    brands = (_brand("옵디보", "옵디보", "L01G5"),)
    molecules = (
        MoleculeScopeEntry("nivolumab", "옵디보", "옵디보", "L01G5"),
    )

    assert match_brand_scope(
        "PD-L1 동반진단 분류 고시 본문에서 nivolumab 반응을 예측",
        brands,
        molecules,
    ) == ()
    assert {
        match.brand_key
        for match in match_brand_scope(
            "L01G5 nivolumab 주사제 분류 고시 급여기준",
            brands,
            molecules,
        )
    } == {"옵디보"}


def test_data_distribution_blocks_non_specific_molecules() -> None:
    rows = tuple(
        MoleculeScopeEntry(
            molecule_norm="sodium",
            brand_key=f"brand-{index}",
            brand_name=f"브랜드{index}",
            atc4_code=f"A{index:03d}",
        )
        for index in range(20)
    ) + tuple(
        MoleculeScopeEntry(
            molecule_norm=f"specific-{index}",
            brand_key=f"specific-brand-{index}",
            brand_name=f"특이브랜드{index}",
            atc4_code="S01P0",
        )
        for index in range(12)
    ) + (
        MoleculeScopeEntry("aflibercept", "아일리아", "아일리아", "S01P0"),
        MoleculeScopeEntry("aflibercept", "경쟁안과", "경쟁안과", "S01P0"),
        MoleculeScopeEntry("31", "가다실9", "가다실 9", "J07E3"),
        MoleculeScopeEntry("제외", "쏘메토", "쏘메토", "G4C9"),
    )

    blocked = derive_non_specific_molecules(
        rows,
        (
            "sodium 함유 기준",
            "sodium 투여 기준",
            "aflibercept 주사제",
        ),
    )

    assert blocked == frozenset({"31", "sodium", "제외"})
