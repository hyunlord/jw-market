from __future__ import annotations

from pipeline.scripts.api.brand_activity_brand_filters import applied_brand_filter
from pipeline.scripts.api.brand_activity_brand_resolver import (
    BrandCandidate,
    BrandSetInputError,
    _brand_candidates,
    _ml_id_for_brand,
    _select_choices,
    validate_audit_code_axis,
)
from pipeline.scripts.api.brand_activity_channel_axis import (
    audit_code_sales_value,
    parse_audit_code_axis,
)
from pipeline.scripts.api.brand_activity_csd_shared import BrandMeta


def test_brand_filter_uses_or_within_dimension_and_and_across_dimensions() -> None:
    candidates = (
        _candidate("선택", rank=99, sales=1.0, dimensions={"atc4": ("C10A1",), "molecule": ("other",), "class": ("Z",)}),
        _candidate("A", rank=1, sales=100.0, dimensions={"atc4": ("C10A1",), "molecule": ("pitavastatin",), "class": ("스타틴",)}),
        _candidate("B", rank=2, sales=90.0, dimensions={"atc4": ("C10A1",), "molecule": ("pitavastatin",), "class": ("복합제",)}),
        _candidate("C", rank=3, sales=80.0, dimensions={"atc4": ("C10A1",), "molecule": ("ezetimibe",), "class": ("스타틴",)}),
        _candidate("D", rank=4, sales=70.0, dimensions={"atc4": ("C10C0",), "molecule": ("pitavastatin",), "class": ("스타틴",)}),
    )
    applied = {"atc4": ["C10A1"], "molecule": ["pitavastatin"], "class": ["스타틴", "복합제"]}

    choices = _select_choices(candidates, selected_brand="선택", applied_filter=applied)

    assert [choice.brand_key for choice in choices] == ["선택", "A", "B"]
    assert choices[0].is_selected is True


def test_general_default_filter_applies_market_atc4() -> None:
    assert applied_brand_filter("general", "c10a1", {}) == {"atc4": ["C10A1"]}


def test_applied_filter_echoes_audit_code_and_ignores_ubist_channel_axis() -> None:
    payload = {
        "analysis_level": {
            "iqvia": {"audit_code": ["khpa", "KCPA", "khpa"]},
            "ubist": {"facility": ["의원"], "specialty": ["순환기(Cardiology IM)"]},
        }
    }

    applied = applied_brand_filter("general", "C10A1", payload)

    assert applied["atc4"] == ["C10A1"]
    assert applied["channel_axis"] == {"source": "iqvia_nsa", "audit_code": ["KHPA", "KCPA"]}
    assert applied_brand_filter("general", "C10A1", {"analysis_level": {"ubist": {"facility": ["의원"]}}}) == {
        "atc4": ["C10A1"]
    }
    assert "channel_axis" not in applied_brand_filter("strategic_ml", "ml_006", payload)


def test_applied_filter_accepts_iqvia_dimension_filters() -> None:
    payload = {
        "analysis_level": {
            "iqvia": {
                "mfr_name_kor": ["제이더블유중외제약"],
                "molecule_type": ["SINGLE"],
                "molecule_desc": ["TOCILIZUMAB"],
                "pack_desc": [" PFS 162MG/0.9ML "],
                "strength": ["162MG"],
                "nhi_type": ["NHI"],
                "audit_code": ["khpa"],
            }
        }
    }

    applied = applied_brand_filter("general", "M01C0", payload)

    assert applied == {
        "atc4": ["M01C0"],
        "mfr": ["제이더블유중외제약"],
        "molecule_type": ["SINGLE"],
        "molecule_desc": ["TOCILIZUMAB"],
        "pack": ["PFS 162MG/0.9ML"],
        "strength": ["162MG"],
        "nhi": ["NHI"],
        "channel_axis": {"source": "iqvia_nsa", "audit_code": ["KHPA"]},
    }


def test_audit_code_sales_value_sums_selected_matrix_codes_for_quarter() -> None:
    channel_axis = parse_audit_code_axis({"analysis_level": {"iqvia": {"audit_code": ["KHPA", "KCPA"]}}})
    row = {
        "audit_code_matrix": {
            "KHPA": {"2026-04": 10, "2026-05": 20},
            "KCPA": {"2026-Q2": 7},
            "KPA": {"2026-Q2": 100},
        }
    }

    assert audit_code_sales_value(row, channel_axis, "2026-Q2") == 37.0


def test_unknown_audit_code_is_rejected_from_dynamic_matrix_keys() -> None:
    channel_axis = parse_audit_code_axis({"analysis_level": {"iqvia": {"audit_code": ["BAD"]}}})

    try:
        validate_audit_code_axis(({"audit_code_matrix": {"KHPA": {"2026-Q2": 1}}},), channel_axis)
    except BrandSetInputError as exc:
        assert "unsupported audit_code" in str(exc)
        assert "BAD" in str(exc)
    else:
        raise AssertionError("unknown audit_code must be rejected")


def test_single_ml_brand_resolves_market(monkeypatch) -> None:
    rows = [{"ml_id": "ml_006"}]
    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_all", lambda *_args, **_kwargs: rows)

    assert _ml_id_for_brand("가드렛") == "ml_006"


def test_ambiguous_ml_brand_raises_without_market_id_escape_hatch(monkeypatch) -> None:
    rows = [{"ml_id": "ml_010"}, {"ml_id": "ml_002"}, {"ml_id": "ml_006"}]
    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_all", lambda *_args, **_kwargs: rows)

    try:
        _ml_id_for_brand("가드렛")
    except BrandSetInputError as exc:
        message = str(exc)
        assert message == "ambiguous ml market for brand: ml_010, ml_002, ml_006"
        assert "pass market_id" not in message
    else:
        raise AssertionError("ambiguous ML brand must fail loudly")


def test_audit_code_axis_replaces_candidate_sales_ranking_value(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.general_molecules_by_product",
        lambda _metas: {},
    )
    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_all", lambda *_args, **_kwargs: [])
    rows = (
        {
            "brand_key": "선택",
            "by_dimension": {"products": [{"product_code": "SEL"}], "atc4_code": ["C10A1"]},
            "metric_history": {"2026-Q2": {"raw_value": 1, "rank": 99}},
            "audit_code_matrix": {"KHPA": {"2026-Q2": 1}},
        },
        {
            "brand_key": "A",
            "by_dimension": {"products": [{"product_code": "A"}], "atc4_code": ["C10A1"]},
            "metric_history": {"2026-Q2": {"raw_value": 100, "rank": 1}},
            "audit_code_matrix": {"KHPA": {"2026-Q2": 3}},
        },
        {
            "brand_key": "B",
            "by_dimension": {"products": [{"product_code": "B"}], "atc4_code": ["C10A1"]},
            "metric_history": {"2026-Q2": {"raw_value": 20, "rank": 2}},
            "audit_code_matrix": {"KHPA": {"2026-Q2": 50}},
        },
    )
    metas = {
        str(row["brand_key"]): BrandMeta(str(row["brand_key"]), str(row["brand_key"]), (str(row["brand_key"]),), False)
        for row in rows
    }
    channel_axis = parse_audit_code_axis({"analysis_level": {"iqvia": {"audit_code": ["KHPA"]}}})

    candidates = _brand_candidates(
        "general",
        rows,
        metas,
        {"quarter": "2026-Q2", "items": [{"brand_key": "A", "rank": 1, "raw_value": 100}]},
        audit_code_axis=channel_axis,
    )
    choices = _select_choices(candidates, selected_brand="선택", applied_filter={"atc4": ["C10A1"]})

    assert [candidate.sales_value for candidate in candidates] == [1.0, 3.0, 50.0]
    assert [choice.brand_key for choice in choices] == ["선택", "B", "A"]


def test_general_brand_candidates_load_iqvia_sidecar_dimensions(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.general_molecules_by_product",
        lambda _metas: {},
    )

    def fake_fetch_all(sql: str, params: tuple) -> list[dict[str, str]]:
        assert "mart_general_filter_dimension_metric" in sql
        return [
            {"brand_key": "A", "dimension_type": "mfr", "dimension_value_norm": "제이더블유중외제약"},
            {"brand_key": "A", "dimension_type": "pack", "dimension_value_norm": "PFS 162MG/0.9ML"},
            {"brand_key": "B", "dimension_type": "mfr", "dimension_value_norm": "경쟁사"},
        ]

    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_all", fake_fetch_all)
    rows = (
        {
            "brand_key": "선택",
            "by_dimension": {"products": [{"product_code": "SEL"}], "atc4_code": ["M01C0"]},
            "metric_history": {"2026-Q2": {"raw_value": 1, "rank": 99}},
        },
        {
            "brand_key": "A",
            "by_dimension": {"products": [{"product_code": "A"}], "atc4_code": ["M01C0"]},
            "metric_history": {"2026-Q2": {"raw_value": 100, "rank": 1}},
        },
        {
            "brand_key": "B",
            "by_dimension": {"products": [{"product_code": "B"}], "atc4_code": ["M01C0"]},
            "metric_history": {"2026-Q2": {"raw_value": 90, "rank": 2}},
        },
    )
    metas = {
        str(row["brand_key"]): BrandMeta(str(row["brand_key"]), str(row["brand_key"]), (str(row["brand_key"]),), False)
        for row in rows
    }

    candidates = _brand_candidates(
        "general",
        rows,
        metas,
        {
            "quarter": "2026-Q2",
            "items": [
                {"brand_key": "A", "rank": 1, "raw_value": 100},
                {"brand_key": "B", "rank": 2, "raw_value": 90},
            ],
        },
    )
    choices = _select_choices(
        candidates,
        selected_brand="선택",
        applied_filter={"atc4": ["M01C0"], "mfr": ["제이더블유중외제약"]},
    )

    assert [candidate.dimensions.get("mfr", ()) for candidate in candidates] == [(), ("제이더블유중외제약",), ("경쟁사",)]
    assert [choice.brand_key for choice in choices] == ["선택", "A"]


def _candidate(brand_key: str, *, rank: int, sales: float, dimensions: dict[str, tuple[str, ...]]) -> BrandCandidate:
    return BrandCandidate(
        meta=BrandMeta(brand_key=brand_key, brand_name=brand_key, product_codes=(brand_key.upper(),), is_jw=False),
        dimensions=dimensions,
        sales_rank=rank,
        sales_value=sales,
    )
