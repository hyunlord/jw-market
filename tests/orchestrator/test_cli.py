import json
from pathlib import Path

import pytest

from pipeline.orchestrator import cli
from pipeline.orchestrator.stages import AGENT3_EXPECTED_REV_ENV, STAGE_BY_KEY, STAGE_ORDER

from fakes import FakeProbe


def test_materialize_full_inputs_forwards_pinned_sidecar(
    tmp_path, monkeypatch, capsys
):
    captured = {}

    def fake_materialize_full_inputs(**kwargs):
        captured.update(kwargs)
        manifest = tmp_path / "input_manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        return manifest

    monkeypatch.setattr(
        "pipeline.orchestrator.full_rehearsal_inputs.materialize_full_inputs",
        fake_materialize_full_inputs,
    )
    source = tmp_path / "may.parquet"

    assert cli.main(
        [
            "materialize-full-inputs",
            "--output-root",
            str(tmp_path / "inputs"),
            "--ubist-parquet-sidecar",
            str(source),
            "year=2026/month=05/data.parquet",
            "a" * 64,
        ]
    ) == 0

    sidecar = captured["ubist_parquet_sidecars"][0]
    assert sidecar.source == source
    assert sidecar.relative_path == Path("year=2026/month=05/data.parquet")
    assert sidecar.sha256 == "a" * 64
    assert capsys.readouterr().out.strip().endswith("input_manifest.json")


def test_stages_subcommand_prints_incremental_table(capsys):
    assert cli.main(["stages"]) == 0
    table = json.loads(capsys.readouterr().out)
    assert [row["stage"] for row in table["stages"]] == list(STAGE_ORDER)
    kinds = {row["stage"]: row["incremental"] for row in table["stages"]}
    assert kinds == {
        "cache": "new_brands",
        "forecast": "market_epoch",
        "strength": "native_hash",
        "shortlong": "native_hash",
        "events": "full_only",
        "elements": "new_brands",
    }
    # full-only 계열은 사유가 함께 정직 표기되어야 한다.
    assert "full-only" in STAGE_BY_KEY["events"].incremental_reason
    assert "full-only" in STAGE_BY_KEY["forecast"].incremental_reason


def test_run_dry_run_via_cli_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("pipeline.orchestrator.probe.MartProbe", lambda: FakeProbe())
    state_file = tmp_path / "state.json"

    exit_code = cli.main(
        ["run", "--mode", "full", "--dry-run", "--state-file", str(state_file), "--run-id", "cli-test"]
    )

    assert exit_code == 0
    assert not state_file.exists()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["event"] == "plan"
    assert lines[-1]["event"] == "dry_run_end"


def test_run_rejects_conflicting_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("pipeline.orchestrator.probe.MartProbe", lambda: FakeProbe())

    exit_code = cli.main(
        ["run", "--stages", "cache", "--from-stage", "events", "--dry-run", "--state-file", str(tmp_path / "s.json")]
    )

    assert exit_code == 2


def test_strength_missing_rev_env_is_surfaced(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("pipeline.orchestrator.probe.MartProbe", lambda: FakeProbe())
    monkeypatch.delenv("AGENT3_WORKFLOW_REV", raising=False)
    monkeypatch.delenv(AGENT3_EXPECTED_REV_ENV, raising=False)

    exit_code = cli.main(
        ["run", "--dry-run", "--state-file", str(tmp_path / "s.json"), "--run-id", "cli-test"]
    )

    assert exit_code == 0
    plan_line = json.loads(capsys.readouterr().out.splitlines()[0])
    assert any("AGENT3" in warning for warning in plan_line["warnings"])


@pytest.mark.parametrize("stage_key", list(STAGE_ORDER))
def test_every_stage_declares_honest_incremental_contract(stage_key):
    spec = STAGE_BY_KEY[stage_key]
    assert spec.incremental in ("native_hash", "new_brands", "market_epoch", "full_only")
    assert spec.incremental_reason
    if spec.incremental == "new_brands":
        assert spec.universe_sql and spec.covered_sql
    else:
        assert spec.universe_sql is None and spec.covered_sql is None
