from dataclasses import replace
from datetime import datetime

from bundle_builder import build_brand_bundle

from .conftest import KST


def test_same_input_same_hash(db_conn, config):
    snapshot = datetime(2026, 5, 24, 8, 0, tzinfo=KST)
    bundle1 = build_brand_bundle("리바로", snapshot, config, db_conn)
    bundle2 = build_brand_bundle("리바로", snapshot, config, db_conn)
    assert bundle1["bundle_meta"]["bundle_hash"] == bundle2["bundle_meta"]["bundle_hash"]


def test_different_snapshot_records_different_snapshot(db_conn, config):
    snapshot1 = datetime(2026, 5, 24, 8, 0, tzinfo=KST)
    snapshot2 = datetime(2026, 5, 24, 9, 0, tzinfo=KST)
    bundle1 = build_brand_bundle("리바로", snapshot1, config, db_conn)
    bundle2 = build_brand_bundle("리바로", snapshot2, config, db_conn)
    assert bundle1["bundle_meta"]["snapshot_at"] != bundle2["bundle_meta"]["snapshot_at"]


def test_config_version_changes_hash(db_conn, config):
    snapshot = datetime(2026, 5, 24, 8, 0, tzinfo=KST)
    config2 = replace(config, config_version="phase_zeta_v2_test")
    bundle1 = build_brand_bundle("리바로", snapshot, config, db_conn)
    bundle2 = build_brand_bundle("리바로", snapshot, config2, db_conn)
    assert bundle1["bundle_meta"]["bundle_hash"] != bundle2["bundle_meta"]["bundle_hash"]
