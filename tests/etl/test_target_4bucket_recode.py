from __future__ import annotations

import shutil

from pipeline.etl.io.cache.archive_runner import materialize_archive
from pipeline.etl.io.catalog.target.text import ubist_customer_label
from pipeline.etl.io.mart.dict_ubist_translation import translate_target_ubist
from pipeline.etl.io.mart.ubist_channel_mapping import parse_channel_code


def test_ubist_customer_label_uses_target_only_four_buckets() -> None:
    assert ubist_customer_label("상급종합병원", "순환기(Cardiology IM)") == "TGH Cardio"
    assert ubist_customer_label("종합병원", "순환기(Cardiology IM)") == "TGH Cardio"
    assert ubist_customer_label("병원", "Others(병원,보건기관, 그 외 요양기관)") == "Semi Others"
    assert ubist_customer_label("보건소", "Others(병원,보건기관, 그 외 요양기관)") == "OT Others"
    assert (
        ubist_customer_label(
            "기타(치과의원, 치과병원 등)",
            "Others(병원,보건기관, 그 외 요양기관)",
        )
        == "OT Others"
    )
    assert ubist_customer_label("의원", "가정의학과(FM)") == "CL IGF"


def test_translate_target_ubist_expands_four_bucket_raw_values() -> None:
    assert translate_target_ubist("TGH Cardio") == [
        "상급종합병원 순환기(Cardiology IM)",
        "종합병원 순환기(Cardiology IM)",
    ]
    assert translate_target_ubist("GH Cardio") == [
        "상급종합병원 순환기(Cardiology IM)",
        "종합병원 순환기(Cardiology IM)",
    ]
    assert translate_target_ubist("Semi Others") == [
        "병원 Others(병원,보건기관, 그 외 요양기관)"
    ]
    assert translate_target_ubist("OT Others") == [
        "보건소 Others(병원,보건기관, 그 외 요양기관)",
        "기타(치과의원, 치과병원 등) Others(병원,보건기관, 그 외 요양기관)",
    ]


def test_shared_ubist_channel_mapping_keeps_existing_global_gh_semantics() -> None:
    parsed = parse_channel_code("GH Cardio")

    assert parsed is not None
    assert parsed.display_name == "종합병원 순환기"
    assert parsed.facility_raw_values == ("상급종합병원", "종합병원", "병원")


def test_archive_builder_materialization_receives_target_patch_only() -> None:
    temp_root = materialize_archive()
    try:
        resolver = (
            temp_root / "pipeline" / "scripts" / "etl" / "ubist_channel_resolver.py"
        ).read_text(encoding="utf-8")
        cause_builder = (
            temp_root / "pipeline" / "scripts" / "etl" / "build_cache_cause.py"
        ).read_text(encoding="utf-8")
        target_mapping = (
            temp_root / "pipeline" / "scripts" / "utils" / "ubist_target_channel_mapping.py"
        )

        assert target_mapping.exists()
        assert "parse_target_channel_code as parse_channel_code" in resolver
        assert "raw_pair_to_target_channel_code as raw_pair_to_channel_code" in resolver
        assert "from pipeline.scripts.utils.ubist_channel_mapping import" not in resolver
        assert (
            'analysis_level_market_channels = analysis_levels.get("channels") '
            'or _channels_for_source(source_api)'
        ) in cause_builder
        assert "analysis_level_market_channels = target_customer_channels or" not in cause_builder
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
