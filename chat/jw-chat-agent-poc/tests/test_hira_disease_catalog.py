from __future__ import annotations

from dataclasses import dataclass

import pytest

from jw_chat_agent_poc.orchestrator.hira_disease import (
    hira_disease_calls,
    hira_disease_code_for_resolution,
    hira_disease_code_for_unbranded_query,
)
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.external import ExternalApiClient
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader


@dataclass(frozen=True)
class _Resolution:
    canonical_brand: str
    molecule_en: tuple[str, ...]
    support_source: str = "fixture"


@dataclass(frozen=True)
class _MembershipReader:
    rows: tuple[dict[str, str], ...]

    def brand_memberships(self) -> tuple[dict[str, str], ...]:
        return self.rows


@dataclass(frozen=True)
class _MoleculeReader:
    rows: tuple[dict[str, str], ...]

    def brand_molecules(self) -> tuple[dict[str, str], ...]:
        return self.rows


def test_competitor_brand_resolves_through_ingredient_dictionary() -> None:
    resolution = _Resolution(canonical_brand="리피토", molecule_en=("ATORVASTATIN",))

    calls = hira_disease_calls(
        "리피토 관련 질병 환자수",
        resolution,
        ExternalApiClient(mode="fixture"),
    )

    mapping = calls[0]
    assert mapping.tool == "hira_disease_mapping"
    assert mapping.status == "mapped"
    assert mapping.render_data["sickCd"] == "E78"
    assert mapping.render_data["mapping_source"] == "ingredient_disease_dictionary"
    assert mapping.render_data["matched_ingredients"] == ["ATORVASTATIN"]
    assert any(call.tool == "hira_disease_hospitalization_outpatient_stats" for call in calls)


@pytest.mark.parametrize(
    ("brand", "molecule", "expected_sick_cd"),
    (
        ("리피토", "atorvastatin", "E78"),
        ("크레스토", "rosuvastatin", "E78"),
        ("아토르바", "atorvastatin", "E78"),
        ("마운자로", "tirzepatide", "E11"),
        ("자누비아", "sitagliptin", "E11"),
    ),
)
def test_mart_resolver_to_hira_mapping_path_for_competitor_brands(
    brand: str,
    molecule: str,
    expected_sick_cd: str,
) -> None:
    resolver = BrandResolver(
        mode="cache",
        brand_reader=StaticMetricsCacheReader(cache_brands=[], market_status=[]),
        membership_reader=_MembershipReader(
            ({"brand": brand, "market_id": "dynamic", "market_name": "동적 시장"},)
        ),
        molecule_reader=_MoleculeReader(
            (
                {
                    "brand_name": brand,
                    "brand_key": brand,
                    "molecule_display": molecule,
                    "molecule_norm": molecule,
                },
            )
        ),
    )

    resolution = resolver.resolve(f"{brand} 관련 질병 환자수", allow_default=False)
    calls = hira_disease_calls(
        f"{brand} 관련 질병 환자수",
        resolution,
        ExternalApiClient(mode="fixture"),
    )

    assert resolution.support_source.endswith("mart_brand_molecule")
    assert calls[0].tool == "hira_disease_mapping"
    assert calls[0].render_data["sickCd"] == expected_sick_cd


def test_new_brand_with_known_ingredient_needs_no_brand_entry() -> None:
    resolution = _Resolution(canonical_brand="신규피타제품", molecule_en=("pitavastatin",))

    assert hira_disease_code_for_resolution(resolution) == "E78"


def test_brand_name_without_ingredient_does_not_fall_back_to_legacy_brand_map() -> None:
    resolution = _Resolution(canonical_brand="리바로", molecule_en=())

    calls = hira_disease_calls(
        "리바로 관련 질병 환자수",
        resolution,
        ExternalApiClient(mode="fixture"),
    )

    assert [call.tool for call in calls] == ["hira_disease_mapping_unresolved"]
    assert calls[0].status == "mapping_failed"
    assert calls[0].render_data["reason"] == "ingredient_to_kcd_mapping_missing"
    assert "매핑 없음" in calls[0].summary_text


def test_resolved_brand_without_ingredient_evidence_does_not_use_question_disease_term() -> None:
    # Given: a catalog-resolved brand has no ingredient evidence, while its question contains a known disease term.
    resolution = _Resolution(
        canonical_brand="검증신약",
        molecule_en=(),
        support_source="catalog_membership",
    )

    # When: the brand-bound HIRA path attempts to resolve the disease.
    calls = hira_disease_calls(
        "검증신약 고혈압 환자수",
        resolution,
        ExternalApiClient(mode="fixture"),
    )

    # Then: the disease term cannot substitute for missing brand-to-ingredient evidence.
    assert [call.tool for call in calls] == ["hira_disease_mapping_unresolved"]
    assert calls[0].status == "mapping_failed"


def test_dictionary_resolution_allows_pure_unbranded_disease_query() -> None:
    # Given: the agent explicitly classified a pure disease question through the disease dictionary.
    resolution = _Resolution(
        canonical_brand="본태성 고혈압",
        molecule_en=(),
        support_source="hira_disease_dictionary",
    )

    # When: the HIRA path resolves the unbranded disease query.
    calls = hira_disease_calls(
        "고혈압 환자수",
        resolution,
        ExternalApiClient(mode="fixture"),
    )

    # Then: the explicit dictionary resolution may map to KCD I10.
    assert calls[0].tool == "hira_disease_mapping"
    assert calls[0].render_data["sickCd"] == "I10"


def test_unknown_ingredient_fails_closed_without_hira_api_calls() -> None:
    resolution = _Resolution(canonical_brand="검증신약", molecule_en=("novel-molecule-x",))

    calls = hira_disease_calls(
        "검증신약 관련 질병 환자수",
        resolution,
        ExternalApiClient(mode="fixture"),
    )

    assert [call.tool for call in calls] == ["hira_disease_mapping_unresolved"]
    assert calls[0].status == "mapping_failed"
    assert calls[0].render_data["unmapped_ingredients"] == ["novel-molecule-x"]
    assert "원천에 없음" not in calls[0].summary_text


@pytest.mark.parametrize(
    "question",
    ("고혈압 환자수?", "2024년 고혈압의 환자 통계 알려줘"),
)
def test_unbranded_disease_query_allows_only_query_scaffolding(question: str) -> None:
    assert hira_disease_code_for_unbranded_query(question) == "I10"


def test_combination_brand_unions_and_deduplicates_ingredient_diseases() -> None:
    resolution = _Resolution(
        canonical_brand="리바로하이",
        molecule_en=("pitavastatin", "valsartan", "amlodipine"),
    )

    calls = hira_disease_calls(
        "리바로하이 관련 질병 환자수",
        resolution,
        ExternalApiClient(mode="fixture"),
    )

    mappings = [call for call in calls if call.tool == "hira_disease_mapping"]
    assert [call.render_data["sickCd"] for call in mappings] == ["E78", "I10"]
    assert mappings[1].render_data["matched_ingredients"] == ["valsartan", "amlodipine"]
