from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.phase0b_characterization import (
    CharacterizationMismatch,
    MissingCassetteError,
    ReplayCassette,
    SNAPSHOT_FIELDS,
    compare_snapshot,
    fingerprint,
    snapshot_from_observation,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _read_json(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_corpus_preserves_all_required_sources_and_lane_coverage() -> None:
    corpus = _read_json("corpus.v1.json")
    assert isinstance(corpus, dict)
    assert corpus["source_counts"] == {
        "dead_end_inventory_r8": 102,
        "r3_live_gate": 69,
        "representative": 10,
    }
    assert corpus["deduplicated_question_count"] == len(corpus["cases"])
    assert corpus["deduplicated_question_count"] >= 102

    lanes = {lane for case in corpus["cases"] for lane in case["lanes"]}
    assert lanes == {
        "clinical",
        "file",
        "market",
        "mixed",
        "multiturn",
        "regulatory",
        "typed",
    }
    assert corpus["coverage_gaps"] == []


def test_replay_is_exact_and_missing_calls_fail_closed() -> None:
    cassette = ReplayCassette.from_path(FIXTURES / "external_calls.v1.json")
    entry = cassette.entries[0]

    assert cassette.replay(entry.dependency, entry.operation, entry.request) == entry.response
    with pytest.raises(MissingCassetteError, match="no cassette entry"):
        cassette.replay("mcp", "not_recorded", {"query": "must not go live"})
    assert cassette.live_fallback_attempts == 0


def test_same_cassette_is_deterministic_across_three_runs() -> None:
    cassette = ReplayCassette.from_path(FIXTURES / "external_calls.v1.json")
    entry = cassette.entries[0]

    results = [cassette.replay(entry.dependency, entry.operation, entry.request) for _ in range(3)]
    assert results[0] == results[1] == results[2]


def test_cassette_response_mutation_is_detected(tmp_path: Path) -> None:
    expected = _read_json("representative_snapshot.v1.json")
    assert isinstance(expected, dict)
    cassette_payload = _read_json("external_calls.v1.json")
    assert isinstance(cassette_payload, dict)
    final_entry = next(
        entry
        for entry in cassette_payload["entries"]
        if entry["dependency"] == "genos" and entry["operation"] == "final"
    )
    final_entry["response"]["text"] += " [mutated cassette response]"
    final_entry["response_sha256"] = fingerprint(final_entry["response"])
    mutated_path = tmp_path / "mutated-cassette.json"
    mutated_path.write_text(json.dumps(cassette_payload, ensure_ascii=False), encoding="utf-8")

    cassette = ReplayCassette.from_path(mutated_path)
    replayed = cassette.replay("genos", "final", final_entry["request"])
    actual = snapshot_from_observation({**expected, "final_answer": replayed["text"]})

    with pytest.raises(CharacterizationMismatch, match="final_answer"):
        compare_snapshot(expected, actual)


def test_route_mutation_is_detected_even_when_final_answer_is_identical() -> None:
    expected = _read_json("representative_snapshot.v1.json")
    assert isinstance(expected, dict)
    changed = dict(expected)
    changed["router_planner"] = "forced-different-planner"
    actual = snapshot_from_observation(changed)

    assert actual["final_answer"] == expected["final_answer"]
    with pytest.raises(CharacterizationMismatch, match="router_planner"):
        compare_snapshot(expected, actual)


def test_snapshots_include_path_and_failure_contract_fields() -> None:
    expected = _read_json("representative_snapshot.v1.json")
    assert isinstance(expected, dict)
    assert set(expected) == {
        "case_id",
        "question",
        "router_planner",
        "tool_call_fingerprints",
        "evidence_facts",
        "gate_decisions",
        "disposition",
        "failure_kind",
        "reason_codes",
        "final_answer",
        "final_answer_sha256",
    }
    assert expected["router_planner"]
    assert expected["final_answer_sha256"]


def test_every_corpus_source_case_has_a_valid_snapshot() -> None:
    corpus = _read_json("corpus.v1.json")
    snapshot_bundle = _read_json("observed_snapshots.v1.json")
    assert isinstance(corpus, dict)
    assert isinstance(snapshot_bundle, dict)
    snapshots = snapshot_bundle["snapshots"]
    assert len(snapshots) == 181
    assert snapshot_bundle["snapshot_count"] == len(snapshots)

    snapshots_by_id = {snapshot["case_id"]: snapshot for snapshot in snapshots}
    source_ids = {
        f"{source_name}:{source_id}"
        for case in corpus["cases"]
        for source_name, ids in case["source_ids"].items()
        for source_id in ids
    }
    assert source_ids == set(snapshots_by_id)
    for snapshot in snapshots:
        assert set(snapshot) == set(SNAPSHOT_FIELDS)
        assert snapshot_from_observation(snapshot) == snapshot


def test_cassettes_cover_each_required_external_dependency() -> None:
    cassette = ReplayCassette.from_path(FIXTURES / "external_calls.v1.json")
    assert {entry.dependency for entry in cassette.entries} == {
        "file_search",
        "genos",
        "mcp",
        "search_news",
        "web_search",
    }
    assert {entry.operation for entry in cassette.entries if entry.dependency == "mcp"} == {
        "clinicaltrials_study_details",
        "clinicaltrials_v2_search",
        "hira_disease_area_stats",
        "hira_disease_gender_age_stats",
        "hira_disease_hospitalization_outpatient_stats",
        "hira_disease_institution_class_stats",
        "hira_disease_name_code",
        "hira_procedure_area_stats",
        "hira_procedure_gender_age_stats",
        "hira_procedure_gender_ipat_opat_stats",
        "hira_procedure_institution_class_stats",
        "mfds_clinical_trial_kr",
        "mfds_composition",
        "mfds_easy_drug",
        "mfds_fda_orangebook",
        "mfds_main_ingredient",
        "mfds_patent",
        "mfds_permission_detail",
        "mfds_permission_search",
        "openfda_label_search",
    }
