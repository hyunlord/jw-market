"""Verify source-file placement before unattended ETL reproduction."""

from __future__ import annotations

import sys
from pathlib import Path

from storage import get_data_path, get_mi_master_path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _count_source_files(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    return sum(
        1
        for file in path.rglob("*")
        if file.is_file() and file.suffix.lower() in {".xlsx", ".csv"}
    )


def main() -> int:
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
    }

    missing: list[str] = []
    print("=== 원본 엑셀 배치 검증 ===")
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


if __name__ == "__main__":
    sys.exit(main())
