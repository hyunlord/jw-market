from __future__ import annotations

# noqa: SIZE_OK - Existing broad auto-topic regression suite; split outside this scoped DB-backed market-group load.

import json
from pathlib import Path

import pytest

from pipeline.scripts.analysis.brand_activity.auto_topic import llm
from pipeline.scripts.analysis.brand_activity.auto_topic.data_source import (
    MissingMariaDbPasswordError,
    _mariadb_password,
    read_env_file,
    resolve_dictionary_source,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.market_scope import expected_markets
from pipeline.etl.io.catalog.master.market_definition import iter_market_definition_rows
from pipeline.scripts.analysis.brand_activity.auto_topic.market_groups import (
    _read_target_sheet,
    _records_from_market_definition_rows,
    _target_records,
    apply_csd_market_names,
    build_market_group_map,
    resolve_mi_master_path,
    scope_metadata_from_group_map,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.models import KeywordRow, TopicDefinition
from pipeline.scripts.analysis.brand_activity.auto_topic.quality import (
    dictionary_cross_check,
    mechanical_guard,
    quality_summary,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.response import normalize_axis_payload, normalize_share_payload
from pipeline.scripts.analysis.brand_activity.auto_topic.sampling import build_market_samples, choose_sample_brands
from pipeline.scripts.analysis.brand_activity.auto_topic.chunking import chunk_rows_by_token_budget
from pipeline.scripts.analysis.brand_activity.auto_topic.stability import stabilize_axis
from pipeline.scripts.analysis.brand_activity.auto_topic.prompts import prompt_template_manifest
from pipeline.scripts.analysis.brand_activity.auto_topic.label_rules import label_quality_summary


def _row(row_id: int, atc4: str, brand: str, text: str = "sample") -> KeywordRow:
    return KeywordRow(
        row_id=row_id,
        period_ym="2025-10",
        atc4=atc4,
        brand=brand,
        keyword_text=text,
        interest="VERY USEFUL",
        prescription_frequency="increase",
        prescription_evolution="increase",
        promotional_lit="YES",
        abstract_lit="NO",
        patient_lit="NO",
        specialty="내과",
        visit_location="clinic",
        stage_row_sha256=f"hash-{row_id}",
    )


def test_expected_market_scope_is_csd_market_backed_13_atc4_for_11_final_markets() -> None:
    markets = expected_markets()

    assert len(markets) == 13
    assert markets[:4] == ("A02B2", "C10C0", "C10A1", "A10N1")
    assert {"K01A3", "V03G2", "V06D0"} <= set(markets)
    assert {"A06B1", "A07E9", "B01C5", "L03A1", "L04B0", "L04D0", "M01C0"}.isdisjoint(markets)


def test_choose_sample_brands_caps_one_to_seven_and_prefers_anchor() -> None:
    rows = [_row(i, "C10C0", "LIVALOZET") for i in range(10)]
    rows += [_row(100 + i, "C10C0", "ATOZET") for i in range(9)]
    rows += [_row(200 + i, "C10C0", "ROSUZET") for i in range(8)]
    rows += [_row(300 + i, "C10C0", "CRESTOR") for i in range(7)]
    rows += [_row(400 + i, "C10C0", "LIPITOR") for i in range(6)]
    rows += [_row(500 + i, "C10C0", "EZETROL") for i in range(5)]
    rows += [_row(600 + i, "C10C0", "MINOR1") for i in range(4)]
    rows += [_row(700 + i, "C10C0", "MINOR2") for i in range(3)]

    selected = choose_sample_brands(rows, known_anchors={"LIVALOZET"}, max_brands=7)

    assert selected == ("LIVALOZET", "ATOZET", "ROSUZET", "CRESTOR", "LIPITOR", "EZETROL", "MINOR1")


def test_choose_sample_brands_allows_configured_limit_above_seven() -> None:
    rows = [_row(i, "C10C0", "LIVALOZET") for i in range(10)]
    rows += [_row(100 + i, "C10C0", "ATOZET") for i in range(9)]
    rows += [_row(200 + i, "C10C0", "ROSUZET") for i in range(8)]
    rows += [_row(300 + i, "C10C0", "CRESTOR") for i in range(7)]
    rows += [_row(400 + i, "C10C0", "LIPITOR") for i in range(6)]
    rows += [_row(500 + i, "C10C0", "EZETROL") for i in range(5)]
    rows += [_row(600 + i, "C10C0", "MINOR1") for i in range(4)]
    rows += [_row(700 + i, "C10C0", "MINOR2") for i in range(3)]

    selected = choose_sample_brands(rows, known_anchors={"LIVALOZET"}, max_brands=20)

    assert selected == ("LIVALOZET", "ATOZET", "ROSUZET", "CRESTOR", "LIPITOR", "EZETROL", "MINOR1", "MINOR2")


def test_prompt_manifest_declares_single_concept_and_distinct_brand_topic_policy() -> None:
    manifest = prompt_template_manifest()
    label_policy = manifest["label_policy"]

    assert label_policy["single_concept_required"] is True
    assert label_policy["forbidden_connectors"] == ["및", "/", ","]
    assert label_policy["brand_specific_near_duplicate_allowed"] is False
    assert label_policy["brand_specific_max_topics"] == 2


def test_build_market_samples_applies_total_axis_rows_cap() -> None:
    rows = [_row(i, "C10C0", "LIVALOZET") for i in range(20)]
    rows += [_row(100 + i, "C10C0", "ATOZET") for i in range(20)]

    samples = build_market_samples(
        rows,
        markets=("C10C0",),
        descriptions={},
        axis_per_brand=20,
        axis_rows_cap=12,
        brand_rows=5,
        brands_per_market=2,
    )

    assert len(samples["axis_samples"]["C10C0"]) == 12
    assert len(samples["brand_samples"]["C10C0:LIVALOZET"]) == 5


def test_mi_master_group_map_groups_livalo_and_gardlet() -> None:
    group_map = build_market_group_map(expected_markets())

    assert group_map["sanity_checks"]["status"] == "pass"
    assert group_map["atc4_map"]["C10A1"]["group_id"] == group_map["atc4_map"]["C10C0"]["group_id"]
    assert group_map["atc4_map"]["A10N1"]["group_id"] == group_map["atc4_map"]["A10N3"]["group_id"]
    assert set(group_map["group_scope_ids"]) == {"group:livalo_family", "group:gardlet_family"}
    assert "V03G2" in set(group_map["mi_master_missing_atc4"])


def test_gateway_chat_path_template_supports_external_and_internal_wf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GENOS_GATEWAY_CHAT_PATH_TEMPLATE", raising=False)
    assert llm._gateway_chat_path("163") == "/api/gateway/rep/serving/163/chat/completions"

    monkeypatch.setenv("GENOS_GATEWAY_CHAT_PATH_TEMPLATE", "/rep/serving/{serving_id}/chat/completions")

    assert llm._gateway_chat_path("163") == "/rep/serving/163/chat/completions"


def test_data_source_accepts_env_only_without_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing_env = tmp_path / ".env"

    assert read_env_file(missing_env) == {}

    monkeypatch.delenv("MARIADB_ROOT_PASSWORD", raising=False)
    with pytest.raises(MissingMariaDbPasswordError):
        _mariadb_password({})

    monkeypatch.setenv("MARIADB_ROOT_PASSWORD", "env-placeholder")

    assert _mariadb_password({}) == "env-placeholder"


def test_dictionary_source_reports_missing_without_loading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pipeline.scripts.analysis.brand_activity.auto_topic import data_source

    missing_path = tmp_path / "missing_dictionary.json"
    monkeypatch.setattr(data_source, "DICTIONARY_PATH", missing_path)

    path, source = resolve_dictionary_source()

    assert path is None
    assert source == {"status": "missing", "path": str(missing_path)}


def test_dictionary_source_reports_found_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pipeline.scripts.analysis.brand_activity.auto_topic import data_source

    dictionary_path = tmp_path / "dictionary.json"
    dictionary_path.write_text('{"A02B2": {}}', encoding="utf-8")
    monkeypatch.setattr(data_source, "DICTIONARY_PATH", dictionary_path)

    path, source = resolve_dictionary_source()

    assert path == dictionary_path
    assert source == {"status": "found", "path": str(dictionary_path)}


def test_db_market_definition_rows_match_workbook_target_records() -> None:
    master_path = resolve_mi_master_path()
    assert master_path is not None
    sheet_names, target_rows = _read_target_sheet(master_path)
    workbook_records = _target_records(target_rows, sheet_names)
    db_records = _records_from_market_definition_rows(list(iter_market_definition_rows(master_path)))

    assert db_records == workbook_records


def test_scope_metadata_uses_mi_master_market_scopes_and_drops_csd_missing() -> None:
    bridge_map = {
        atc4: {"csd_market": f"{atc4} Market", "csd_market_missing": False, "csd_market_candidates": []}
        for atc4 in expected_markets()
    }
    bridge_map.update(
        {
            "C10A1": {"csd_market": "LIVALO Market", "csd_market_missing": False, "csd_market_candidates": []},
            "C10C0": {"csd_market": "LIVALOZET Market", "csd_market_missing": False, "csd_market_candidates": []},
            "A10N1": {"csd_market": "GUARDLET Market", "csd_market_missing": False, "csd_market_candidates": []},
            "A10N3": {"csd_market": "GUARDLET Market", "csd_market_missing": False, "csd_market_candidates": []},
            "V03G2": {"csd_market": None, "csd_market_missing": True, "csd_market_candidates": []},
        }
    )
    group_map = apply_csd_market_names(
        build_market_group_map(expected_markets()),
        {
            "atc4_map": bridge_map,
            "csd_market_missing_atc4": ["V03G2"],
            "all_csd_markets": ["LIVALO Market", "LIVALOZET Market", "GUARDLET Market", "FOSRENOL Market"],
        },
    )

    metadata = scope_metadata_from_group_map(group_map)

    assert "group:livalo_family" in metadata
    assert metadata["group:livalo_family"]["display_name"] == "LIVALO+LIVALOZET Market"
    assert metadata["group:livalo_family"]["atc4_values"] == ["C10A1", "C10C0"]
    assert "group:gardlet_family" in metadata
    assert metadata["group:gardlet_family"]["display_name"] == "GUARDLET Market"
    assert "C10A1" not in metadata
    assert "C10C0" not in metadata
    assert "A10N1" not in metadata
    assert "A10N3" not in metadata
    assert "V03G2" not in metadata
    assert "V03G2" in group_map["dropped_atc4_csd_missing"]
    assert "C10A1" not in group_map["dropped_atc4_csd_missing"]
    assert group_map["csd_markets_without_keyword_data"] == ["FOSRENOL Market"]


def test_build_market_samples_uses_final_mi_master_market_scopes_only() -> None:
    rows = [_row(i, "C10A1", "LIVALO") for i in range(6)]
    rows += [_row(100 + i, "C10C0", "LIVALOZET") for i in range(6)]
    rows += [_row(200 + i, "A10N1", "GUARDLET") for i in range(6)]
    rows += [_row(300 + i, "A10N3", "GUARDMET") for i in range(6)]
    group_map = apply_csd_market_names(
        build_market_group_map(expected_markets()),
        {
            "atc4_map": {
                "C10A1": {"csd_market": "LIVALO Market", "csd_market_missing": False, "csd_market_candidates": []},
                "C10C0": {"csd_market": "LIVALOZET Market", "csd_market_missing": False, "csd_market_candidates": []},
                "A10N1": {"csd_market": "GUARDLET Market", "csd_market_missing": False, "csd_market_candidates": []},
                "A10N3": {"csd_market": "GUARDLET Market", "csd_market_missing": False, "csd_market_candidates": []},
            },
            "csd_market_missing_atc4": [],
        },
    )

    samples = build_market_samples(
        rows,
        markets=expected_markets(),
        descriptions={},
        axis_per_brand=4,
        axis_rows_cap=20,
        brand_rows=3,
        brands_per_market=2,
        group_map=group_map,
    )

    assert "group:livalo_family" in samples["axis_samples"]
    assert "C10A1" not in samples["axis_samples"]
    assert "C10C0" not in samples["axis_samples"]
    assert "A10N1" not in samples["axis_samples"]
    assert "A10N3" not in samples["axis_samples"]
    assert "group:livalo_family:C10A1:LIVALO" in samples["brand_samples"]
    assert "group:gardlet_family:A10N3:GUARDMET" in samples["brand_samples"]
    assert samples["scope_metadata"]["group:livalo_family"]["display_name"] == "LIVALO+LIVALOZET Market"


def test_build_market_samples_full_rows_removes_axis_and_brand_caps() -> None:
    rows = [_row(i, "C10C0", "LIVALOZET") for i in range(20)]
    rows += [_row(100 + i, "C10C0", "ATOZET") for i in range(18)]

    samples = build_market_samples(
        rows,
        markets=("C10C0",),
        descriptions={},
        axis_per_brand=3,
        axis_rows_cap=5,
        brand_rows=4,
        brands_per_market=2,
        full_rows=True,
    )

    assert len(samples["axis_samples"]["C10C0"]) == 38
    assert len(samples["brand_samples"]["C10C0:LIVALOZET"]) == 20
    assert samples["sample_summary"]["mode"] == "full_rows"


def test_apply_csd_market_names_uses_english_group_union_and_flags_missing() -> None:
    group_map = build_market_group_map(expected_markets())
    bridge = {
        "atc4_map": {
            "C10A1": {"csd_market": "LIVALO Market", "csd_market_missing": False, "csd_market_candidates": []},
            "C10C0": {"csd_market": "LIVALOZET Market", "csd_market_missing": False, "csd_market_candidates": []},
            "A10N1": {"csd_market": "GUARDLET Market", "csd_market_missing": False, "csd_market_candidates": []},
            "A10N3": {"csd_market": "GUARDLET Market", "csd_market_missing": False, "csd_market_candidates": []},
            "V03G2": {"csd_market": None, "csd_market_missing": True, "csd_market_candidates": []},
        },
        "csd_market_missing_atc4": ["V03G2"],
    }

    renamed = apply_csd_market_names(group_map, bridge)

    assert renamed["atc4_map"]["C10A1"]["friendly_name"] == "LIVALO Market"
    assert renamed["atc4_map"]["C10C0"]["group_name"] == "LIVALO+LIVALOZET Market"
    assert renamed["atc4_map"]["V03G2"]["csd_market_missing"] is True
    assert "V03G2" in renamed["csd_market_missing_atc4"]


def test_chunk_rows_by_token_budget_keeps_calls_bounded() -> None:
    rows = [_row(i, "A02B2", "JAQBO", text="x" * 220) for i in range(5)]

    chunks = chunk_rows_by_token_budget(rows, token_budget=200)

    assert [len(chunk) for chunk in chunks] == [2, 2, 1]


def test_response_normalization_and_guard_accepts_valid_share() -> None:
    topics = [TopicDefinition("T1", "효능", "효능 메시지", ("LDL",)), TopicDefinition("T2", "안전성", "안전성 메시지", ("당뇨",))]
    payload = {"topic_shares": [{"topic_id": "T1", "label": "효능", "affected_row_count": 12}, {"topic_id": "T2", "label": "안전성", "affected_row_count": 5}]}

    normalized = normalize_share_payload(payload, brand="LIVALOZET", atc4="C10C0", scope_id="atc4:C10C0", axis_version="v1", row_count=20)
    guard = mechanical_guard(normalized, valid_topic_ids={"T1", "T2"}, brand_total_rows=20)

    assert "etc_pct" not in normalized
    assert normalized["topic_shares"][0]["share_pct"] == 60.0
    assert normalized["topic_shares"][1]["share_pct"] == 25.0
    assert guard["status"] == "pass"


def test_share_normalization_backfills_missing_topic_id_from_axis_label() -> None:
    topics = [TopicDefinition("T1", "효능", "효능 메시지", ("LDL",)), TopicDefinition("T2", "안전성", "안전성 메시지", ("당뇨",))]
    payload = {
        "topic_shares": [
            {"topic_id": None, "label": " 효능 ", "affected_row_count": 12},
            {"topic_id": "", "label": "안 전 성", "affected_row_count": 5},
        ],
    }

    normalized = normalize_share_payload(
        payload,
        brand="LIVALOZET",
        atc4="C10C0",
        scope_id="atc4:C10C0",
        axis_version="v1",
        row_count=20,
        axis_topics=topics,
    )
    guard = mechanical_guard(normalized, valid_topic_ids={"T1", "T2"}, brand_total_rows=20)

    assert [row["topic_id"] for row in normalized["topic_shares"]] == ["T1", "T2"]
    assert normalized["topic_id_backfill_count"] == 2
    assert guard["status"] == "pass"


def test_share_normalization_keeps_unmatched_missing_topic_id_unknown() -> None:
    topics = [TopicDefinition("T1", "효능", "효능 메시지", ("LDL",))]
    payload = {"topic_shares": [{"topic_id": "", "label": "미정 토픽", "affected_row_count": 16}]}

    normalized = normalize_share_payload(
        payload,
        brand="LIVALOZET",
        atc4="C10C0",
        scope_id="atc4:C10C0",
        axis_version="v1",
        row_count=20,
        axis_topics=topics,
    )
    guard = mechanical_guard(normalized, valid_topic_ids={"T1"}, brand_total_rows=20)

    assert normalized["topic_shares"][0]["topic_id"] == ""
    assert normalized["topic_id_backfill_count"] == 0
    assert normalized["unmatched_missing_topic_labels"] == ["미정 토픽"]
    assert guard["status"] == "fail"
    assert "unknown_topic_id" in guard["reasons"]


def test_axis_normalization_caps_topics_at_seven() -> None:
    payload = {
        "topics": [
            {"topic_id": f"T{index}", "label": f"토픽 {index}", "definition": "한 의미", "keywords": []}
            for index in range(1, 10)
        ]
    }

    normalized = normalize_axis_payload(payload, scope_id="atc4:A02B2", fallback_label="PPI")

    assert normalized["status"] == "ok"
    assert len(normalized["topics"]) == 7


def test_share_normalization_adds_brand_specific_topics_without_etc() -> None:
    payload = {
        "topic_shares": [{"topic_id": "T1", "label": "시장 효능", "affected_row_count": 55}],
        "brand_specific_topics": [
            {"topic_id": "B1", "label": "브랜드 특화 근거", "affected_row_count": 20},
            {"topic_id": "B2", "label": "브랜드 특화 편의", "affected_row_count": 10},
            {"topic_id": "B3", "label": "초과 특화", "affected_row_count": 9},
        ],
    }

    normalized = normalize_share_payload(
        payload,
        brand="WINUF",
        atc4="K01D2",
        scope_id="atc4:K01D2",
        axis_version="v1",
        row_count=100,
    )
    guard = mechanical_guard(normalized, valid_topic_ids={"T1"}, brand_total_rows=100)

    assert len(normalized["brand_specific_topics"]) == 2
    assert "etc_pct" not in normalized
    assert sum(float(row["share_pct"]) for row in [*normalized["topic_shares"], *normalized["brand_specific_topics"]]) == 85.0
    assert guard["status"] == "pass"


def test_share_normalization_merges_near_duplicate_brand_specific_topics() -> None:
    payload = {
        "topic_shares": [{"topic_id": "T1", "label": "시장 효능", "affected_row_count": 55}],
        "brand_specific_topics": [
            {"topic_id": "B1", "label": "국산 신약 브랜드 가치", "affected_row_count": 12},
            {"topic_id": "B2", "label": "국산 신약 가치", "affected_row_count": 8},
            {"topic_id": "B3", "label": "제형 편의", "affected_row_count": 7},
        ],
    }

    normalized = normalize_share_payload(
        payload,
        brand="JAQBO",
        atc4="L04D0",
        scope_id="atc4:L04D0",
        axis_version="v1",
        row_count=100,
    )

    assert [row["label"] for row in normalized["brand_specific_topics"]] == ["국산 신약 브랜드 가치", "제형 편의"]
    assert normalized["brand_specific_topics"][0]["affected_row_count"] == 20
    assert normalized["brand_specific_topics"][0]["share_pct"] == 20.0
    assert normalized["brand_specific_dedup_count"] == 1
    assert normalized["brand_specific_dedup_log"][0]["dropped_label"] == "국산 신약 가치"


def test_label_quality_summary_counts_complex_labels_and_brand_specific_duplicates() -> None:
    axis_results = {
        "A": {
            "topics": [
                {"topic_id": "T1", "label": "질환 치료 및 적응증"},
                {"topic_id": "T2", "label": "안전성"},
            ]
        }
    }
    brand_results = {
        "A:BRAND": {
            "topic_shares": [{"topic_id": "T1", "label": "질환 치료 및 적응증", "share_pct": 50.0}],
            "brand_specific_topics": [
                {"topic_id": "B1", "label": "국산 신약 브랜드 가치", "share_pct": 10.0},
                {"topic_id": "B2", "label": "국산 신약 가치", "share_pct": 8.0},
            ],
        }
    }

    summary = label_quality_summary(axis_results, brand_results)

    assert summary["complex_label_count"] == 2
    assert summary["brand_specific_duplicate_pair_count"] == 1


def test_mechanical_guard_rejects_unknown_topic_and_bad_affected_count() -> None:
    payload = {
        "status": "ok",
        "row_count": 10,
        "topic_shares": [{"topic_id": "T999", "label": "환각", "share_pct": 120.0, "affected_row_count": 12}],
    }

    guard = mechanical_guard(payload, valid_topic_ids={"T1"}, brand_total_rows=10)

    assert guard["status"] == "fail"
    assert "unknown_topic_id" in guard["reasons"]
    assert "share_pct_out_of_bounds" in guard["reasons"]
    assert "affected_row_count_out_of_bounds" in guard["reasons"]


def test_stabilize_axis_keeps_previous_when_similarity_is_high() -> None:
    previous = normalize_axis_payload(
        {
            "axis_version": "C10C0_v3",
            "topics": [
                {"topic_id": "T1", "label": "LDL-C 강하", "definition": "지질 개선", "keywords": ["LDL", "강하"]},
                {"topic_id": "T2", "label": "당뇨 안전성", "definition": "혈당 안전성", "keywords": ["당뇨", "혈당"]},
            ],
        },
        scope_id="atc4:C10C0",
        fallback_label="C10C0",
        minimum_topics=1,
    )
    new = normalize_axis_payload(
        {
            "axis_version": "draft",
            "topics": [
                {"topic_id": "N1", "label": "LDL 강하 효능", "definition": "지질 개선", "keywords": ["LDL", "강하"]},
                {"topic_id": "N2", "label": "당뇨병 안전성", "definition": "혈당 안전성", "keywords": ["당뇨", "혈당"]},
            ],
        },
        scope_id="atc4:C10C0",
        fallback_label="C10C0",
        minimum_topics=1,
    )

    result = stabilize_axis(previous, new, threshold=0.45)

    assert result["stability"]["action"] == "keep"
    assert result["axis_version"] == "C10C0_v3"
    assert result["stability"]["similarity"] >= 0.45


def test_dictionary_cross_check_flags_large_top_topic_mismatch() -> None:
    share_payload = {"topic_shares": [{"label": "효능", "affected_row_count": 14, "share_pct": 70.0}]}
    dict_payload = {"topics": [{"label": "안전성", "share_pct": 80.0}]}

    result = dictionary_cross_check(share_payload, dict_payload, min_overlap=0.2)

    assert result["status"] == "flag"
    assert result["layer"] == "dict_xcheck"


def test_quality_summary_counts_grades_without_etc_average() -> None:
    axis_results = {
        "A": {"status": "ok", "scope_id": "atc4:A", "topics": [{"topic_id": "T1", "label": "효능"}, {"topic_id": "T2", "label": "안전성"}, {"topic_id": "T3", "label": "편의"}, {"topic_id": "T4", "label": "근거"}, {"topic_id": "T5", "label": "기타"}]},
        "B": {"status": "ok", "scope_id": "atc4:B", "topics": [{"topic_id": "T1", "label": "효능"} for _ in range(5)]},
    }
    brand_results = {
        "A:BRAND1": {"atc4": "A", "topic_shares": [{"topic_id": "T1", "label": "효능", "affected_row_count": 90, "share_pct": 90.0}], "qc": {"guard": {"status": "pass"}}},
        "B:BRAND2": {"atc4": "B", "topic_shares": [], "qc": {"guard": {"status": "pass"}}},
    }

    summary = quality_summary(axis_results, brand_results, large_markets=("A",))

    assert summary["grade_distribution"]["A"] == 1
    assert summary["grade_distribution"]["C"] == 1
    assert "average_etc_pct" not in summary


def test_auto_topic_html_uses_embedded_measured_json(tmp_path: Path) -> None:
    from pipeline.scripts.analysis.brand_activity.auto_topic.viz import render_html

    html = render_html({"markets": [{"atc4": "C10C0", "quality_grade": "A"}], "brand_results": [{"brand": "LIVALOZET", "row_count": 20, "topic_shares": [{"label": "효능", "affected_row_count": 10, "share_pct": 50.0}]}], "models": ["flash"]})
    output = tmp_path / "viz.html"
    output.write_text(html, encoding="utf-8")

    assert "AUTO_TOPIC_DATA" in output.read_text(encoding="utf-8")
    assert "LIVALOZET" in html
    assert "placeholder" not in html.lower()
    assert json.loads(html.split('<script id="AUTO_TOPIC_DATA" type="application/json">', 1)[1].split("</script>", 1)[0])["models"] == ["flash"]
