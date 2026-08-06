"""Affected-scope planning and strategic refresh validation."""

from __future__ import annotations

from typing import Sequence

from pipeline.etl.mi_master_refresh.contracts import (
    SUPPORTED_REFRESH_CACHE_TABLES,
    AffectedDefinition,
    AffectedScopePlan,
    StrategicMarketValidationInput,
)


def plan_affected_scope(
    *,
    affected_definitions: Sequence[AffectedDefinition],
    existing_general_atc4: Sequence[str],
    all_ml_ids: Sequence[str] = (),
    all_cd_ids: Sequence[str] = (),
) -> AffectedScopePlan:
    existing = set(existing_general_atc4)
    affected_ml_ids = tuple(sorted({item.market_id for item in affected_definitions}))
    affected_cd_ids = tuple(
        sorted({cd_id for item in affected_definitions for cd_id in item.cd_ids})
    )
    cache_tables = tuple(
        table
        for table in SUPPORTED_REFRESH_CACHE_TABLES
        if any(table in item.cache_tables for item in affected_definitions)
    )
    general_rebuild = tuple(
        sorted(
            {
                code
                for item in affected_definitions
                for code in item.atc4_codes
                if code not in existing
            }
        )
    )
    return AffectedScopePlan(
        affected_ml_ids,
        cache_tables,
        general_rebuild,
        affected_ml_ids=affected_ml_ids,
        affected_cd_ids=affected_cd_ids,
        unchanged_ml_ids=tuple(sorted(set(all_ml_ids) - set(affected_ml_ids))),
        unchanged_cd_ids=tuple(sorted(set(all_cd_ids) - set(affected_cd_ids))),
    )


def validate_strategic_market_refresh(
    payload: StrategicMarketValidationInput,
) -> None:
    changed_unchanged = [
        market_id
        for market_id, before_hash in payload.unchanged_market_hash_before.items()
        if payload.unchanged_market_hash_after.get(market_id) != before_hash
    ]
    if changed_unchanged:
        raise ValueError(
            "unchanged market hash changed: " + ", ".join(sorted(changed_unchanged))
        )
    for cd_id, members in payload.cd_members.items():
        parent = payload.cd_parent_ml.get(cd_id)
        if parent is None:
            raise ValueError(f"CD membership is not a subset of parent ML: {cd_id}")
        if not set(members) <= set(payload.ml_members.get(parent, ())):
            raise ValueError(f"CD membership is not a subset of parent ML: {cd_id}")
    sigma_mismatches = [
        market_id
        for market_id, before_value in payload.sigma_before.items()
        if payload.sigma_after.get(market_id) != before_value
    ]
    if sigma_mismatches:
        raise ValueError("sigma mismatch: " + ", ".join(sorted(sigma_mismatches)))
