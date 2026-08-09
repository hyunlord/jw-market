from dataclasses import replace
from datetime import datetime

from bundle_builder import build_brand_bundle
from bundle_builder.hash_util import compute_bundle_hash

from .conftest import KST


def test_same_input_same_hash(db_conn, config):
    snapshot = datetime(2026, 5, 24, 8, 0, tzinfo=KST)
    bundle1 = build_brand_bundle("리바로", snapshot, config, db_conn)
    bundle2 = build_brand_bundle("리바로", snapshot, config, db_conn)
    assert bundle1["bundle_meta"]["bundle_hash"] == bundle2["bundle_meta"]["bundle_hash"]


def test_snapshot_at_is_not_part_of_bundle_hash():
    first = {
        "bundle_meta": {"snapshot_at": "2026-08-08T17:16:02", "brand": "리바로"},
        "rows": [{"snapshot_at": "2026-08-08T17:16:02", "value": 7}],
    }
    second = {
        "bundle_meta": {"snapshot_at": "2026-08-09T17:16:02", "brand": "리바로"},
        "rows": [{"snapshot_at": "2026-08-09T17:16:02", "value": 7}],
    }

    assert compute_bundle_hash(first) == compute_bundle_hash(second)
    second["rows"][0]["value"] = 8
    assert compute_bundle_hash(first) != compute_bundle_hash(second)


def test_different_snapshot_records_different_snapshot(db_conn, config):
    snapshot1 = datetime(2026, 5, 24, 8, 0, tzinfo=KST)
    snapshot2 = datetime(2026, 5, 24, 9, 0, tzinfo=KST)
    bundle1 = build_brand_bundle("리바로", snapshot1, config, db_conn)
    bundle2 = build_brand_bundle("리바로", snapshot2, config, db_conn)
    assert bundle1["bundle_meta"]["snapshot_at"] != bundle2["bundle_meta"]["snapshot_at"]
    assert bundle1["bundle_meta"]["bundle_hash"] == bundle2["bundle_meta"]["bundle_hash"]


def test_config_version_changes_hash(db_conn, config):
    snapshot = datetime(2026, 5, 24, 8, 0, tzinfo=KST)
    config2 = replace(config, config_version="phase_zeta_v2_test")
    bundle1 = build_brand_bundle("리바로", snapshot, config, db_conn)
    bundle2 = build_brand_bundle("리바로", snapshot, config2, db_conn)
    assert bundle1["bundle_meta"]["bundle_hash"] != bundle2["bundle_meta"]["bundle_hash"]
