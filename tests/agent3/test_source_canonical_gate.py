from __future__ import annotations

import pytest

from pipeline.scripts.agent3.run_source import (
    ExecutionContractError,
    SourceCoverage,
    _should_reestablish_revision,
    _validate_execution_contract,
    _iter_identity_inputs,
    _verify_existing_market_positions,
    run_source,
    validate_source_coverage,
)
from pipeline.scripts.agent3.source_loader import (
    Agent3SourceRecord,
    ExistingAgent3SourceState,
    HashMigrationSourceState,
    canonical_content_matches,
)


def test_canonical_content_match_ignores_hash_and_revision() -> None:
    old = ExistingAgent3SourceState(
        input_hash="old-hash",
        workflow_rev=5365,
        profile_json={"brand": "A"},
        strength_candidates_json=[{"metric": "market_position", "value": 1}],
        strength_summary_json={"strength_items": [{"metric": "market_position"}]},
    )
    record = Agent3SourceRecord(
        brand_key="A",
        source="iqvia",
        brand_name="A",
        serving_brand_name="A",
        profile_json={"brand": "A"},
        strength_candidates_json=[{"value": 1, "metric": "market_position"}],
        strength_summary_json={"strength_items": [{"metric": "market_position"}]},
        workflow_id=316,
        workflow_rev=5692,
        input_hash="new-hash",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    assert canonical_content_matches(old, record)


def test_canonical_content_mismatch_requires_update() -> None:
    old = ExistingAgent3SourceState(
        input_hash="old",
        workflow_rev=5365,
        profile_json={"brand": "A"},
        strength_candidates_json=[],
        strength_summary_json={"strength_items": []},
    )
    record = Agent3SourceRecord(
        brand_key="A",
        source="iqvia",
        brand_name="A",
        serving_brand_name="A",
        profile_json={"brand": "A"},
        strength_candidates_json=[{"metric": "market_position"}],
        strength_summary_json={"strength_items": [{"metric": "market_position"}]},
        workflow_id=316,
        workflow_rev=5692,
        input_hash="new",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    assert not canonical_content_matches(old, record)


def test_pre_refresh_coverage_accepts_current_canonical_counts() -> None:
    validate_source_coverage(SourceCoverage(source_units=35_521, brands=24_789, profile_only=0))


@pytest.mark.parametrize(
    "coverage",
    [
        SourceCoverage(source_units=35_520, brands=24_789, profile_only=0),
        SourceCoverage(source_units=35_521, brands=24_788, profile_only=0),
        SourceCoverage(source_units=35_521, brands=24_789, profile_only=1),
    ],
)
def test_pre_refresh_coverage_rejects_incomplete_source(coverage: SourceCoverage) -> None:
    with pytest.raises(RuntimeError, match="coverage gate"):
        validate_source_coverage(coverage)


def test_execution_contract_rejects_revision_mismatch() -> None:
    with pytest.raises(ExecutionContractError, match="workflow revision mismatch"):
        _validate_execution_contract(
            workflow_rev=5365,
            expected_workflow_rev=5692,
            cli_mode="full",
            environment_mode=None,
        )


def test_execution_contract_rejects_duplicate_mode_configuration() -> None:
    with pytest.raises(ExecutionContractError, match="AGENT3_MODE must be unset"):
        _validate_execution_contract(
            workflow_rev=5692,
            expected_workflow_rev=5692,
            cli_mode="full",
            environment_mode="dry-run",
        )


def test_revision_mismatch_aborts_before_repository_or_write(monkeypatch, tmp_path) -> None:
    def unexpected_repository(*_args, **_kwargs):
        raise AssertionError("revision assertion must run before repository construction")

    monkeypatch.setattr("pipeline.scripts.agent3.run_source.Agent3Repository", unexpected_repository)
    monkeypatch.setattr("pipeline.scripts.agent3.run_source.Agent3SourceLoader", unexpected_repository)

    with pytest.raises(ExecutionContractError, match="workflow revision mismatch"):
        run_source(
            brand_source="general_all",
            mode="full",
            source_selection="all",
            explicit_brands=None,
            output=tmp_path / "result.json",
            top_n=5,
            workflow_rev=5365,
            expected_workflow_rev=5692,
            environment_mode=None,
        )


def test_revision_reestablishment_is_explicit_and_only_for_stale_lineage() -> None:
    old = ExistingAgent3SourceState(
        input_hash="old-revision-hash",
        workflow_rev=5365,
        profile_json={"brand": "A"},
        strength_candidates_json=[],
        strength_summary_json={"strength_items": []},
    )

    assert _should_reestablish_revision(old, workflow_rev=5692, enabled=True)
    assert not _should_reestablish_revision(old, workflow_rev=5692, enabled=False)
    assert not _should_reestablish_revision(old, workflow_rev=5365, enabled=True)


def test_manifests_use_source_aware_runner() -> None:
    from pathlib import Path

    images: set[str] = set()
    for name in ("agent3-full-job.yaml", "agent3-refresh-cronjob.yaml"):
        text = (Path("deploy/k8s/agent3") / name).read_text(encoding="utf-8")
        assert "pipeline.scripts.agent3.run_source" in text
        assert "pipeline.scripts.agent3.run_full" not in text
        assert "--brand-source general_all" in text or '${AGENT3_BRAND_SOURCE}' in text
        assert "--source all" in text
        assert 'name: AGENT3_WORKFLOW_REV' in text
        assert 'value: "5692"' in text
        assert "--expected-workflow-rev 5692" in text
        assert "AGENT3_MODE" not in text
        images.add(next(line.split("image:", 1)[1].strip() for line in text.splitlines() if "image:" in line))

    assert len(images) == 1
    assert "@sha256:" in next(iter(images))


def test_api_image_contains_canonical_agent3_runtime() -> None:
    from pathlib import Path

    text = Path("api/Dockerfile").read_text(encoding="utf-8")

    assert "COPY pipeline/scripts/agent3 /app/pipeline/scripts/agent3" in text
    assert (
        "COPY pipeline/scripts/ai_analysis/bundle_builder "
        "/app/pipeline/scripts/ai_analysis/bundle_builder"
    ) in text


def test_identity_inputs_are_loaded_in_bounded_batches() -> None:
    identities = [type("Identity", (), {"brand_key": str(i), "brand_name": f"B{i}"})() for i in range(5)]
    seen_sizes: list[int] = []

    class Repo:
        def load_general_rows_for_brands(self, keys):
            seen_sizes.append(len(keys))
            return {key: [] for key in keys}

        def load_strategic_rows_for_brands(self, keys):
            return {key: [] for key in keys}

        def load_molecule_rows_for_brands(self, names):
            return {name: [] for name in names}

        def load_market_metric_rows(self, rows):
            return rows

    class Loader:
        def load_existing_hashes(self, keys):
            assert len(keys) <= 2
            return {}

    rows = list(_iter_identity_inputs(Repo(), Loader(), identities, load_existing=True, batch_size=2))

    assert len(rows) == 5
    assert seen_sizes == [2, 2, 1]


def test_verify_existing_market_positions_skips_equal_content_without_write(tmp_path) -> None:
    state = HashMigrationSourceState(
        brand_key="A",
        source="iqvia",
        brand_name="A",
        serving_brand_name="A",
        profile_json={"brand": "A"},
        strength_candidates_json=[{"metric": "market_position"}],
        strength_summary_json={"strength_items": [{"metric": "market_position"}]},
        workflow_id=316,
        workflow_rev=5365,
        input_hash="historical",
    )

    class Loader:
        def iter_market_position_states(self):
            yield state

        def upsert_many(self, *_args, **_kwargs):
            raise AssertionError("verify mode must not write")

    result = _verify_existing_market_positions(
        Loader(), workflow_rev=5692, output=tmp_path / "verify.json"
    )

    assert result["skipped_same_content"] == 1
    assert result["canonical_mismatch"] == 0
    assert result["workflow_calls"] == 0
    assert result["affected"] == 0
