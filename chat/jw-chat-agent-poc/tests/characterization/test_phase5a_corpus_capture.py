from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.phase5a_routing_input_capture import capture_corpus


FIXTURES = Path(__file__).parent / "fixtures"
CORPUS_V1 = FIXTURES / "corpus.v1.json"
ROUTING_INPUTS_LATEST = FIXTURES / "routing_inputs.v3.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_capture_preserves_v1_and_accounts_for_all_route_point_cells() -> None:
    payload = _read_json(ROUTING_INPUTS_LATEST)
    corpus_sha256 = hashlib.sha256(CORPUS_V1.read_bytes()).hexdigest()

    assert payload["source_corpus"] == "corpus.v1.json"
    assert payload["source_corpus_sha256"] == corpus_sha256
    assert payload["case_count"] == 128
    assert payload["route_point_count"] == 4
    assert payload["cell_count"] == 512
    assert sum(payload["comparison_totals"].values()) == 512


def test_capture_distinguishes_unfired_from_missing_input() -> None:
    payload = _read_json(ROUTING_INPUTS_LATEST)
    statuses = {
        point["capture_status"]
        for case in payload["cases"]
        for point in case["route_points"].values()
    }

    assert "unfired" in statuses
    assert "missing_input" in statuses
    assert "captured" in statuses


def test_capture_is_deterministic_across_two_local_runs(monkeypatch) -> None:
    # v3 is regenerated only when an accepted routing contract changes. The
    # active-file MIXED contract intentionally changes four previously unfired
    # app-scope cells while preserving deterministic local capture.
    monkeypatch.setenv("JW_CHAT_ROUTER_CUTOVER_HIRA_REIMBURSEMENT", "0")
    monkeypatch.setenv("JW_CHAT_ROUTER_CUTOVER_HIRA_DISEASE_STATS", "0")
    monkeypatch.setenv("JW_CHAT_ROUTER_CUTOVER_MFDS", "0")
    monkeypatch.setenv("JW_CHAT_ROUTER_CUTOVER_CLINICAL_TRIALS", "0")
    first = capture_corpus(CORPUS_V1, FIXTURES / "observed_snapshots.v1.json")
    second = capture_corpus(CORPUS_V1, FIXTURES / "observed_snapshots.v1.json")

    assert first == second == _read_json(ROUTING_INPUTS_LATEST)


def test_capture_contains_no_credential_values_or_live_fallback() -> None:
    payload = _read_json(ROUTING_INPUTS_LATEST)
    serialized = json.dumps(payload, ensure_ascii=False).casefold()

    assert payload["capture_environment"] == {
        "cassette_entry_count": 25,
        "database_writes": 0,
        "external_calls": 0,
        "external_dependency_mode": "exact_replay_cassette_plus_network_block",
        "live_chat_calls": 0,
        "mode": "local_fixture",
    }
    assert "api_key" not in serialized
    assert "authorization" not in serialized
    assert "bearer " not in serialized


def test_routing_v4_is_captured_and_matches_for_all_128_questions() -> None:
    payload = _read_json(ROUTING_INPUTS_LATEST)
    routing_v4_points = [
        case["route_points"]["routing_v4_rules"] for case in payload["cases"]
    ]

    assert len(routing_v4_points) == 128
    assert all(point["capture_status"] == "captured" for point in routing_v4_points)
    assert all(point["comparison"]["matches"] is True for point in routing_v4_points)
