"""cache_cause row identity: reader/producer agreement and collision fail-close."""

from __future__ import annotations

import pytest

from pipeline.etl.io.cache.cause_key import (
    CACHE_CAUSE_KEY_COLUMNS,
    CACHE_CAUSE_TARGET_KEY_COLUMNS,
    CacheCauseKeyCollision,
    assert_no_key_collisions,
    cache_cause_identity,
    cache_market_id,
    cache_view_source_id,
    find_key_collisions,
    strategy_id_for,
    usable_optional_columns,
)

# Sibling CD markets under one parent ML, from the catalog registry.
SIBLING_SPLITS = {"ml_008": ("cd_008", "cd_009"), "ml_009": ("cd_010", "cd_011"), "ml_010": ("cd_012", "cd_013")}


def _identity(brand, view, ml_id, cd_id=None, source="UBIST", measure="sales"):
    return cache_cause_identity(
        brand=brand, view_type=view, source=source, measure=measure, ml_id=ml_id, cd_id=cd_id
    )


def test_strategy_mapping_matches_the_producer_helper():
    assert strategy_id_for("ml_7") == "strategy_007"
    assert strategy_id_for("ml_007") == "strategy_007"
    assert strategy_id_for(None) is None


def test_current_key_is_five_columns_and_target_adds_view_source_id():
    assert CACHE_CAUSE_KEY_COLUMNS == ("brand", "view_type", "source", "measure", "market_id")
    assert CACHE_CAUSE_TARGET_KEY_COLUMNS[-1] == "view_source_id"


# ------------------------------------------------- fault injection (4) -------


@pytest.mark.parametrize("parent, children", sorted(SIBLING_SPLITS.items()))
def test_sibling_cd_markets_collide_on_the_current_primary_key(parent, children):
    """(4) Two sibling CD rows on one PK must FAIL, not silently REPLACE."""

    first, second = children
    identities = [
        _identity("경쟁브랜드", "competitive_dynamics", parent, first),
        _identity("경쟁브랜드", "competitive_dynamics", parent, second),
    ]

    collisions = find_key_collisions(identities)

    assert len(collisions) == 1
    key, sources = next(iter(collisions.items()))
    assert key[4] == cache_market_id("competitive_dynamics", parent)
    assert sources == sorted(children)

    with pytest.raises(CacheCauseKeyCollision) as error:
        assert_no_key_collisions(identities)
    assert "would drop rows" in str(error.value)


def test_distinct_brands_under_one_parent_do_not_collide():
    identities = [
        _identity("리바로하이", "competitive_dynamics", "ml_008", "cd_008"),
        _identity("리바로브이", "competitive_dynamics", "ml_008", "cd_009"),
    ]

    assert find_key_collisions(identities) == {}
    assert_no_key_collisions(identities)


def test_dual_ml_membership_does_not_collide():
    """The 264 dual brands land on different strategy ids, so ML is already safe."""

    identities = [
        _identity("건카베딜", "market_landscape", "ml_005"),
        _identity("건카베딜", "market_landscape", "ml_008"),
    ]

    assert find_key_collisions(identities) == {}


def test_same_row_repeated_is_not_a_collision():
    identity = _identity("리바로페노", "market_landscape", "ml_007")

    assert find_key_collisions([identity, identity]) == {}


def test_ml_and_cd_of_one_brand_do_not_collide():
    identities = [
        _identity("리바로페노", "market_landscape", "ml_007"),
        _identity("리바로페노", "competitive_dynamics", "ml_007", "cd_007"),
    ]

    assert find_key_collisions(identities) == {}


# -------------------------------------------------------------- provenance ---


def test_optional_columns_are_skipped_on_an_unmigrated_table():
    assert usable_optional_columns(["brand", "view_type", "market_id"]) == ()


def test_optional_columns_are_used_once_the_migration_lands():
    migrated = [
        "brand",
        "view_type",
        "source",
        "measure",
        "market_id",
        "response_json",
        "payload_size",
        "view_source_id",
        "run_id",
        "build_sha",
        "input_manifest_json",
    ]

    assert usable_optional_columns(migrated) == (
        "view_source_id",
        "run_id",
        "build_sha",
        "input_manifest_json",
    )


def test_view_source_id_is_the_market_the_row_is_about():
    assert cache_view_source_id("market_landscape", "ml_007", "cd_007") == "ml_007"
    assert cache_view_source_id("competitive_dynamics", "ml_008", "cd_009") == "cd_009"
