from __future__ import annotations

from pathlib import Path


def test_stage_script_emits_machine_readable_four_part_gate() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "pipeline/scripts/crawler/crawl_chain_steps.sh").read_text(encoding="utf-8")

    assert "stage_gate.json" in script
    assert '"exit_code"' in script
    assert '"failures"' in script
    assert '"events_raw_gap"' in script
    assert '"pending_gap"' in script
    assert "gate-status" in script


def test_final_stage_preserves_each_machine_readable_summary() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "pipeline/scripts/crawler/crawl_chain_steps.sh").read_text(encoding="utf-8")

    assert "sync_summary.json" in script
    assert "append_summary.json" in script
    assert "refresh_summary.json" in script
    assert "classification_summary.json" in script


def test_loader_partial_failures_and_global_gaps_are_fail_closed() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "pipeline/scripts/crawler/crawl_chain_steps.sh").read_text(encoding="utf-8")

    assert '("failures", "error_count")' in script
    assert 'payload.get("status") == "partial"' in script
    assert 'payload.get("errors")' in script
    assert '"${output}/load_summary.json"' in script
    assert 'json.load(open(sys.argv[1]))["events_raw_gap"]' in script
    assert 'json.load(open(sys.argv[1]))["pending_gap"]' in script


def test_stage_script_uses_configured_repo_root_for_loader_imports() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "pipeline/scripts/crawler/crawl_chain_steps.sh").read_text(encoding="utf-8")

    assert 'REPO_ROOT="${repo_root}"' in script
    assert 'Path(os.environ["REPO_ROOT"]) / "crawl" / "agent1"' in script
    assert 'sys.path[:0] = ["/app/crawl/agent1"]' not in script
