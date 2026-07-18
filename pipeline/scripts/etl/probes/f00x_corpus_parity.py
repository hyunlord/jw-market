from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from typing import Any

from pipeline.scripts.api import db
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.strategic_runtime import build_strategic_payload
from pipeline.scripts.api.models.dynamic_market import DynamicMarketAnalysisLevelFilters, DynamicMarketRequest
from pipeline.scripts.api.routes.dynamic_market import _build_general_dynamic_response
from pipeline.scripts.etl.build_analysis_level_blocks import BUILD_VERSION, discover_general_profiles, enumerate_base_keys



def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def build(case: dict[str, Any]) -> dict[str, Any]:
    if case["view"] == "general":
        request = DynamicMarketRequest.model_validate(
            {
                "view": "general",
                "source": case["source"],
                "measure": case["measure"],
                "filters": {"atc4": [case["market_id"]], "focus_brand_key": case.get("focus")},
            }
        )
        return _build_general_dynamic_response(request)
    return build_strategic_payload(
        mart_db=config.db_name,
        ml_id=case["market_id"] if case["view"] == "strategic_ml" else None,
        cd_market_id=case["market_id"] if case["view"] == "strategic_cd" else None,
        focus_brand_key=case.get("focus"),
        source=case["source"],
        measure=case["measure"],
        analysis_level=DynamicMarketAnalysisLevelFilters(),
    )


def general_cases() -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        SELECT market_id, source, measure, profile_sig
        FROM mart_analysis_level_block
        WHERE view='general' AND build_version=%s
        ORDER BY market_id, source, measure, profile_sig
        """,
        (BUILD_VERSION,),
    )
    by_key: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        by_key[(str(row["market_id"]), str(row["source"]), str(row["measure"]))].add(str(row.get("profile_sig") or ""))
    discovered = discover_general_profiles(enumerate_base_keys())

    cases: list[dict[str, Any]] = []
    one_ubist = 0
    iqvia = 0
    no_focus = 0
    for (market_id, source, measure), signatures in sorted(by_key.items()):
        if source == "UBIST" and len(signatures) > 1:
            representatives = dict(discovered[(market_id, measure)])
            missing = signatures - representatives.keys()
            if missing:
                raise RuntimeError(f"missing focus representatives key={(market_id, source, measure)} signatures={sorted(missing)}")
            for signature, focus in sorted(representatives.items()):
                cases.append({"group": "multi_signature", "expected_profile_sig": signature, "view": "general", "market_id": market_id, "source": "ubist", "measure": measure, "focus": focus})
        elif source == "UBIST" and one_ubist < 10:
            cases.append({"group": "general_ubist", "view": "general", "market_id": market_id, "source": "ubist", "measure": measure, "focus": None})
            one_ubist += 1
        elif source == "IQVIA" and iqvia < 10:
            cases.append({"group": "general_iqvia", "view": "general", "market_id": market_id, "source": "iqvia", "measure": measure, "focus": None})
            iqvia += 1
        if source == "UBIST" and no_focus < 5:
            cases.append({"group": "general_no_focus", "view": "general", "market_id": market_id, "source": "ubist", "measure": measure, "focus": None})
            no_focus += 1

    for measure in ("sales", "volume"):
        cases.append({"group": "required_c10c_livalofeno", "view": "general", "market_id": "C10C", "source": "ubist", "measure": measure, "focus": "리바로페노"})
    cases.extend(
        [
            {"group": "prior_failure", "view": "general", "market_id": "C10A1", "source": "ubist", "measure": "sales", "focus": "리바로"},
            {"group": "prior_failure", "view": "general", "market_id": "C10A1", "source": "iqvia", "measure": "sales", "focus": "리바로"},
        ]
    )
    return cases


def strategic_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for view, table, id_column in (
        ("strategic_ml", "mart_strategic_ml_brand_metric", "ml_id"),
        ("strategic_cd", "mart_strategic_cd_brand_metric", "cd_market_id"),
    ):
        markets = db.fetch_all(
            f"SELECT DISTINCT {id_column} AS market_id FROM {table} WHERE source='ubist' AND measure='sales' ORDER BY {id_column}"
        )
        for index, market in enumerate(markets):
            market_id = str(market["market_id"])
            brands = db.fetch_all(
                f"SELECT brand_key, is_jw FROM {table} WHERE {id_column}=%s AND source='ubist' AND measure='sales' ORDER BY is_jw DESC, brand_key",
                (market_id,),
            )
            full = next((str(row["brand_key"]) for row in brands if row.get("is_jw")), None)
            trim = next((str(row["brand_key"]) for row in brands if not row.get("is_jw")), None)
            if full:
                cases.append({"group": "strategic_full", "view": view, "market_id": market_id, "source": "ubist", "measure": "sales", "focus": full})
            if trim and (view == "strategic_ml" or index < 5):
                cases.append({"group": "strategic_trim", "view": view, "market_id": market_id, "source": "ubist", "measure": "sales", "focus": trim})
    # Explicitly retain the previously failing strategic focus cases.
    cases.extend(
        [
            {"group": "prior_failure", "view": "strategic_ml", "market_id": "ml_003", "source": "ubist", "measure": "sales", "focus": "가드렛"},
            {"group": "prior_failure", "view": "strategic_ml", "market_id": "ml_003", "source": "ubist", "measure": "sales", "focus": "가브스"},
            {"group": "f001_census", "view": "strategic_ml", "market_id": "ml_001", "source": "ubist", "measure": "sales", "focus": None},
            {"group": "f003_census", "view": "strategic_ml", "market_id": "ml_015", "source": "iqvia", "measure": "sales", "focus": None},
            {"group": "f003_census", "view": "strategic_cd", "market_id": "cd_014", "source": "iqvia", "measure": "sales", "focus": None},
        ]
    )
    return cases


def main() -> None:
    deduped: dict[str, dict[str, Any]] = {}
    for case in general_cases() + strategic_cases():
        key = json.dumps({key: case.get(key) for key in ("view", "market_id", "source", "measure", "focus")}, ensure_ascii=False, sort_keys=True)
        deduped.setdefault(key, case)
    cases = list(deduped.values())
    print(json.dumps({"event": "corpus", "count": len(cases), "groups": {group: sum(case["group"] == group for case in cases) for group in sorted({case["group"] for case in cases})}}, ensure_ascii=False), flush=True)

    mismatches = []
    started = time.monotonic()
    for index, case in enumerate(cases, 1):
        os.environ["ANALYSIS_LEVEL_BLOCK_REPLAY_DISABLED"] = "1"
        fallback = canonical(build(case))
        os.environ.pop("ANALYSIS_LEVEL_BLOCK_REPLAY_DISABLED", None)
        replay = canonical(build(case))
        same = replay == fallback
        row = {"index": index, **case, "fallback_bytes": len(fallback), "replay_bytes": len(replay), "equal": same}
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if not same:
            mismatches.append(row)
    summary = {"event": "complete", "checked": len(cases), "mismatches": len(mismatches), "seconds": time.monotonic() - started}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
