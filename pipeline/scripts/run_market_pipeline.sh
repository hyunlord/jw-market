#!/usr/bin/env bash
# JW Market ETL orchestrator.
#
# Layers:
#   Layer0: MI Master/catalog YAML/raw snapshots -> catalog parquet
#   Layer1: source files -> raw Layer1 stores
#   Layer2: raw/catalog inputs -> enriched parquet
#   Layer3: enriched/general inputs -> mart tables
#   Layer4: mart tables -> API cache tables

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ETL_DIR="$ROOT_DIR/pipeline/scripts/etl"
SCRIPT_DIR="$ROOT_DIR/pipeline/scripts"

usage() {
  cat <<'EOF'
Usage: pipeline/scripts/run_market_pipeline.sh <mode>

Modes:
  --all               Run Layer0(catalog), Layer1, Layer2, Layer3, Layer4
  --layer0            Run MI Master/catalog YAML -> catalog parquet only
  --layer0-catalog    Alias for --layer0
  --layer0-postfix    Run post-fix + Phase G molecule on existing Layer0 catalog (4495 -> 3874)
  --from-layer0       Run Layer0(catalog), Layer1, Layer2, Layer3, Layer4
  --from-layer2       Run Layer2, Layer3, Layer4
  --from-layer3       Run Layer3, Layer4
  --layer1            Run source loaders only
  --layer2            Run enriched parquet build only
  --layer3            Run general + strategic marts only
  --layer3-general    Run general marts only
  --layer3-strategic  Run strategic ML/CD marts only
  --layer4            Run API cache builders only
  --verify-only       Print current key row counts and Phase 15 IQVIA ratios
  --help              Show this help

Environment:
  PYTHON_BIN          Python executable (default: python3)

Notes:
  - --all/--from-layer0/--layer0 first verify that source files are placed under data/.
  - IQVIA Layer1 loading is resumable by source_file/sheet_name.
  - For exact same-file replacement, reset the affected raw table intentionally
    before rerunning Layer1; do not rely on --all to overwrite loaded files.
  - Phase 15 IQVIA de-duplication lives in layer3_compute_general_v3.py.
  - Layer4 runs build_cache_market_status.py, build_cache_cause.py, and
    build_cache_deep_analysis.py in that order.
EOF
}

run_verify_sources() {
  echo "=== Source files: verify original Excel/CSV placement ==="
  "$PYTHON_BIN" "$ETL_DIR/verify_source_files.py"
}

run_layer0_catalog() {
  echo "=== Layer0: MI Master + catalog YAML -> catalog parquet ==="
  run_verify_sources

  "$PYTHON_BIN" "$ETL_DIR/iqvia_loader.py" --source nsa --materialize-parquet --skip-db

  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_07_master_market_definition_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_08_master_qa_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_09_master_brand_consolidation_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_10_master_mapping_table_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_11_master_drug_to_parquet.py"

  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_12_dim_jw_products_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_13_brand_group_split_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_14_dim_market_landscape_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_15_dim_market_competitive_dynamics_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_16_dim_market_target_priority_to_parquet.py"

  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_17_ml_market_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_18_cd_filter_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_19_cd_market_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_20_strategic_brand_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_21_strategic_product_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_22_cd_brand_to_parquet.py"
  "$PYTHON_BIN" "$SCRIPT_DIR/prototype_23_cd_product_to_parquet.py"
  echo "=== Layer0 catalog build done ==="
}

run_layer0_postfix() {
  echo "=== Layer0 post-fix: canonical -> fix_ml_003 -> rebuild_sb -> oxgx -> rebuild_cd -> phase_g_molecule ==="
  "$PYTHON_BIN" "$ETL_DIR/build_strategic_brand_canonical.py"
  "$PYTHON_BIN" "$ETL_DIR/fix_ml_003_catalog_brands.py" --apply
  "$PYTHON_BIN" "$ETL_DIR/rebuild_strategic_brand_catalog.py" --apply
  "$PYTHON_BIN" "$ETL_DIR/apply_phase27_oxgx_catalog.py" --apply
  "$PYTHON_BIN" "$ETL_DIR/rebuild_cd_brand_catalog.py" --apply
  "$PYTHON_BIN" "$ETL_DIR/apply_molecule_worklist.py" --apply
  echo "=== Layer0 post-fix done (incl Phase G molecule) ==="
}

run_layer1() {
  echo "=== Layer1: source files -> raw stores ==="
  "$PYTHON_BIN" "$ETL_DIR/ubist_parquet_loader.py" --all --truncate
  "$PYTHON_BIN" "$ETL_DIR/iqvia_loader.py" --all
}

run_layer2() {
  echo "=== Layer2: raw/catalog inputs -> enriched parquet ==="
  "$PYTHON_BIN" "$ETL_DIR/layer2_enrich.py" --all --truncate
}

run_layer3_general() {
  echo "=== Layer3 general marts ==="
  "$PYTHON_BIN" "$ETL_DIR/layer3_compute_general_v3.py" --all --insert
}

run_layer3_strategic() {
  echo "=== Layer3 strategic marts ==="
  "$PYTHON_BIN" "$ETL_DIR/layer3_compute_strategic_ml_v3.py" --insert
  "$PYTHON_BIN" "$ETL_DIR/layer3_compute_strategic_cd_v3.py" --insert
}

run_layer3() {
  run_layer3_general
  run_layer3_strategic
}

run_layer4() {
  echo "=== Layer4 API caches ==="
  "$PYTHON_BIN" "$ETL_DIR/build_cache_market_status.py" --output-db jw_mart
  "$PYTHON_BIN" "$ETL_DIR/build_cache_cause.py" --output-db jw_mart
  "$PYTHON_BIN" "$ETL_DIR/build_cache_deep_analysis.py" --output-db jw_mart
}

run_verify_only() {
  echo "=== Verify current mart/cache state ==="
  "$PYTHON_BIN" - <<'PY'
import json
import urllib.parse
import urllib.request

import pymysql

conn = pymysql.connect(host="localhost", port=3308, user="root", password="<LOCAL_ROOT_PW>", database="jw_mart")
cur = conn.cursor(pymysql.cursors.DictCursor)
for table in [
    "mart_general_brand_metric",
    "mart_general_market_metric",
    "mart_strategic_ml_brand_metric",
    "mart_strategic_ml_market_metric",
    "mart_strategic_cd_brand_metric",
    "mart_strategic_cd_market_metric",
    "cache_market_status",
    "cache_cause",
    "cache_deep_analysis",
]:
    cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
    print(f"{table}: {cur.fetchone()['n']}")
conn.close()

for brand_name in ["제이클", "뉴트로진", "가드메트", "페린젝트"]:
    brand = urllib.parse.quote(brand_name)
    with urllib.request.urlopen(
        f"http://127.0.0.1:8013/api/cause/{brand}?view=competitive_dynamics&source=IQVIA&measure=sales",
        timeout=30,
    ) as response:
        api = json.load(response)
    series = {point["period"]: point for point in api["data"]["sources_data"]["market_size_series"]}
    q1 = series["2025-Q1"]["value"]
    q3 = series["2025-Q3"]["value"]
    print(f"{brand_name}: 2025-Q3/Q1={q3 / q1 * 100:.1f}%")
PY
}

mode="${1:---help}"
case "$mode" in
  --all)
    run_layer0_catalog
    run_layer0_postfix
    run_layer1
    run_layer2
    run_layer3
    run_layer4
    ;;
  --from-layer0)
    run_layer0_catalog
    run_layer0_postfix
    run_layer1
    run_layer2
    run_layer3
    run_layer4
    ;;
  --from-layer2)
    run_layer2
    run_layer3
    run_layer4
    ;;
  --from-layer3)
    run_layer3
    run_layer4
    ;;
  --layer0|--layer0-catalog) run_layer0_catalog ;;
  --layer0-postfix) run_layer0_postfix ;;
  --layer1) run_layer1 ;;
  --layer2) run_layer2 ;;
  --layer3) run_layer3 ;;
  --layer3-general) run_layer3_general ;;
  --layer3-strategic) run_layer3_strategic ;;
  --layer4) run_layer4 ;;
  --verify-only) run_verify_only ;;
  --help|-h|help) usage ;;
  *)
    echo "Unknown mode: $mode" >&2
    usage >&2
    exit 2
    ;;
esac
