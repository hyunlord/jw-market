from __future__ import annotations

import hashlib
import json

from pipeline.scripts.deploy import analysis_cache_blue_green_validation as validation


class RecordingCursor:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.statements)


class RowCursor(RecordingCursor):
    def __init__(self, statements: list[str], row: dict[str, object]) -> None:
        super().__init__(statements)
        self.row = row

    def fetchone(self) -> dict[str, object]:
        return self.row


class RowConnection(RecordingConnection):
    def __init__(self, row: dict[str, object]) -> None:
        super().__init__()
        self.row = row

    def cursor(self) -> RowCursor:
        return RowCursor(self.statements, self.row)


def _cache_payload() -> list[dict[str, object]]:
    return [
        {
            "brand": f"brand-{index}",
            "general_sources": ["UBIST"],
            "strategic_sources": ["IQVIA"],
        }
        for index in range(25)
    ]


def _cache_sha(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_cache_validation_requires_expected_hash_count_and_source_keys() -> None:
    payload = _cache_payload()
    digest = _cache_sha(payload)

    assert validation.validate_cache_brands_payload(
        payload,
        expected_sha256=digest,
        expected_brand_count=25,
    ) == digest

    payload[0].pop("general_sources")
    try:
        validation.validate_cache_brands_payload(
            payload,
            expected_sha256=digest,
            expected_brand_count=25,
        )
    except RuntimeError as exc:
        assert "general_sources" in str(exc)
    else:
        raise AssertionError("missing source metadata must fail validation")


def test_cache_validation_rejects_self_consistent_but_unapproved_payload() -> None:
    payload = _cache_payload()

    try:
        validation.validate_cache_brands_payload(
            payload,
            expected_sha256="0" * 64,
            expected_brand_count=25,
        )
    except RuntimeError as exc:
        assert "canonical sha mismatch" in str(exc)
    else:
        raise AssertionError("unapproved cache payload must fail validation")


def test_staging_validation_rejects_empty_expected_source_epoch() -> None:
    conn = RecordingConnection()

    try:
        validation.validate_staging_tables(
            conn,
            target_db="jw_mart_stage",
            expected_brands_sha256="a" * 64,
            expected_source_epoch="",
        )
    except ValueError as exc:
        assert "expected_source_epoch" in str(exc)
    else:
        raise AssertionError("empty approved source epoch must fail validation")

    assert conn.statements == []


def test_staging_validation_rejects_incomplete_malb_before_cache_read(monkeypatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(validation, "table_exists", lambda _conn, _db, _table: True)
    monkeypatch.setattr(validation, "_table_row_count", lambda _conn, _db, _table: 3137)

    try:
        validation.validate_staging_tables(
            conn,
            target_db="jw_mart_stage",
            expected_brands_sha256="a" * 64,
            expected_source_epoch="epoch",
        )
    except RuntimeError as exc:
        assert "MALB staging row count mismatch" in str(exc)
    else:
        raise AssertionError("incomplete MALB staging must fail validation")

    assert conn.statements == []


def test_staging_validation_rejects_wrong_malb_build_version(monkeypatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(validation, "table_exists", lambda _conn, _db, _table: True)
    monkeypatch.setattr(
        validation,
        "_table_row_count",
        lambda _conn, _db, table: 3138 if "analysis_level" in table else 1,
    )
    monkeypatch.setattr(
        validation,
        "_malb_identity",
        lambda _conn, _db, _table: ("epoch", "analysis-level-block-v4"),
    )

    try:
        validation.validate_staging_tables(
            conn,
            target_db="jw_mart_stage",
            expected_brands_sha256="a" * 64,
            expected_source_epoch="epoch",
        )
    except RuntimeError as exc:
        assert "build version mismatch" in str(exc)
    else:
        raise AssertionError("wrong MALB build version must fail validation")

    assert conn.statements == []


def test_malb_identity_rejects_multiple_source_epochs() -> None:
    conn = RowConnection(
        {
            "epoch_count": 2,
            "source_epoch": "epoch-a",
            "build_version_count": 1,
            "build_version": validation.ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
        }
    )

    try:
        validation._malb_identity(
            conn,
            "jw_mart_stage",
            "mart_analysis_level_block_staging",
        )
    except RuntimeError as exc:
        assert "exactly one source epoch" in str(exc)
    else:
        raise AssertionError("multiple MALB source epochs must fail validation")


def test_staging_validation_rejects_expected_source_epoch_mismatch(monkeypatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(validation, "table_exists", lambda _conn, _db, _table: True)
    monkeypatch.setattr(validation, "_table_row_count", lambda _conn, _db, _table: 3138)
    monkeypatch.setattr(
        validation,
        "_malb_identity",
        lambda _conn, _db, _table: (
            "actual-epoch",
            validation.ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
        ),
    )

    try:
        validation.validate_staging_tables(
            conn,
            target_db="jw_mart_stage",
            expected_brands_sha256="a" * 64,
            expected_source_epoch="approved-epoch",
        )
    except RuntimeError as exc:
        assert "source epoch mismatch" in str(exc)
    else:
        raise AssertionError("unapproved MALB source epoch must fail validation")

    assert conn.statements == []


def test_staging_validation_rejects_extra_cache_rows(monkeypatch) -> None:
    conn = RecordingConnection()
    monkeypatch.setattr(validation, "table_exists", lambda _conn, _db, _table: True)
    monkeypatch.setattr(
        validation,
        "_table_row_count",
        lambda _conn, _db, table: 3138 if "analysis_level" in table else 2,
    )
    monkeypatch.setattr(
        validation,
        "_malb_identity",
        lambda _conn, _db, _table: (
            "epoch",
            validation.ANALYSIS_LEVEL_BLOCK_SCHEMA_VERSION,
        ),
    )

    try:
        validation.validate_staging_tables(
            conn,
            target_db="jw_mart_stage",
            expected_brands_sha256="a" * 64,
            expected_source_epoch="epoch",
        )
    except RuntimeError as exc:
        assert "cache_brands staging row count mismatch" in str(exc)
    else:
        raise AssertionError("extra cache_brands rows must fail validation")

    assert conn.statements == []
