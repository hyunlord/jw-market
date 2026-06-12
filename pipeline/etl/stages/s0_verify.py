"""Stage s0 verify - 원본 배치 검증 + 파일 매니페스트 비교.

s0 remains a file-level gate. It can say which source files exist and whether
their byte fingerprints match the last recorded baseline, but it does not infer
periods, five-year coverage, or row counts. Some source file names do not carry
period metadata, so content-level validation is intentionally delegated to s1
load. The rejected alternative was letting verify decide skip/incremental work
directly; that would make s0 own load orchestration, so run.py will own that
decision when later stages become real.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.etl.io.db import connect_local, ensure_manifest_table, latest_manifest, record_manifest
from pipeline.etl.io.manifest import compare, scan_source_files
from pipeline.etl.lib.storage import get_data_path, get_mi_master_path

STAGE = "s0 verify"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
TARGET_PRIORITY_SKELETON = (
    PROJECT_ROOT / "data" / "cache" / "prototype_11_step_c4_target_priority_precompute_sample.csv"
)


def _count_source_files(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    return sum(
        1
        for file in path.rglob("*")
        if file.is_file() and file.suffix.lower() in {".xlsx", ".csv"}
    )


def run(params: dict[str, Any]) -> int:
    required = {
        "MI Master": get_mi_master_path(),
        "UBIST dir": get_data_path(
            bucket_env="MINIO_BUCKET_RAW_UBIST",
            bucket_default="jw-market-raw-ubist",
            local_default=PROJECT_ROOT / "data" / "UBIST",
        ),
        "IQVIA dir": get_data_path(
            bucket_env="MINIO_BUCKET_RAW_IQVIA",
            bucket_default="jw-market-raw-iqvia",
            local_default=PROJECT_ROOT / "data" / "IQVIA",
        ),
        "Target priority skeleton": TARGET_PRIORITY_SKELETON,
    }

    missing: list[str] = []
    print(f"[{STAGE}] === 원본 배치 검증 ===")
    for name, path in required.items():
        if not path.exists():
            print(f"  ✗ {name}: {path} (없음)")
            missing.append(name)
            continue
        if path.is_dir():
            source_count = _count_source_files(path)
            print(f"  ✓ {name}: {path} ({source_count} files)")
            if source_count == 0:
                missing.append(f"{name} (디렉토리 비어있음)")
        else:
            print(f"  ✓ {name}: {path}")

    if missing:
        print(f"\n누락: {missing}")
        print("data/에 원본 엑셀/CSV를 배치한 후 재실행하세요.")
        return 1

    print("\n✓ 모든 원본 파일 배치 확인 - 재현 가능")
    rows = scan_source_files(required)
    with connect_local() as conn:
        ensure_manifest_table(conn)
        previous = latest_manifest(conn)
        result = compare(previous, rows)

        print(f"\n[{STAGE}] 매니페스트 비교")
        if previous:
            previous_run_id = next(iter(previous.values()))["run_id"]
            print(f"  - 이전 적재: 있음(run_id={previous_run_id}, 파일 {len(previous)})")
        else:
            print("  - 이전 적재: 없음")
        print(f"  - 동일: {'yes' if result['identical'] else 'no'}")
        print(f"  - 새 파일: {_format_files(result['new_files'])}")
        print(f"  - 변경 파일: {_format_files(result['changed_files'])}")
        print(f"  - 누락 파일: {_format_files(result['missing_files'])}")
        print("  - 판단: verify는 비교 결과만 출력합니다. skip/증분 결정은 run.py 영역입니다.")

        if params.get("record_baseline"):
            run_id = f"s0_{datetime.now():%Y%m%d_%H%M%S}"
            record_manifest(conn, rows, run_id)
            print(f"  - 베이스라인 기록 완료(run_id={run_id}, 파일 {len(rows)})")

    # Manifest differences are information, not a gate. Existence failures keep
    # the legacy rc=1 behavior; existence pass returns rc=0.
    return 0


def _format_files(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "[]"
    return "[" + ", ".join(f"{row['source']}:{row['file_name']}" for row in rows) + "]"
