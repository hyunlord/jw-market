"""STAGE 0 dry-run driver: read-only payload introspection for cache_brand_elements.

Verifies (1) alias-duplicate behavior ('리바로 브이' vs '리바로브이'), (2) factors/strength
fill vs empty/not_generated, (3) source brand universe count. No writes, no LLM.
"""
import json
import sys

sys.path.insert(0, ".")

from pipeline.scripts.etl.cache_brand_elements import (  # noqa: E402
    build_brand_element_payloads,
    connect_db,
    source_brands,
)

AGENT3_SCHEMA = "jw_mart_d2_stage_20260630_r2"
ALIAS_CASES = ["리바로 브이", "리바로브이", "마운자로", "로수젯", "크레스토"]


def factors_filled(factors):
    if not factors:
        return False
    if factors.get("atc"):
        return True
    for group in ("ubist", "iqvia"):
        for values in (factors.get(group) or {}).values():
            if values:
                return True
    return False


def main():
    conn = connect_db()
    try:
        brands = source_brands(conn)
        print("STAGE0_TOTAL_SOURCE_BRANDS=%d" % len(brands))
        print("STAGE0_ALIAS_IN_SOURCE=" + json.dumps([b for b in ALIAS_CASES if b in brands], ensure_ascii=False))
        targets = list(dict.fromkeys(brands[:195] + ALIAS_CASES))
        payloads = build_brand_element_payloads(conn, targets, agent3_schema=AGENT3_SCHEMA)
        keys = [p.brand_key for p in payloads]
        if len(keys) != len(set(keys)):
            print("STAGE0_FAIL=duplicate brand_key in payloads")
            raise SystemExit(3)
        n_factors = sum(1 for p in payloads if factors_filled(p.factors))
        n_strength = sum(1 for p in payloads if p.strength.get("available"))
        print("STAGE0_SAMPLE_N=%d STAGE0_FACTORS_FILLED=%d STAGE0_STRENGTH_AVAILABLE=%d"
              % (len(payloads), n_factors, n_strength))
        for p in payloads:
            if p.brand_key in ALIAS_CASES:
                print("STAGE0_ALIAS_DETAIL=" + json.dumps({
                    "brand_key": p.brand_key,
                    "factors_filled": factors_filled(p.factors),
                    "atc": p.factors.get("atc"),
                    "strength_available": p.strength.get("available"),
                    "strength_reason": p.strength.get("reason"),
                    "strength_workflow_rev": p.strength_workflow_rev,
                }, ensure_ascii=False, default=str))
        print("STAGE0_CHECK_OK")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
