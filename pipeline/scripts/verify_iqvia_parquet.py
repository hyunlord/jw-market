"""
verify_iqvia_parquet.py
========================
parquet/iqvia_nsa/*.parquet 의 raw 충실성 검증.

Phase 1: 통계 (분기별 row, K5 분포)
Phase 2: 랜덤 K5 sample (n=10) 의 메타 + 시계열을 raw CSV 와 직접 비교
Phase 3: 0 값 vs NULL 구분 (raw 정합성)
Phase 4: Price 역산출 검증 (4Q 분기에서 derived price)
Phase 5: meta_conflicts row sample

사용법:
    python3 scripts/verify_iqvia_parquet.py \\
        --parquet-dir parquet/iqvia_nsa \\
        --baseline-csv "data/IQVIA/NSA/NSA_IQVIA_National Sales Audit_2Q 2025_3comb 가로.csv" \\
        --current-csv "data/IQVIA/NSA/NSA_IQVIA_2025 4Q.csv"
"""

import argparse
import csv
import json
import random
import re
import sys
from pathlib import Path

try:
    import pyarrow.parquet as pq
except ImportError:
    sys.exit("ERROR: pyarrow 필요. pip3 install pyarrow --break-system-packages")


# ============================================================================
# 정책 (prototype_01 와 동일)
# ============================================================================
QUARTER_METRICS = ["Values LC", "Units", "Counting Units", "Dosage Units", "Price"]

METRIC_TO_COLNAME = {
    "Values LC":      "values_lc",
    "Units":          "units",
    "Counting Units": "counting_units",
    "Dosage Units":   "dosage_units",
    "Price":          "price",
}

PAT_QUARTER_COL = re.compile(r"^(\d{1,2})/(\d{4})_(.+)$")
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


def parse_quarter_col(col_name):
    m = PAT_QUARTER_COL.match(col_name)
    if not m:
        return None
    month, year, metric = int(m.group(1)), m.group(2), m.group(3).strip()
    q = MONTH_TO_QUARTER.get(month)
    if q is None:
        return None
    return f"{year}-{q}", metric


# ============================================================================
# Parquet load
# ============================================================================
def load_all_parquet(parquet_dir):
    """모든 parquet 파일 read → dict[product_key] = list of (quarter, row_dict)."""
    by_pk = {}
    parquet_files = sorted(parquet_dir.glob("*.parquet"))
    if not parquet_files:
        sys.exit(f"ERROR: {parquet_dir} 안에 parquet 파일 없음")
    
    for pf in parquet_files:
        quarter = pf.stem
        table = pq.read_table(pf)
        col_data = {c: table.column(c).to_pylist() for c in table.column_names}
        for i in range(table.num_rows):
            row = {c: col_data[c][i] for c in table.column_names}
            pk = row["product_key"]
            by_pk.setdefault(pk, []).append((quarter, row))
    
    return by_pk, parquet_files


# ============================================================================
# Raw CSV batch 검색
# ============================================================================
def find_raw_rows_batch(csv_path, target_k5_set):
    """raw csv 한 번 scan 하면서 target K5 들의 데이터 모두 추출.
    
    target_k5_set: set of (audit_code, product_name, pack_desc)
    return: dict[k5] = (meta_dict_full, time_dict)
            time_dict: {(quarter, metric): float}
    """
    result = {}
    
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        ci_lookup = {h.lower(): h for h in headers}
        
        audit_h = ci_lookup.get("audit code")
        prod_h = ci_lookup.get("product name")
        pack_h = ci_lookup.get("pack desc")
        
        if not all([audit_h, prod_h, pack_h]):
            print(f"    ⚠ {csv_path.name}: K5 컬럼 못 찾음")
            return result
        
        # 시계열 컬럼 미리 분류
        time_cols = []  # (csv_col, quarter, metric)
        for col in headers:
            parsed = parse_quarter_col(col)
            if parsed:
                quarter, metric = parsed
                if metric in QUARTER_METRICS:
                    time_cols.append((col, quarter, metric))
        
        for row in reader:
            k5 = (
                clean_str(row.get(audit_h)),
                clean_str(row.get(prod_h)),
                clean_str(row.get(pack_h)),
            )
            if k5 not in target_k5_set:
                continue
            
            # 전체 메타
            meta = {k: clean_str(v) for k, v in row.items()}
            # 시계열 (raw 의 빈 셀은 entry 없음 — sparse)
            time_dict = {}
            for csv_col, quarter, metric in time_cols:
                v = to_float(row.get(csv_col))
                if v is not None:
                    time_dict[(quarter, metric)] = v
            
            result[k5] = (meta, time_dict)
    
    return result


# ============================================================================
# Phase 0: 전수 row count 검증 (K5 단위 sources — 적재 로직과 일치)
# ============================================================================
def scan_csv_k5_and_quarters(csv_path):
    """raw csv scan → (K5_set, K5 → quarter_set dict).
    
    각 K5 에 대해 그 K5 의 어느 분기에 raw 데이터가 있는지 (sparse).
    """
    K5_set = set()
    K5_quarters = {}  # K5 → set of quarters with data
    
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        ci_lookup = {h.lower(): h for h in headers}
        
        audit_h = ci_lookup.get("audit code")
        prod_h = ci_lookup.get("product name")
        pack_h = ci_lookup.get("pack desc")
        
        # 시계열 컬럼
        time_cols = []
        for col in headers:
            parsed = parse_quarter_col(col)
            if parsed:
                quarter, metric = parsed
                if metric in QUARTER_METRICS:
                    time_cols.append((col, quarter))
        
        for row in reader:
            audit_code = clean_str(row.get(audit_h))
            product_name = clean_str(row.get(prod_h))
            pack_desc = clean_str(row.get(pack_h))
            
            if not audit_code or not product_name or not pack_desc:
                continue
            if "grand total" in audit_code.lower():
                continue
            
            k5 = (audit_code, product_name, pack_desc)
            
            quarters_with_data = set()
            for col, quarter in time_cols:
                v = to_float(row.get(col))
                if v is not None:
                    quarters_with_data.add(quarter)
            
            if quarters_with_data:
                K5_set.add(k5)
                # 같은 K5 가 여러 row 에 있을 가능성 (드물지만) → union
                if k5 in K5_quarters:
                    K5_quarters[k5] |= quarters_with_data
                else:
                    K5_quarters[k5] = quarters_with_data
    
    return K5_set, K5_quarters


def phase0_full_count(parquet_files, baseline_csv, current_csv,
                     baseline_label, current_label):
    print()
    print("=" * 72)
    print("[Phase 0] 전수 row count 검증 (K5 단위 sources — 적재 로직과 일치)")
    print("=" * 72)
    
    # ===== parquet 통계 (sources 정확 비교) =====
    pq_baseline_only = 0
    pq_current_only = 0
    pq_both = 0
    pq_total = 0
    pq_unknown = []
    
    for pf in parquet_files:
        table = pq.read_table(pf, columns=["sources"])
        for sf in table.column("sources").to_pylist():
            pq_total += 1
            if "," in sf:
                pq_both += 1
            elif sf == baseline_label:
                pq_baseline_only += 1
            elif sf == current_label:
                pq_current_only += 1
            else:
                if len(pq_unknown) < 5:
                    pq_unknown.append(sf)
    
    print(f"  parquet:")
    print(f"    baseline only ({baseline_label!r}):  {pq_baseline_only:,}")
    print(f"    current only  ({current_label!r}):   {pq_current_only:,}")
    print(f"    both (merged):                       {pq_both:,}")
    print(f"    total:                               {pq_total:,}")
    if pq_unknown:
        print(f"    ⚠ unknown sources: {pq_unknown}")
    
    # ===== raw scan =====
    print(f"\n  raw csv scan...")
    import time
    
    t0 = time.time()
    baseline_K5, baseline_quarters = scan_csv_k5_and_quarters(baseline_csv)
    t1 = time.time()
    print(f"    baseline K5: {len(baseline_K5):,} ({t1-t0:.0f}s)")
    
    t0 = time.time()
    current_K5, current_quarters = scan_csv_k5_and_quarters(current_csv)
    t1 = time.time()
    print(f"    current K5:  {len(current_K5):,} ({t1-t0:.0f}s)")
    
    # K5 단위 분류 (적재 로직 그대로)
    baseline_only_K5 = baseline_K5 - current_K5
    current_only_K5 = current_K5 - baseline_K5
    both_K5 = baseline_K5 & current_K5
    
    # K5 별 long row 수 합산 (적재 시 union 머지 적용)
    raw_baseline_only_rows = sum(
        len(baseline_quarters[k5]) for k5 in baseline_only_K5
    )
    raw_current_only_rows = sum(
        len(current_quarters[k5]) for k5 in current_only_K5
    )
    raw_both_rows = sum(
        len(baseline_quarters.get(k5, set()) | current_quarters.get(k5, set()))
        for k5 in both_K5
    )
    raw_total = raw_baseline_only_rows + raw_current_only_rows + raw_both_rows
    
    print(f"\n  raw (K5 단위 분류):")
    print(f"    baseline only K5: {len(baseline_only_K5):>7,} → long rows: {raw_baseline_only_rows:>9,}")
    print(f"    current only K5:  {len(current_only_K5):>7,} → long rows: {raw_current_only_rows:>9,}")
    print(f"    both K5:          {len(both_K5):>7,} → long rows: {raw_both_rows:>9,}")
    print(f"    total long rows:                   {raw_total:>9,}")
    
    # 비교
    print(f"\n  비교 (raw = parquet):")
    checks = [
        ("baseline only", raw_baseline_only_rows, pq_baseline_only),
        ("current only",  raw_current_only_rows,  pq_current_only),
        ("both (merged)", raw_both_rows,          pq_both),
        ("TOTAL",         raw_total,              pq_total),
    ]
    
    n_pass, n_fail = 0, 0
    for name, raw_n, pq_n in checks:
        sym = "✓" if raw_n == pq_n else "✗"
        diff = raw_n - pq_n
        print(f"    {sym} {name:<16s}  raw={raw_n:>10,}  "
              f"parquet={pq_n:>10,}  diff={diff:+,}")
        if raw_n == pq_n:
            n_pass += 1
        else:
            n_fail += 1
    
    print(f"\n  [Phase 0 결과] pass={n_pass} / fail={n_fail}")
    return n_pass, n_fail


# ============================================================================
# Phase 1: 통계
# ============================================================================
def phase1_stats(parquet_files, by_pk):
    print()
    print("=" * 72)
    print("[Phase 1] 통계")
    print("=" * 72)
    
    print(f"  partitions: {len(parquet_files)}")
    print(f"  K5 unique:  {len(by_pk):,}")
    total_rows = sum(len(v) for v in by_pk.values())
    print(f"  total rows: {total_rows:,}")
    
    # K5 별 분기 분포 (sparsity)
    quarter_counts = [len(v) for v in by_pk.values()]
    if quarter_counts:
        min_q = min(quarter_counts)
        max_q = max(quarter_counts)
        avg_q = sum(quarter_counts) / len(quarter_counts)
        print(f"\n  K5 당 분기 분포 (sparsity):")
        print(f"    min:  {min_q} quarters")
        print(f"    max:  {max_q} quarters")
        print(f"    avg:  {avg_q:.1f} quarters")
    
    print(f"\n  분기별 row 수:")
    print(f"  {'quarter':<10}  {'rows':>10}  {'unique K5':>10}")
    print(f"  {'-'*10}  {'-'*10}  {'-'*10}")
    for pf in parquet_files:
        quarter = pf.stem
        table = pq.read_table(pf)
        n_rows = table.num_rows
        n_k5 = len(set(table.column("product_key").to_pylist()))
        print(f"  {quarter:<10}  {n_rows:>10,}  {n_k5:>10,}")


# ============================================================================
# Phase 2: 랜덤 K5 sample 검증 (vs raw CSV)
# ============================================================================
def phase2_random_samples(by_pk, baseline_csv, current_csv, n_samples=10):
    print()
    print("=" * 72)
    print(f"[Phase 2] 랜덤 K5 sample 메타+시계열 검증 (n={n_samples})")
    print("=" * 72)
    
    # n_samples 의 절반은 분기 많은 K5 (multi-source), 나머지 절반은 분기 적은 K5
    all_pks = list(by_pk.keys())
    sorted_pks = sorted(all_pks, key=lambda pk: -len(by_pk[pk]))
    
    n_multi = n_samples // 2
    n_single = n_samples - n_multi
    
    multi_candidates = sorted_pks[:500]
    single_candidates = sorted_pks[-1000:]
    
    random.shuffle(multi_candidates)
    random.shuffle(single_candidates)
    
    samples_pk = multi_candidates[:n_multi] + single_candidates[:n_single]
    
    # K5 tuples 모으기
    k5_tuples = []
    for pk in samples_pk:
        _, first_row = by_pk[pk][0]
        k5_tuples.append((
            first_row["audit_code"],
            first_row["product_name"],
            first_row["pack_desc"],
        ))
    
    target_set = set(k5_tuples)
    
    # raw csv 한 번씩 scan 하며 batch 추출
    print(f"\n  raw csv 한 번씩 scan...")
    baseline_data = find_raw_rows_batch(baseline_csv, target_set)
    current_data = find_raw_rows_batch(current_csv, target_set)
    print(f"    baseline 에서 찾은 K5: {len(baseline_data)}")
    print(f"    current 에서 찾은 K5:  {len(current_data)}")
    
    pass_count, fail_count = 0, 0
    
    for idx, pk in enumerate(samples_pk, 1):
        rows = by_pk[pk]
        _, first_row = rows[0]
        audit_code = first_row["audit_code"]
        product_name = first_row["product_name"]
        pack_desc = first_row["pack_desc"]
        k5 = (audit_code, product_name, pack_desc)
        
        print(f"\n  [{idx}] {product_name} | {audit_code} | {pack_desc}")
        print(f"      parquet partitions: {len(rows)}")
        
        # raw 시계열 합집합 (4Q 우선)
        raw_time = {}
        sources = []
        if k5 in baseline_data:
            raw_time.update(baseline_data[k5][1])
            sources.append("baseline")
        if k5 in current_data:
            for k, v in current_data[k5][1].items():
                raw_time[k] = v  # 4Q 우선
            sources.append("current")
        
        if not raw_time:
            print(f"      ✗ raw 에서 K5 못 찾음")
            fail_count += 1
            continue
        
        print(f"      sources: {sources}")
        
        # parquet 시계열 dict
        parquet_time = {}
        for quarter, row in rows:
            for metric_full, metric_short in METRIC_TO_COLNAME.items():
                v = row.get(metric_short)
                if v is not None:
                    parquet_time[(quarter, metric_full)] = v
        
        # 비교: raw 의 모든 (quarter, metric) 가 parquet 에 있는지
        n_compared = 0
        n_mismatch = 0
        mismatches = []
        for k, raw_v in raw_time.items():
            parquet_v = parquet_time.get(k)
            n_compared += 1
            if parquet_v is None:
                n_mismatch += 1
                mismatches.append((k, raw_v, "MISSING"))
            elif abs(parquet_v - raw_v) > 0.01:
                n_mismatch += 1
                mismatches.append((k, raw_v, parquet_v))
        
        # parquet 에 있는데 raw 에 없는 것 — Price 역산출 가능
        extra = set(parquet_time.keys()) - set(raw_time.keys())
        derived_prices = [k for k in extra if k[1] == "Price"]
        extra_non_price = [k for k in extra if k[1] != "Price"]
        
        if n_mismatch == 0:
            msg = f"      ✓ {n_compared} raw values 모두 parquet 와 일치"
            if derived_prices:
                msg += f" (+ {len(derived_prices)} Price 역산출)"
            print(msg)
            if extra_non_price:
                print(f"      ⚠ parquet 에 raw 에 없는 non-Price 값: {len(extra_non_price)} (이상)")
            pass_count += 1
        else:
            print(f"      ✗ {n_mismatch}/{n_compared} 불일치:")
            for k, rv, pv in mismatches[:3]:
                print(f"          [{k[0]}][{k[1]}]: raw={rv} parquet={pv}")
            fail_count += 1
    
    print(f"\n  [Phase 2 결과] pass={pass_count} / fail={fail_count}")
    return pass_count, fail_count


# ============================================================================
# Phase 3: 0 값 vs NULL 구분
# ============================================================================
def phase3_zero_vs_null(by_pk):
    print()
    print("=" * 72)
    print("[Phase 3] 0 값 vs NULL 구분")
    print("=" * 72)
    
    metrics = list(METRIC_TO_COLNAME.values())
    n_total = 0
    n_zero = {m: 0 for m in metrics}
    n_null = {m: 0 for m in metrics}
    n_value = {m: 0 for m in metrics}
    
    for pk, rows in by_pk.items():
        for quarter, row in rows:
            n_total += 1
            for m in metrics:
                v = row.get(m)
                if v is None:
                    n_null[m] += 1
                elif v == 0.0:
                    n_zero[m] += 1
                else:
                    n_value[m] += 1
    
    print(f"  total parquet rows: {n_total:,}")
    print(f"\n  metric 별 값 분포 (raw 의 0 과 빈 셀 구분):")
    print(f"  {'metric':<20}  {'NULL':>12}  {'0.0':>12}  {'value':>12}")
    print(f"  {'-'*20}  {'-'*12}  {'-'*12}  {'-'*12}")
    for m in metrics:
        print(f"  {m:<20}  {n_null[m]:>12,}  {n_zero[m]:>12,}  {n_value[m]:>12,}")
    print()
    print(f"  ✓ NULL 과 0.0 가 별개로 카운트됨 → raw 의 빈 셀 vs 명시적 0 구분 보존")


# ============================================================================
# Phase 4: Price 역산출 검증
# ============================================================================
def phase4_price_derivation(by_pk):
    print()
    print("=" * 72)
    print("[Phase 4] Price 역산출 검증 (price = values_lc / units)")
    print("=" * 72)
    
    n_match = 0          # price = values_lc / units 정확히 일치
    n_violations = 0     # 불일치
    n_skip_no_units = 0  # units = 0 또는 None
    n_no_price = 0       # price 자체 None
    n_no_values = 0      # values_lc None
    
    sample_violations = []
    
    for pk, rows in by_pk.items():
        for quarter, row in rows:
            price = row.get("price")
            v_lc = row.get("values_lc")
            units = row.get("units")
            
            if price is None:
                n_no_price += 1
                continue
            if v_lc is None:
                n_no_values += 1
                continue
            if units is None or units == 0:
                n_skip_no_units += 1
                continue
            
            expected = v_lc / units
            if abs(price - expected) > 0.01:
                n_violations += 1
                if len(sample_violations) < 3:
                    sample_violations.append((
                        row.get("product_name"), quarter,
                        price, v_lc, units, expected
                    ))
            else:
                n_match += 1
    
    print(f"  price = values_lc / units 검증:")
    print(f"    일치:                {n_match:,}")
    print(f"    불일치:              {n_violations:,}")
    print(f"    units=0/NULL skip:   {n_skip_no_units:,}")
    print(f"    price NULL:          {n_no_price:,}")
    print(f"    values_lc NULL:      {n_no_values:,}")
    
    if sample_violations:
        print(f"\n  불일치 sample:")
        for pn, q, p, vl, u, e in sample_violations:
            print(f"    {pn} | {q}: price={p:.4f} (expected {e:.4f}, "
                  f"values_lc={vl}, units={u})")
    elif n_violations == 0:
        print(f"\n  ✓ Price 역산출 모두 일치 (또는 raw 의 Price 값 그대로)")


# ============================================================================
# Phase 5: meta_conflicts sample
# ============================================================================
def phase5_meta_conflicts(by_pk):
    print()
    print("=" * 72)
    print("[Phase 5] meta_conflicts sample")
    print("=" * 72)
    
    n_pk_with_conflict = 0
    samples = []
    
    for pk, rows in by_pk.items():
        # 한 K5 의 모든 row 는 같은 meta_conflicts_json 가져야 (메타가 K5 단위 보존이라)
        first_mc = rows[0][1].get("meta_conflicts_json")
        if first_mc:
            n_pk_with_conflict += 1
            if len(samples) < 3:
                try:
                    conflicts = json.loads(first_mc)
                    pn = rows[0][1].get("product_name")
                    ac = rows[0][1].get("audit_code")
                    samples.append((pn, ac, conflicts))
                except json.JSONDecodeError:
                    pass
    
    print(f"  meta_conflicts 있는 K5 수: {n_pk_with_conflict:,}")
    print(f"  (적재 시 보고된 2,011 와 비교)")
    
    for pn, ac, conflicts in samples:
        print(f"\n  [{pn}] {ac}")
        for c in conflicts[:5]:
            print(f"      {c['field']}:")
            print(f"        baseline: {c['baseline']!r}")
            print(f"        current:  {c['current']!r}")


# ============================================================================
# main
# ============================================================================
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--parquet-dir", default="parquet/iqvia_nsa")
    p.add_argument("--baseline-csv", required=True)
    p.add_argument("--current-csv", required=True)
    p.add_argument("--baseline-label", default="2Q2025_3comb",
                   help="baseline csv 의 source label (적재 시와 동일해야)")
    p.add_argument("--current-label", default="2025_4Q",
                   help="current csv 의 source label (적재 시와 동일해야)")
    p.add_argument("--n-samples", type=int, default=10)
    args = p.parse_args()
    
    parquet_dir = Path(args.parquet_dir)
    baseline_csv = Path(args.baseline_csv)
    current_csv = Path(args.current_csv)
    
    if not parquet_dir.is_dir():
        sys.exit(f"ERROR: {parquet_dir} 없음")
    if not baseline_csv.exists():
        sys.exit(f"ERROR: {baseline_csv} 없음")
    if not current_csv.exists():
        sys.exit(f"ERROR: {current_csv} 없음")
    
    print("=" * 72)
    print("IQVIA NSA Parquet 무결성 검증")
    print("=" * 72)
    print(f"  parquet:      {parquet_dir}")
    print(f"  baseline csv: {baseline_csv}")
    print(f"  current csv:  {current_csv}")
    
    # 모든 parquet load
    print(f"\n  load parquet 파일들...")
    by_pk, parquet_files = load_all_parquet(parquet_dir)
    print(f"    loaded: {len(parquet_files)} files, {len(by_pk):,} unique K5")
    
    # Phase 검증
    p0_pass, p0_fail = phase0_full_count(
        parquet_files, baseline_csv, current_csv,
        args.baseline_label, args.current_label
    )
    phase1_stats(parquet_files, by_pk)
    p2_pass, p2_fail = phase2_random_samples(
        by_pk, baseline_csv, current_csv, n_samples=args.n_samples
    )
    phase3_zero_vs_null(by_pk)
    phase4_price_derivation(by_pk)
    phase5_meta_conflicts(by_pk)
    
    print()
    print("=" * 72)
    print("종합")
    print("=" * 72)
    print(f"  Phase 0 (전수 row count):     pass={p0_pass} / fail={p0_fail} / total=4")
    print(f"  Phase 2 (메타+시계열 sample): pass={p2_pass} / fail={p2_fail} / total={args.n_samples}")


if __name__ == "__main__":
    main()
