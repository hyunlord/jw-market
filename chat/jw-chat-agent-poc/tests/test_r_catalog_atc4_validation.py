from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Protocol

import pytest

from jw_chat_agent_poc.resolver.catalog_membership import MariaDbCatalogMembershipReader
from jw_chat_agent_poc.service.general_view_routing import (
    CatalogDefinitionLoadError,
    GeneralViewService,
    MariaDbStrategicMarketDefinitionReader,
    StrategicMarketDefinition,
)


class _FakeCursor:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[str, ...]) -> None:
        assert "WHERE ml_id = %s" in query
        assert params == ("ml_006",)

    def fetchall(self) -> tuple[dict[str, object], ...]:
        return self._rows


class _FakeConnection:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)


def _reader(
    monkeypatch: pytest.MonkeyPatch,
    atc_codes_json: object,
    *,
    data_source: str = "ubist",
    row_present: bool = True,
    connect_error: BaseException | None = None,
) -> MariaDbStrategicMarketDefinitionReader:
    rows = (
        (
            {
                "ml_id": "ml_006",
                "data_source": data_source,
                "atc_codes_json": atc_codes_json,
            },
        )
        if row_present
        else ()
    )
    class FakeMySqlError(Exception):
        pass

    class FakeOperationalError(FakeMySqlError):
        pass

    def connect(**kwargs: object) -> _FakeConnection:
        if connect_error is not None:
            raise FakeOperationalError(*connect_error.args)
        return _FakeConnection(rows)

    fake_pymysql = SimpleNamespace(
        connect=connect,
        cursors=SimpleNamespace(DictCursor=object),
        MySQLError=FakeMySqlError,
        err=SimpleNamespace(OperationalError=FakeOperationalError),
    )
    monkeypatch.setitem(sys.modules, "pymysql", fake_pymysql)
    return MariaDbStrategicMarketDefinitionReader(
        MariaDbCatalogMembershipReader(
            host="db.example",
            database="catalog",
            user="reader",
            password="not-a-real-secret",
        )
    )


def test_reader_preserves_observed_ubist_codes_without_all_or_nothing_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _reader(monkeypatch, '["C10A1", "C10C"]')

    definition = reader.resolve("ml_006")

    assert definition is not None
    assert definition.atc4_codes == ("C10A1", "C10C")
    assert definition.excluded_atc4_count == 0


@pytest.mark.parametrize(
    ("data_source", "codes"),
    (
        ("ubist", '["C1D", "C10C", "C10A1"]'),
        ("iqvia", '["A06B1", "C10C0"]'),
        ("both", '["A10C1", "V06D0"]'),
    ),
)
def test_reader_accepts_only_observed_source_shapes(
    monkeypatch: pytest.MonkeyPatch,
    data_source: str,
    codes: str,
) -> None:
    definition = _reader(monkeypatch, codes, data_source=data_source).resolve("ml_006")

    assert definition is not None
    assert definition.atc4_codes == tuple(json.loads(codes))
    assert definition.excluded_atc4_count == 0


def test_reader_keeps_valid_code_and_counts_invalid_code(monkeypatch: pytest.MonkeyPatch) -> None:
    definition = _reader(monkeypatch, '["C10A1", "ZZZZZZ"]').resolve("ml_006")

    assert definition is not None
    assert definition.atc4_codes == ("C10A1",)
    assert definition.excluded_atc4_count == 1


def test_reader_fails_when_every_code_is_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _reader(monkeypatch, '["ZZZZZZ", "YYYYYY"]')

    with pytest.raises(CatalogDefinitionLoadError) as captured:
        reader.resolve("ml_006")

    assert captured.value.reason_code == "catalog_all_codes_invalid"


def test_reader_preserves_empty_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    definition = _reader(monkeypatch, "[]").resolve("ml_006")

    assert definition is not None
    assert definition.atc4_codes == ()
    assert definition.excluded_atc4_count == 0


def test_reader_returns_none_for_absent_primary_key(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _reader(monkeypatch, "[]", row_present=False).resolve("ml_006") is None


def test_reader_types_invalid_json_as_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _reader(monkeypatch, "{not-json")

    with pytest.raises(CatalogDefinitionLoadError) as captured:
        reader.resolve("ml_006")

    assert captured.value.reason_code == "catalog_parse_error"


def test_reader_types_database_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _reader(
        monkeypatch,
        "[]",
        connect_error=RuntimeError(2003, "cannot connect password=do-not-expose"),
    )

    with pytest.raises(CatalogDefinitionLoadError) as captured:
        reader.resolve("ml_006")

    assert captured.value.reason_code == "catalog_db_unreachable"


class _DefinitionReader(Protocol):
    def resolve(self, market_id: str) -> StrategicMarketDefinition | None: ...


class _StaticReader:
    def __init__(
        self,
        value: StrategicMarketDefinition | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.value = value
        self.error = error

    def resolve(self, market_id: str) -> StrategicMarketDefinition | None:
        assert market_id == "ml_006"
        if self.error is not None:
            raise self.error
        return self.value


def _candidate_result(
    reader: _DefinitionReader,
) -> tuple[tuple[str, ...], str, str | None, int]:
    service = GeneralViewService(
        object(),
        object(),
        enabled=True,
        market_definition_reader=reader,
    )
    candidates, source, reason, excluded_count = service._catalog_market_candidates(
        "ml_006",
        requested_source=None,
    )
    return tuple(candidate.code for candidate in candidates), source, reason, excluded_count


@pytest.mark.parametrize(
    ("reader", "expected"),
    (
        (
            _StaticReader(StrategicMarketDefinition("ml_006", "ubist", ("C10A1", "C10C"))),
            (("C10A1", "C10C"), "ubist", None, 0),
        ),
        (
            _StaticReader(StrategicMarketDefinition("ml_006", "ubist", ("C10A1",), 1)),
            (("C10A1",), "ubist", "catalog_code_invalid", 1),
        ),
        (
            _StaticReader(
                error=CatalogDefinitionLoadError(
                    "catalog_all_codes_invalid",
                    "password=do-not-expose",
                )
            ),
            ((), "ubist", "catalog_all_codes_invalid", 0),
        ),
        (
            _StaticReader(StrategicMarketDefinition("ml_006", "ubist", ())),
            ((), "ubist", "catalog_definition_empty", 0),
        ),
        (
            _StaticReader(None),
            ((), "ubist", "catalog_row_absent", 0),
        ),
        (
            _StaticReader(error=OSError("mysql://user:secret@db/catalog")),
            ((), "ubist", "catalog_db_unreachable", 0),
        ),
        (
            _StaticReader(
                error=CatalogDefinitionLoadError(
                    "catalog_parse_error",
                    "password=do-not-expose",
                )
            ),
            ((), "ubist", "catalog_parse_error", 0),
        ),
    ),
    ids=(
        "a-all-valid",
        "b-partially-invalid",
        "c-all-invalid",
        "d-empty-definition",
        "e-row-absent",
        "f-db-unreachable",
        "g-parse-error",
    ),
)
def test_failure_injection_has_distinct_allowlisted_results(
    reader: _DefinitionReader,
    expected: tuple[tuple[str, ...], str, str | None, int],
) -> None:
    result = _candidate_result(reader)

    print(
        json.dumps(
            {
                "candidate_atc4_codes": result[0],
                "source": result[1],
                "reduction_reason": result[2],
                "excluded_atc4_count": result[3],
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    assert result == expected


def test_logged_exception_is_masked_and_public_result_has_no_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_secret = "do-not-expose"
    result = _candidate_result(
        _StaticReader(
            error=CatalogDefinitionLoadError(
                "catalog_parse_error",
                f"password={raw_secret}",
            )
        )
    )

    assert result == ((), "ubist", "catalog_parse_error", 0)
    assert raw_secret not in caplog.text
    assert "password=[REDACTED]" in caplog.text
