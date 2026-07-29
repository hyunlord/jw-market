from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from pipeline.etl.io.iqvia_loader import iter_nsa_xlsx
from pipeline.etl.io.iqvia_parquet_cache import (
    CacheIntegrityError,
    CacheUnavailableError,
    LocalCacheStorage,
    build_iqvia_parquet_cache,
    cache_identity_for_source,
    iter_iqvia_parquet_cache,
)


HEADERS = [
    "DATA PERIOD",
    "AUDIT CODE",
    "AUDIT DESC",
    "MFR CODE",
    "MFR NAME",
    "PRODUCT NAME",
    "PACK DESC",
    "ATC 4 CODE",
    "Values LC",
    "Units",
    "Counting Units",
    "Dosage Units",
    "Price",
]


def _row(period: str, atc4: str, product: str, value: int) -> list[object]:
    return [
        period,
        "KCPA",
        "Korea Direct Clinic Pharmaceutical Audit",
        "MFR",
        "MAKER",
        product,
        "10MG",
        atc4,
        value,
        value // 2,
        value // 5,
        value // 4,
        1,
    ]


def _write_workbook(path: Path) -> Path:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "NSA"
    sheet.append(HEADERS)
    sheet.append(_row("2026-03-01", "A01A", "ALPHA", 100))
    sheet.append(_row("2026-06-01", "C10A", "BETA", 200))
    sheet.append(_row("2026-06-01", "A01A", "GAMMA", 300))
    workbook.save(path)
    return path


def test_cache_build_and_scoped_read_matches_direct_reader(tmp_path: Path) -> None:
    source = _write_workbook(tmp_path / "nsa.xlsx")
    storage = LocalCacheStorage(tmp_path / "cache")

    manifest = build_iqvia_parquet_cache(source, storage, batch_size=2)
    cached = list(
        iter_iqvia_parquet_cache(
            source,
            storage,
            quarters=("2026-Q2",),
            atc4_codes=("C10A",),
        )
    )
    direct = list(
        iter_nsa_xlsx(
            source,
            quarters=("2026-Q2",),
            atc4_codes=("C10A",),
        )
    )

    assert cached == direct
    assert manifest.total_rows == 3
    assert {
        (partition.quarter, partition.atc4_code, partition.row_count)
        for partition in manifest.partitions
    } == {
        ("2026-Q1", "A01A", 1),
        ("2026-Q2", "A01A", 1),
        ("2026-Q2", "C10A", 1),
    }
    assert storage.exists(f"{manifest.cache_prefix}/manifest.json")
    assert storage.exists(f"{manifest.cache_prefix}/_SUCCESS")


def test_scope_free_cache_read_matches_direct_reader_values(tmp_path: Path) -> None:
    source = _write_workbook(tmp_path / "nsa.xlsx")
    storage = LocalCacheStorage(tmp_path / "cache")
    build_iqvia_parquet_cache(source, storage)

    cached = list(iter_iqvia_parquet_cache(source, storage))
    direct = list(iter_nsa_xlsx(source))

    assert cached == direct


def test_cache_consumer_fails_closed_when_cache_is_missing(tmp_path: Path) -> None:
    source = _write_workbook(tmp_path / "nsa.xlsx")
    storage = LocalCacheStorage(tmp_path / "cache")

    with pytest.raises(CacheUnavailableError, match="_SUCCESS"):
        list(iter_iqvia_parquet_cache(source, storage))


def test_cache_consumer_fails_closed_when_partition_is_corrupt(tmp_path: Path) -> None:
    source = _write_workbook(tmp_path / "nsa.xlsx")
    storage = LocalCacheStorage(tmp_path / "cache")
    manifest = build_iqvia_parquet_cache(source, storage)
    partition_file = manifest.partitions[0].files[0]
    storage.put_bytes(partition_file.key, b"corrupt")

    with pytest.raises(CacheIntegrityError, match="checksum"):
        list(iter_iqvia_parquet_cache(source, storage))


def test_source_change_selects_new_cache_key_and_ignores_old_cache(tmp_path: Path) -> None:
    source = _write_workbook(tmp_path / "nsa.xlsx")
    storage = LocalCacheStorage(tmp_path / "cache")
    original = build_iqvia_parquet_cache(source, storage)

    workbook = openpyxl.load_workbook(source)
    workbook["NSA"].append(_row("2026-06-01", "C10A", "DELTA", 400))
    workbook.save(source)
    changed = cache_identity_for_source(source)

    assert changed.cache_key != original.cache_key
    with pytest.raises(CacheUnavailableError, match="_SUCCESS"):
        list(iter_iqvia_parquet_cache(source, storage))


def test_repeated_build_of_same_source_is_a_noop(tmp_path: Path) -> None:
    source = _write_workbook(tmp_path / "nsa.xlsx")
    storage = LocalCacheStorage(tmp_path / "cache")

    first = build_iqvia_parquet_cache(source, storage)
    first_keys = storage.list_keys(first.cache_prefix)
    second = build_iqvia_parquet_cache(source, storage)

    assert second == first
    assert storage.list_keys(first.cache_prefix) == first_keys


class _ManifestFailingStorage(LocalCacheStorage):
    def put_bytes(self, key: str, content: bytes) -> None:
        if key.endswith("/manifest.json"):
            raise OSError("injected manifest write failure")
        super().put_bytes(key, content)


def test_failed_build_never_publishes_success_marker(tmp_path: Path) -> None:
    source = _write_workbook(tmp_path / "nsa.xlsx")
    storage = _ManifestFailingStorage(tmp_path / "cache")
    identity = cache_identity_for_source(source)

    with pytest.raises(OSError, match="injected"):
        build_iqvia_parquet_cache(source, storage)

    assert not storage.exists(f"{identity.cache_prefix}/_SUCCESS")
