"""
prototype_01_iqvia_to_parquet.py
==================================
IQVIA NSA CSV (2Q baseline + 4Q current) → Parquet 분기별 partition

검증된 정책 (prototype_sqlite_v1 의 결과):
- K5 = AUDIT CODE + PRODUCT NAME + PACK DESC
- 메타: 양쪽 CSV 의 union (case insensitive lookup)
- 분기 데이터만 (yearly, MAT 제외)
- 충돌 시 4Q 우선 + meta_conflicts 기록
- Grand Total row 제외
- Price 역산출: 4Q raw 에 Price 없으면 Values LC / Units

출력:
- parquet/iqvia_nsa/{YYYY-Q?}.parquet (분기별, 22 partition 예상)
- 각 row = 한 K5 + 그 분기의 metric 5개 + 메타 + source_files

사용법:
    python3 scripts/prototype_01_iqvia_to_parquet.py \\
        --baseline-csv "data/NSA_2Q2025_..._cols1_re_rows1.csv" \\
        --current-csv "data/NSA_2025_4Q_cols1_rows1.csv" \\
        --output-dir parquet/iqvia_nsa
"""

import argparse
import csv
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    sys.exit(
        "ERROR: pyarrow 필요.\n"
        "  pip3 install pyarrow --break-system-packages"
    )


# ============================================================================
# 정책 상수
# ============================================================================

# K5 분리축 (case insensitive lookup)
K5_FIELDS = ["AUDIT CODE", "PRODUCT NAME", "PACK DESC"]

# 메타 컬럼들 (2Q + 4Q union, case insensitive 매핑)
# (CSV 헤더 후보들, staging 컬럼명)
META_DEFS = [
    # K5
    ("AUDIT CODE",          "audit_code"),
    ("PRODUCT NAME",        "product_name"),
    ("PACK DESC",           "pack_desc"),
    # 양쪽 CSV 공통
    ("OTC/ETHICAL",         "otc_ethical"),
    ("MFR CODE",            "mfr_code"),
    ("MFR NAME",            "mfr_name"),
    ("MFR NAME KOR",        "mfr_name_kor"),
    ("MFT TYPE",            "mft_type"),
    ("MFR TYPE GROUP",      "mfr_type_group"),
    ("ATC 1 CODE",          "atc1_code"),
    ("ATC 1 DESC",          "atc1_desc"),
    ("ATC 2 CODE",          "atc2_code"),
    ("ATC 2 DESC",          "atc2_desc"),
    ("ATC 3 CODE",          "atc3_code"),
    ("ATC 3 DESC",          "atc3_desc"),
    ("ATC 4 CODE",          "atc4_code"),
    ("ATC 4 DESC",          "atc4_desc"),
    ("NFC 1 CODE",          "nfc1_code"),
    ("NFC 1 DESC",          "nfc1_desc"),
    ("NFC 2 CODE",          "nfc2_code"),
    ("NFC 2 DESC",          "nfc2_desc"),
    ("NFC 3 CODE",          "nfc3_code"),
    ("NFC 3 DESC",          "nfc3_desc"),
    ("STRENGTH",            "strength"),
    ("MOLECULE DESC",       "molecule_desc"),
    ("MOLECULE TYPE",       "molecule_type"),
    ("NHI TYPE",            "nhi_type"),
    ("PACK LAUNCHDATE",     "pack_launchdate"),
    # 2Q 에만 (case mixed)
    ("Product Launch Date", "product_launch_date"),
    ("AUDIT DESC",          "audit_desc"),
    ("HERBAL",              "herbal"),
    # 4Q 에만 (uppercase)
    ("PRODUCT LAUNCH DATE", "product_launch_date"),
    ("PRODUCT AGE",         "product_age"),
    ("PACK SIZE",           "pack_size"),
    ("PACK AGE",            "pack_age"),
]

# 분기 metric (5개)
QUARTER_METRICS = ["Values LC", "Units", "Counting Units", "Dosage Units", "Price"]

# CSV header → staging 컬럼명 변환
METRIC_TO_COLNAME = {
    "Values LC":      "values_lc",
    "Units":          "units",
    "Counting Units": "counting_units",
    "Dosage Units":   "dosage_units",
    "Price":          "price",
}

# 시계열 컬럼 패턴: "M/YYYY_metric" (예: "3/2022_Values LC")
PAT_QUARTER_COL = re.compile(r"^(\d{1,2})/(\d{4})_(.+)$")

# 분기 month → quarter 매핑
MONTH_TO_QUARTER = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}


# ============================================================================
# 헬퍼
# ============================================================================
def clean_str(v):
    if v is None:
        return None
    v = str(v).strip()
    return v if v else None


def to_float(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.replace(",", "").strip()
        if not v or v.lower() == "nan":
            return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute_product_key(audit_code, product_name, pack_desc):
    """K5 → SHA256 hash 32 char."""
    s = f"{audit_code or ''}|{product_name or ''}|{pack_desc or ''}"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def parse_quarter_col(col_name):
    """
    '3/2022_Values LC' → ('2022-Q1', 'Values LC')
    '2023_Values LC' → None (yearly)
    'MAT Dec 2024_Values LC' → None
    """
    m = PAT_QUARTER_COL.match(col_name)
    if not m:
        return None
    month, year, metric = int(m.group(1)), m.group(2), m.group(3).strip()
    q = MONTH_TO_QUARTER.get(month)
    if q is None:
        return None
    return f"{year}-{q}", metric


def get_row_value(row, header_candidates):
    """row 에서 case insensitive lookup. header_candidates 중 첫 매칭."""
    for h in header_candidates:
        if h in row:
            return row[h]
        # case insensitive 매칭
        for k in row.keys():
            if k.lower() == h.lower():
                return row[k]
    return None


def build_case_insensitive_lookup(headers):
    """csv headers 로 lower → original 매핑."""
    return {h.lower(): h for h in headers}


# ============================================================================
# CSV 파싱
# ============================================================================
def parse_csv(csv_path, source_label):
    """
    return:
      result: {pk: {meta: {...}, observations: {quarter: {metric: float}}, sources: [label]}}
      meta_columns_present: set of staging 메타 컬럼명 (실제 사용된 것)
    """
    print(f"\n  parsing: {csv_path.name}  [{source_label}]")
    
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        
        # case insensitive lookup table
        ci_lookup = build_case_insensitive_lookup(headers)
        
        # 시계열 컬럼 분류
        time_cols = []  # list of (csv_col, quarter, metric)
        for col in headers:
            parsed = parse_quarter_col(col)
            if parsed:
                quarter, metric = parsed
                if metric in QUARTER_METRICS:
                    time_cols.append((col, quarter, metric))
        
        print(f"    headers: {len(headers)}")
        print(f"    quarter time cols: {len(time_cols)}")
        quarters = sorted(set(q for _, q, _ in time_cols))
        if quarters:
            print(f"    quarters: {quarters[0]} ~ {quarters[-1]} ({len(quarters)} unique)")
        metrics = sorted(set(m for _, _, m in time_cols))
        print(f"    metrics:  {metrics}")
        
        # 메타 컬럼 매핑 (CSV header → staging 컬럼)
        meta_col_map = {}  # staging_col → csv_col (case insensitive resolved)
        for csv_col_candidate, staging_col in META_DEFS:
            actual = ci_lookup.get(csv_col_candidate.lower())
            if actual is not None and staging_col not in meta_col_map:
                meta_col_map[staging_col] = actual
        
        result = {}
        n_total = 0
        n_grand_total_skipped = 0
        n_missing_k5 = 0
        
        for row in reader:
            n_total += 1
            
            # K5 추출 (case insensitive)
            audit_code = clean_str(row.get(ci_lookup.get("audit code", "AUDIT CODE")))
            product_name = clean_str(row.get(ci_lookup.get("product name", "PRODUCT NAME")))
            pack_desc = clean_str(row.get(ci_lookup.get("pack desc", "PACK DESC")))
            
            # Grand Total row skip
            if audit_code and "grand total" in audit_code.lower():
                n_grand_total_skipped += 1
                continue
            
            # K5 결손 row
            if not audit_code or not product_name or not pack_desc:
                n_missing_k5 += 1
                continue
            
            pk = compute_product_key(audit_code, product_name, pack_desc)
            
            # 메타 수집
            meta = {}
            for staging_col, csv_col in meta_col_map.items():
                v = clean_str(row.get(csv_col))
                if v is not None:
                    meta[staging_col] = v
            # K5 메타도 explicit
            meta["audit_code"] = audit_code
            meta["product_name"] = product_name
            meta["pack_desc"] = pack_desc
            
            # 시계열 수집 (sparse: raw 의 빈 셀은 obs 에 entry 없음)
            obs = {}
            for csv_col, quarter, metric in time_cols:
                v = to_float(row.get(csv_col))
                if v is not None:  # raw 빈 셀 ≠ raw 의 명시적 0
                    obs.setdefault(quarter, {})[metric] = v
            
            # Price 역산출 (raw 에 Price 없거나 None 인 분기)
            for q, ms in obs.items():
                if ms.get("Price") is None:
                    v_lc = ms.get("Values LC")
                    units = ms.get("Units")
                    if v_lc is not None and units is not None and units != 0:
                        ms["Price"] = v_lc / units
            
            if pk not in result:
                result[pk] = {
                    "meta": meta,
                    "observations": obs,
                    "sources": [source_label],
                }
            else:
                # 같은 K5 가 같은 CSV 안에 또 있는 경우 — observations 머지
                for q, ms in obs.items():
                    result[pk]["observations"].setdefault(q, {}).update(ms)
        
        print(f"    rows: {n_total:,} | "
              f"grand_total: {n_grand_total_skipped} | "
              f"missing K5: {n_missing_k5} | "
              f"unique K5: {len(result):,}")
        return result, set(meta_col_map.keys())


# ============================================================================
# 두 source 머지 (4Q 우선)
# ============================================================================
def merge_sources(baseline, current, baseline_label, current_label):
    """
    충돌 시 current (4Q) 우선. baseline (2Q) 의 다른 값은 meta_conflicts 기록.
    """
    print(f"\n  merging: {baseline_label} ← {current_label} (current 우선)")
    
    merged = {}
    meta_conflicts = {}
    
    all_pks = set(baseline.keys()) | set(current.keys())
    n_baseline_only = 0
    n_current_only = 0
    n_both = 0
    
    for pk in all_pks:
        b = baseline.get(pk)
        c = current.get(pk)
        
        if b and not c:
            merged[pk] = {
                "meta": dict(b["meta"]),
                "observations": dict(b["observations"]),
                "sources": list(b["sources"]),
            }
            n_baseline_only += 1
        elif c and not b:
            merged[pk] = {
                "meta": dict(c["meta"]),
                "observations": dict(c["observations"]),
                "sources": list(c["sources"]),
            }
            n_current_only += 1
        else:
            n_both += 1
            # current 메타 우선
            merged_meta = dict(c["meta"])
            row_conflicts = []
            for k, b_val in b["meta"].items():
                c_val = c["meta"].get(k)
                if b_val is not None and c_val is not None and b_val != c_val:
                    row_conflicts.append({
                        "field": k,
                        "baseline": b_val,
                        "current": c_val,
                    })
                elif b_val is not None and c_val is None:
                    # baseline 만 있는 메타는 보존
                    merged_meta[k] = b_val
            if row_conflicts:
                meta_conflicts[pk] = row_conflicts
            
            # observations 머지: 같은 분기는 current 우선
            merged_obs = dict(b["observations"])
            for q, ms in c["observations"].items():
                if q in merged_obs:
                    # 분기 충돌: metric 단위로 current 우선
                    for metric, v in ms.items():
                        if v is not None:
                            merged_obs[q][metric] = v
                else:
                    merged_obs[q] = dict(ms)
            
            merged[pk] = {
                "meta": merged_meta,
                "observations": merged_obs,
                "sources": list(set(b["sources"] + c["sources"])),
            }
    
    print(f"    baseline only:  {n_baseline_only:,}")
    print(f"    current only:   {n_current_only:,}")
    print(f"    both (merged):  {n_both:,}")
    print(f"    meta_conflicts: {len(meta_conflicts):,}")
    print(f"    total merged:   {len(merged):,}")
    
    return merged, meta_conflicts


# ============================================================================
# 분기별 Parquet write
# ============================================================================
def write_parquet_partitions(merged, meta_conflicts, all_meta_cols, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 모든 분기 수집
    all_quarters = set()
    for entry in merged.values():
        all_quarters.update(entry["observations"].keys())
    all_quarters = sorted(all_quarters)
    
    print(f"\n  quarters: {len(all_quarters)} ({all_quarters[0]} ~ {all_quarters[-1]})")
    
    ingested_at = datetime.now(timezone.utc).isoformat()
    
    # 메타 컬럼 순서 (모든 source 의 union)
    meta_col_order = []
    for _, staging_col in META_DEFS:
        if staging_col in all_meta_cols and staging_col not in meta_col_order:
            meta_col_order.append(staging_col)
    
    print(f"  meta columns: {len(meta_col_order)}")
    print(f"  metric columns: {len(QUARTER_METRICS)}")
    
    written = []
    
    for quarter in all_quarters:
        rows = []
        for pk, entry in merged.items():
            qobs = entry["observations"].get(quarter)
            if not qobs:  # None 또는 empty dict → 이 K5 는 이 분기에 raw 데이터 없음
                continue
            
            row = {"product_key": pk}
            # 메타 컬럼
            for col in meta_col_order:
                row[col] = entry["meta"].get(col)
            # metric 컬럼
            for metric in QUARTER_METRICS:
                row[METRIC_TO_COLNAME[metric]] = qobs.get(metric)
            # source 정보
            row["sources"] = ",".join(sorted(set(entry["sources"])))
            # meta_conflicts (있는 경우만)
            mc = meta_conflicts.get(pk)
            row["meta_conflicts_json"] = json.dumps(mc, ensure_ascii=False) if mc else None
            # ingest timestamp
            row["ingested_at"] = ingested_at
            
            rows.append(row)
        
        if not rows:
            continue
        
        table = pa.Table.from_pylist(rows)
        out_path = output_dir / f"{quarter}.parquet"
        pq.write_table(
            table, out_path,
            compression="zstd",
            compression_level=3,
        )
        size_mb = out_path.stat().st_size / 1024 / 1024
        written.append((quarter, len(rows), size_mb))
    
    return written


# ============================================================================
# main
# ============================================================================
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--baseline-csv", required=True,
                   help="2Q baseline CSV (예: NSA_2Q2025..._cols1_re_rows1.csv)")
    p.add_argument("--baseline-label", default="2Q2025_3comb",
                   help="2Q csv source 라벨 (기본: 2Q2025_3comb)")
    p.add_argument("--current-csv", required=True,
                   help="4Q current CSV (예: NSA_2025_4Q_cols1_rows1.csv)")
    p.add_argument("--current-label", default="2025_4Q",
                   help="4Q csv source 라벨 (기본: 2025_4Q)")
    p.add_argument("--output-dir", default="parquet/iqvia_nsa",
                   help="Parquet 출력 디렉토리 (기본: parquet/iqvia_nsa)")
    args = p.parse_args()
    
    baseline_csv = Path(args.baseline_csv)
    current_csv = Path(args.current_csv)
    output_dir = Path(args.output_dir)
    
    if not baseline_csv.exists():
        sys.exit(f"ERROR: {baseline_csv} 없음")
    if not current_csv.exists():
        sys.exit(f"ERROR: {current_csv} 없음")
    
    print("=" * 72)
    print("IQVIA NSA → Parquet (분기별 partition)")
    print("=" * 72)
    print(f"  baseline: {baseline_csv}")
    print(f"            [{args.baseline_label}]")
    print(f"  current:  {current_csv}")
    print(f"            [{args.current_label}]")
    print(f"  output:   {output_dir}/")
    
    # Step 1: CSV 파싱
    print("\n[Step 1] CSV 파싱")
    baseline, meta_cols_b = parse_csv(baseline_csv, args.baseline_label)
    current, meta_cols_c = parse_csv(current_csv, args.current_label)
    all_meta_cols = meta_cols_b | meta_cols_c
    print(f"\n  union meta columns: {len(all_meta_cols)}")
    
    # Step 2: 머지
    print("\n[Step 2] 머지 (4Q 우선)")
    merged, meta_conflicts = merge_sources(
        baseline, current, args.baseline_label, args.current_label
    )
    
    # Step 3: 분기별 Parquet write
    print("\n[Step 3] 분기별 Parquet write")
    written = write_parquet_partitions(merged, meta_conflicts, all_meta_cols, output_dir)
    
    # 출력 요약
    print()
    print("=" * 72)
    print("결과")
    print("=" * 72)
    print(f"  partitions: {len(written)}")
    print(f"  K5 unique:  {len(merged):,}")
    print()
    print(f"  {'quarter':<10} {'rows':>10}  {'size (MB)':>10}")
    print(f"  {'-'*10} {'-'*10}  {'-'*10}")
    total_rows = 0
    total_size = 0.0
    for q, n, s in written:
        print(f"  {q:<10} {n:>10,}  {s:>10.2f}")
        total_rows += n
        total_size += s
    print(f"  {'-'*10} {'-'*10}  {'-'*10}")
    print(f"  {'TOTAL':<10} {total_rows:>10,}  {total_size:>10.2f}")
    
    # 검증용 hint
    print()
    print("=" * 72)
    print("검증 (DuckDB)")
    print("=" * 72)
    print("  pip3 install duckdb --break-system-packages")
    print("  python3")
    print("    >>> import duckdb")
    print(f"    >>> duckdb.sql(\"SELECT COUNT(*) FROM '{output_dir}/*.parquet'\").show()")
    print(f"    >>> duckdb.sql(\"SELECT * FROM '{output_dir}/2025-Q4.parquet' LIMIT 5\").show()")


if __name__ == "__main__":
    main()
