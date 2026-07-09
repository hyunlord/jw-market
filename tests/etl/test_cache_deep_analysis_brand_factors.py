from __future__ import annotations

import json

import pytest

from pipeline.scripts.etl.cache_deep_analysis_brand_factors import (
    CacheBrandFactorsError,
    build_brand_factor_map,
    dump_brand_factors,
    empty_brand_factors,
    load_brand_factor_map,
    quote_ident,
)


def test_build_brand_factor_map_projects_requested_contract_keys() -> None:
    factors = build_brand_factor_map(
        brands=["리바로젯", "원천없음"],
        atc_rows=[
            {"brand_name": "리바로젯", "atc4_code": "C10C"},
            {"brand_name": "리바로젯", "atc4_code": "C10C0"},
            {"brand_name": "리바로젯", "atc4_code": "C10C"},
        ],
        dimension_rows=[
            {"brand_name": "리바로젯", "source": "ubist", "dimension_type": "seller", "dimension_value": "JW중외제약"},
            {"brand_name": "리바로젯", "source": "ubist", "dimension_type": "form", "dimension_value": "정제"},
            {"brand_name": "리바로젯", "source": "ubist", "dimension_type": "route", "dimension_value": "내복"},
            {"brand_name": "리바로젯", "source": "ubist", "dimension_type": "reimbursement", "dimension_value": "급여"},
            {
                "brand_name": "리바로젯",
                "source": "ubist",
                "dimension_type": "molecule_strength",
                "dimension_value": "ezetimibe 10㎎",
            },
            {"brand_name": "리바로젯", "source": "iqvia_nsa", "dimension_type": "mfr", "dimension_value": "제이더블유중외제약"},
            {"brand_name": "리바로젯", "source": "iqvia_nsa", "dimension_type": "molecule_type", "dimension_value": "COMBINE"},
            {
                "brand_name": "리바로젯",
                "source": "iqvia_nsa",
                "dimension_type": "molecule_desc",
                "dimension_value": "EZETIMIBE+PITAVASTATIN",
            },
            {"brand_name": "리바로젯", "source": "iqvia_nsa", "dimension_type": "pack", "dimension_value": "TAB 30"},
            {"brand_name": "리바로젯", "source": "iqvia_nsa", "dimension_type": "strength", "dimension_value": "2MG"},
            {"brand_name": "리바로젯", "source": "iqvia_nsa", "dimension_type": "nhi", "dimension_value": "NHI"},
        ],
    )

    assert factors["리바로젯"] == {
        "atc": ["C10C", "C10C0"],
        "ubist": {
            "seller": ["JW중외제약"],
            "molecule_strength": ["ezetimibe 10㎎"],
            "form": ["정제"],
            "route": ["내복"],
            "reimbursement": ["급여"],
        },
        "iqvia": {
            "mfr_name_kor": ["제이더블유중외제약"],
            "molecule_type": ["COMBINE"],
            "molecule_desc": ["EZETIMIBE+PITAVASTATIN"],
            "pack_desc": ["TAB 30"],
            "strength": ["2MG"],
            "nhi_type": ["NHI"],
        },
    }
    assert factors["원천없음"] == empty_brand_factors()


def test_build_brand_factor_map_uses_compact_brand_match_when_exact_missing() -> None:
    factors = build_brand_factor_map(
        brands=["리바로 브이"],
        atc_rows=[{"brand_name": "리바로브이", "atc4_code": "C10A1"}],
        dimension_rows=[
            {"brand_name": "리바로브이", "source": "ubist", "dimension_type": "seller", "dimension_value": "JW중외제약"}
        ],
    )

    assert factors["리바로 브이"]["atc"] == ["C10A1"]
    assert factors["리바로 브이"]["ubist"]["seller"] == ["JW중외제약"]


def test_build_brand_factor_map_does_not_guess_ambiguous_compact_brand() -> None:
    factors = build_brand_factor_map(
        brands=["AB C", "A BC"],
        atc_rows=[{"brand_name": "ABC", "atc4_code": "C10A1"}],
        dimension_rows=[
            {"brand_name": "ABC", "source": "ubist", "dimension_type": "seller", "dimension_value": "JW중외제약"}
        ],
    )

    assert factors["AB C"] == empty_brand_factors()
    assert factors["A BC"] == empty_brand_factors()


def test_load_brand_factor_map_queries_compact_brand_candidates() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[str, ...]]] = []
            self._rows: list[dict[str, str]] = []

        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, params: tuple[str, ...]) -> None:
            self.calls.append((sql, params))
            if "atc4_code" in sql:
                self._rows = [{"brand_name": "리바로브이", "atc4_code": "C10A1"}]
            else:
                self._rows = [
                    {
                        "brand_name": "리바로브이",
                        "source": "ubist",
                        "dimension_type": "seller",
                        "dimension_value": "JW중외제약",
                    }
                ]

        def fetchall(self) -> list[dict[str, str]]:
            return self._rows

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_obj = FakeCursor()

        def cursor(self) -> FakeCursor:
            return self.cursor_obj

    conn = FakeConnection()

    factors = load_brand_factor_map(conn, ["리바로 브이"])

    assert factors["리바로 브이"]["atc"] == ["C10A1"]
    assert factors["리바로 브이"]["ubist"]["seller"] == ["JW중외제약"]
    assert all("REPLACE" in sql for sql, _params in conn.cursor_obj.calls)
    assert conn.cursor_obj.calls[0][1] == ("리바로 브이", "리바로브이")


def test_load_brand_factor_map_full_scan_uses_compact_brand_candidates(monkeypatch) -> None:
    # Given: a large brand batch forces the full-scan path, and the UBIST catalog
    # stores the requested brand with a display-space variant.
    class FakeCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, str]] = []

        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str) -> None:
            if "atc4_code" in sql:
                self._rows = [{"brand_name": "리바로 브이", "atc4_code": "C10A1"}]
            else:
                self._rows = [
                    {
                        "brand_name": "리바로 브이",
                        "source": "ubist",
                        "dimension_type": "seller",
                        "dimension_value": "JW중외제약",
                    }
                ]

        def fetchall(self) -> list[dict[str, str]]:
            return self._rows

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    brands = ["리바로브이", *[f"브랜드{i}" for i in range(1000)]]

    # When: the factor map is loaded through the full-scan path.
    factors = load_brand_factor_map(FakeConnection(), brands)

    # Then: the compact-only source brand populates the requested brand.
    assert factors["리바로브이"]["atc"] == ["C10A1"]
    assert factors["리바로브이"]["ubist"]["seller"] == ["JW중외제약"]


def test_build_brand_factor_map_rejects_ambiguous_source_compact_brand(caplog) -> None:
    # Given: exact lookup misses and compact fallback would merge two different
    # source catalog brands into one requested brand.
    rows = [
        {"brand_name": "AB C", "source": "ubist", "dimension_type": "seller", "dimension_value": "Seller A"},
        {"brand_name": "A BC", "source": "ubist", "dimension_type": "seller", "dimension_value": "Seller B"},
    ]

    # When: the requested brand can only be resolved by compact matching.
    factors = build_brand_factor_map(brands=["ABC"], atc_rows=[], dimension_rows=rows)

    # Then: the builder does not silently merge the ambiguous rows.
    assert factors["ABC"] == empty_brand_factors()
    assert "ambiguous compact brand factor lookup" in caplog.text


def test_dump_brand_factors_is_valid_json_with_empty_default() -> None:
    assert json.loads(dump_brand_factors(None)) == empty_brand_factors()


def test_quote_ident_rejects_unsafe_identifier() -> None:
    with pytest.raises(CacheBrandFactorsError):
        quote_ident("cache_deep_analysis;DROP")
