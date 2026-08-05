from __future__ import annotations

from jw_chat_agent_poc.tools.general_view_membership import (
    GeneralBrandMembership,
    StaticGeneralMembershipReader,
    TtlGeneralMembershipCache,
    shared_general_membership_cache,
)
from scripts.measure_v3_latency_shadow import DbWriteGuard, QUESTIONS


def _membership() -> GeneralBrandMembership:
    return GeneralBrandMembership(
        brand_key="eylea",
        brand_name="아일리아",
        atc4_code="S01P",
        atc4_description="Ophthalmologicals",
        source="IQVIA",
    )


def test_general_membership_observability_distinguishes_miss_and_hit() -> None:
    cache = TtlGeneralMembershipCache(
        StaticGeneralMembershipReader((_membership(),)),
        ttl_seconds=300,
    )

    assert cache.resolve("아일리아", "IQVIA") is not None
    assert cache.resolve("아일리아", "IQVIA") is not None

    observed = cache.observability()
    assert observed["cache_hits"] == 1
    assert observed["cache_misses"] == 1
    assert observed["refresh_successes"] == 1


def test_shadow_executors_reuse_the_existing_general_membership_cache(
    monkeypatch,
) -> None:
    import jw_chat_agent_poc.tools.general_view_membership as membership_module

    reader = StaticGeneralMembershipReader((_membership(),))
    monkeypatch.setattr(membership_module, "_SHARED_GENERAL_MEMBERSHIP_CACHES", {})
    monkeypatch.setattr(membership_module, "MariaDbGeneralMembershipReader", lambda: reader)

    first = shared_general_membership_cache(ttl_seconds=300)
    second = shared_general_membership_cache(ttl_seconds=300)

    assert first is second
    assert first.resolve("아일리아", "IQVIA") is not None
    assert second.resolve("아일리아", "IQVIA") is not None
    assert first.observability()["refresh_successes"] == 1


def test_latency_harness_preserves_prior_interleaved_question_order() -> None:
    assert tuple(QUESTIONS) == (78, 223, 170, 64, 40, 114, 77, 51, 95, 86)


def test_db_write_guard_allows_only_read_only_set_forms() -> None:
    assert DbWriteGuard._read_only_statement("SET NAMES utf8mb4")
    assert DbWriteGuard._read_only_statement("SET CHARACTER SET utf8mb4")
    assert DbWriteGuard._read_only_statement("SET TRANSACTION READ ONLY")
    assert DbWriteGuard._read_only_statement("SET SESSION TRANSACTION READ ONLY")

    assert not DbWriteGuard._read_only_statement("SET GLOBAL max_connections=100")
    assert not DbWriteGuard._read_only_statement("SET PERSIST max_connections=100")
    assert not DbWriteGuard._read_only_statement("SET @@global.max_connections=100")
    assert not DbWriteGuard._read_only_statement("SET @user_variable=1")
