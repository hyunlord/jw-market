from __future__ import annotations

import json
import os

from pipeline.scripts.agent3.config import WORKFLOW_ID, resolve_workflow_rev
from pipeline.scripts.agent3.db import DbConfig, connect
from pipeline.scripts.agent3.market_loader import Agent3MarketLoader, make_market_record
from pipeline.scripts.agent3.market_processing import (
    build_native_market_position,
    build_strategic_inputs,
)
from pipeline.scripts.agent3.market_repository import MarketUnit, StrategicMarketRepository
from pipeline.scripts.agent3.run_full import _run_workflow_with_validation
from pipeline.scripts.agent3.run_source import _validate_execution_contract
from pipeline.scripts.agent3.workflow_client import Agent3WorkflowClient


EXPECTED_REV = 5692
TARGET = ("아보투윈", "ubist", "ml_009")


def snapshot(label: str) -> dict[str, object]:
    brand_key, source, market_id = TARGET
    queries = {
        "market": """
            SELECT COUNT(*) rows_n,
                   SUM(JSON_LENGTH(JSON_EXTRACT(strength_summary_json,'$.strength_items'))=0) profile_only,
                   SUM(generation_status='validation_isolated') isolated_n
            FROM agent3_brand_strength_market
        """,
        "non_target": """
            SELECT COUNT(*) rows_n,
                   COALESCE(SUM(CRC32(CONCAT_WS('|',brand_key,source,market_id,input_hash,
                                               generation_status,generated_at))),0) checksum_n
            FROM agent3_brand_strength_market
            WHERE NOT (brand_key=%s AND source=%s AND market_id=%s)
        """,
        "general": """
            SELECT COUNT(*) rows_n, COUNT(DISTINCT brand_key) brands_n,
                   SUM(JSON_LENGTH(JSON_EXTRACT(strength_summary_json,'$.strength_items'))=0) profile_only,
                   COALESCE(SUM(CRC32(CONCAT_WS('|',brand_key,source,input_hash,generated_at))),0) checksum_n
            FROM agent3_brand_strength_source
        """,
    }
    result: dict[str, object] = {}
    with connect(DbConfig.from_env()) as conn:
        with conn.cursor() as cursor:
            for name, sql in queries.items():
                cursor.execute(sql, TARGET if name == "non_target" else None)
                result[name] = cursor.fetchone()
                print(f"[{label}-{name}] {result[name]}", flush=True)
    return result


workflow_rev = resolve_workflow_rev(None)
_validate_execution_contract(
    workflow_rev=workflow_rev,
    expected_workflow_rev=EXPECTED_REV,
    cli_mode="full",
    environment_mode=os.environ.get("AGENT3_MODE"),
)
print(
    f"[target-preflight] workflow_rev={workflow_rev} expected={EXPECTED_REV} "
    "mode=full environment_mode=unset target=아보투윈/ubist/ml_009",
    flush=True,
)
before = snapshot("before")

unit = MarketUnit(
    view_kind="market_landscape",
    market_id="ml_009",
    brand_key="아보투윈",
    brand_name="아보투윈",
    source="ubist",
    mart_source="ubist",
)
repository = StrategicMarketRepository(DbConfig.from_env())
scope = repository.load_native_scope(unit)
profile, primary_candidates = build_strategic_inputs(unit, scope, top_n=5)
if not primary_candidates:
    raise RuntimeError("target unexpectedly has no candidates")

workflow_result = _run_workflow_with_validation(
    client=Agent3WorkflowClient(workflow_id=WORKFLOW_ID),
    profile=profile,
    candidates=primary_candidates,
    brand=unit.brand_name,
)
if workflow_result.workflow_calls > 2:
    raise RuntimeError(f"bounded retry exceeded: {workflow_result.workflow_calls}")

if workflow_result.status == "ready":
    stored_candidates = primary_candidates
    summary = {
        **workflow_result.summary,
        "source": unit.source,
        "view_kind": unit.view_kind,
        "market_id": unit.market_id,
    }
    status = "ready"
else:
    fallback = build_native_market_position(
        unit,
        scope,
        base_summary={
            "brand": unit.brand_name,
            "profile_display": profile,
            "limitations": [
                "wf316 validation failed after bounded retry; deterministic market_position fallback"
            ],
            "candidate_count": 1,
        },
    )
    stored_candidates = [fallback.candidate]
    summary = fallback.summary
    status = "complete_template_fallback"

record = make_market_record(
    brand_key=unit.brand_key,
    source=unit.source,
    market_id=unit.market_id,
    view_kind=unit.view_kind,
    brand_name=unit.brand_name,
    serving_brand_name=unit.brand_name,
    profile=profile,
    candidates=stored_candidates,
    hash_candidates=primary_candidates,
    summary=summary,
    workflow_id=WORKFLOW_ID,
    workflow_rev=workflow_rev,
    generation_status=status,
)
affected = Agent3MarketLoader(DbConfig.from_env()).upsert_many([record])
print(
    "[target-result] "
    + json.dumps(
        {
            "affected": affected,
            "generation_status": status,
            "strength_items": len(summary.get("strength_items") or []),
            "workflow_calls": workflow_result.workflow_calls,
            "validation_isolated": workflow_result.validation_isolated,
            "validation_log": workflow_result.isolation_log,
        },
        ensure_ascii=False,
        sort_keys=True,
    ),
    flush=True,
)
after = snapshot("after")
if before["non_target"] != after["non_target"]:
    raise RuntimeError("non-target rows changed")
if before["general"] != after["general"]:
    raise RuntimeError("general source table changed")
market = after["market"]
if int(market["rows_n"]) != 7706 or int(market["profile_only"] or 0) != 0 or int(market["isolated_n"] or 0) != 0:
    raise RuntimeError(f"final market gate failed: {market}")
