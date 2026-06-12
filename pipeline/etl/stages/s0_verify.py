"""Stage s0 verify - 원본 배치 검증."""
from __future__ import annotations

from pathlib import Path
from typing import Any

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
    _ = params
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
    return 0
