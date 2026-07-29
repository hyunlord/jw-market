from bundle_builder import BundleConfig
from phase_zeta_runner.config import RunnerConfig


def test_runtime_db_configs_override_pinned_localhost_for_both_connections(
    monkeypatch,
    config_v1_1_path,
):
    # Given: both checked-in Agent2 configs pin localhost.
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_PORT", "3307")
    monkeypatch.setenv("DB_NAME", "jw_mart_runtime")

    # When: both configs are loaded under the Job runtime DB contract.
    bundle_config = BundleConfig.from_yaml(str(config_v1_1_path))
    runner_config = RunnerConfig.from_yaml(
        str(config_v1_1_path.parent / "genos_runner_v1.yaml")
    )

    # Then: neither real connection can retain the pinned localhost target.
    assert bundle_config.db.host == "db.internal"
    assert bundle_config.db.port == 3307
    assert bundle_config.db.database == "jw_mart_runtime"
    assert runner_config.composer.db_host == "db.internal"
    assert runner_config.composer.db_port == 3307
    assert runner_config.composer.db_name == "jw_mart_runtime"
