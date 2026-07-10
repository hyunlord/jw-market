from __future__ import annotations

from pipeline.scripts.api.brand_activity_brand_filters import applied_brand_filter
from pipeline.scripts.api.brand_activity_brand_resolver import (
    BrandCandidate,
    BrandSetInputError,
    _brand_candidates,
    _ml_id_for_brand,
    _select_choices,
    resolve_brand_set,
    validate_audit_code_axis,
)
from pipeline.scripts.api.brand_activity_channel_axis import (
    audit_code_sales_value,
    parse_audit_code_axis,
)
from pipeline.scripts.api.brand_activity_csd_shared import BrandMeta
from pipeline.scripts.api.models.brand_activity import BrandActivityTopicsRequest


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


def test_applied_filter_accepts_ubist_dimension_filters_and_camel_case_alias() -> None:
    request = BrandActivityTopicsRequest.model_validate(
        {
            "selected_brand": "리바로",
            "filters": {
                "atc4": ["C10A1"],
                "analysis_level": {
                    "ubist": {
                        "seller": ["JW중외제약"],
                        "molecule": ["Pitavastatin"],
                        "moleculeStrength": ["pitavastatin calcium 2mg"],
                        "form": ["정제"],
                        "route": ["경구"],
                        "reimbursement": ["급여"],
                    }
                },
            },
        }
    )
    payload = request.model_dump()["filters"]

    applied = applied_brand_filter("general", "C10A1", payload)

    assert applied == {
        "atc4": ["C10A1"],
        "ubist_seller": ["JW중외제약"],
        "ubist_molecule": ["pitavastatin"],
        "ubist_molecule_strength": ["pitavastatin calcium 2mg"],
        "ubist_form": ["정제"],
        "ubist_route": ["경구"],
        "ubist_reimbursement": ["급여"],
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


def test_general_market_scope_member_resolves_livalo_to_member_atc4(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    _patch_resolver_db(monkeypatch, calls)

    result = resolve_brand_set(
        view_name="general",
        market_id=None,
        selected_brand="리바로",
        filter_payload={"market_scope": {"option_id": "group:livalo_family", "member": "리바로"}},
        ranking_quarters=("2026-Q2",),
    )

    assert result is not None
    assert result.market_id == "C10A1"
    assert result.applied_filter["atc4"] == ["C10A1"]
    assert ("brand", ("C10A1", "iqvia_nsa", "sales")) in calls
    assert ("market", ("C10A1", "iqvia_nsa", "sales")) in calls


def test_general_market_scope_member_resolves_livalozet_to_member_atc4(monkeypatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    _patch_resolver_db(monkeypatch, calls)

    result = resolve_brand_set(
        view_name="general",
        market_id=None,
        selected_brand="리바로젯",
        filter_payload={"market_scope": {"option_id": "group:livalo_family", "member": "리바로젯"}},
        ranking_quarters=("2026-Q2",),
    )

    assert result is not None
    assert result.market_id == "C10C0"
    assert result.applied_filter["atc4"] == ["C10C0"]
    assert ("brand", ("C10C0", "iqvia_nsa", "sales")) in calls


def test_general_market_scope_rejects_whole_group_member(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "group:livalo_family", "member": "전체"}},
        )
    except BrandSetInputError as exc:
        assert "unsupported_market_scope_member" in str(exc)
    else:
        raise AssertionError("whole group member must be rejected in Phase 1")


def test_general_market_scope_rejects_missing_member(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "group:livalo_family"}},
        )
    except BrandSetInputError as exc:
        assert "unsupported_market_scope_member" in str(exc)
    else:
        raise AssertionError("omitted member must be rejected in Phase 1")


def test_general_market_scope_rejects_unknown_member(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "group:livalo_family", "member": "존재안함"}},
        )
    except BrandSetInputError as exc:
        assert "invalid_market_scope_member" in str(exc)
    else:
        raise AssertionError("unknown member must be rejected")


def test_general_market_scope_rejects_unknown_option(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "group:not_found", "member": "리바로"}},
        )
    except BrandSetInputError as exc:
        assert "invalid_market_scope" in str(exc)
    else:
        raise AssertionError("unknown option_id must be rejected")


def test_general_market_scope_rejects_non_group_option(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "source:strategy_006", "member": "리바로"}},
        )
    except BrandSetInputError as exc:
        assert "invalid_market_scope" in str(exc)
    else:
        raise AssertionError("non-group option must be rejected")


def test_general_market_scope_rejects_multi_atc4_member(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="general",
            market_id=None,
            selected_brand="위너프A+",
            filter_payload={"market_scope": {"option_id": "group:winuf_family", "member": "위너프A+"}},
        )
    except BrandSetInputError as exc:
        assert "unsupported_member_scope" in str(exc)
    else:
        raise AssertionError("multi-ATC4 member must be rejected in Phase 1")


def test_strategic_ml_market_scope_is_rejected(monkeypatch) -> None:
    _patch_resolver_db(monkeypatch, [])

    try:
        resolve_brand_set(
            view_name="strategic_ml",
            market_id=None,
            selected_brand="리바로",
            filter_payload={"market_scope": {"option_id": "group:livalo_family", "member": "리바로"}},
        )
    except BrandSetInputError as exc:
        assert "unsupported_view_for_market_scope" in str(exc)
    else:
        raise AssertionError("strategic_ml market_scope must be rejected")


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


def test_brand_choices_rank_competitors_by_total_sales_before_snapshot_rank() -> None:
    candidates = (
        _candidate("선택", rank=99, sales=1.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("A", rank=1, sales=10.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("B", rank=5, sales=50.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("C", rank=2, sales=50.0, dimensions={"atc4": ("C10A1",)}),
    )

    choices = _select_choices(candidates, selected_brand="선택", applied_filter={"atc4": ["C10A1"]})

    assert [choice.brand_key for choice in choices] == ["선택", "B", "C", "A"]


def test_brand_choices_can_rank_competitors_by_latest_market_rank() -> None:
    candidates = (
        _candidate("선택", rank=3, sales=1.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("A", rank=1, sales=10.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("B", rank=5, sales=500.0, dimensions={"atc4": ("C10A1",)}),
        _candidate("C", rank=2, sales=50.0, dimensions={"atc4": ("C10A1",)}),
    )

    choices = _select_choices(
        candidates,
        selected_brand="선택",
        applied_filter={"atc4": ["C10A1"]},
        rank_by_latest_period=True,
    )

    assert [choice.brand_key for choice in choices] == ["선택", "A", "C", "B"]
    assert [choice.sales_rank for choice in choices] == [3, 1, 2, 5]


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


def test_general_brand_candidates_apply_ubist_sidecar_dimensions(monkeypatch) -> None:
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.general_molecules_by_product",
        lambda _metas: {},
    )

    def fake_fetch_all(sql: str, params: tuple) -> list[dict[str, str]]:
        assert "mart_general_filter_dimension_metric" in sql
        source = str(params[0])
        if source == "iqvia_nsa":
            return []
        assert source == "ubist"
        return [
            {"brand_key": "A", "dimension_type": "seller", "dimension_value_norm": "JW중외제약"},
            {"brand_key": "A", "dimension_type": "molecule", "dimension_value_norm": "pitavastatin"},
            {"brand_key": "B", "dimension_type": "seller", "dimension_value_norm": "경쟁사"},
            {"brand_key": "B", "dimension_type": "molecule", "dimension_value_norm": "atorvastatin"},
        ]

    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_all", fake_fetch_all)
    rows = (
        {
            "brand_key": "선택",
            "by_dimension": {"products": [{"product_code": "SEL"}], "atc4_code": ["C10A1"]},
            "metric_history": {"2026-Q2": {"raw_value": 1, "rank": 99}},
        },
        {
            "brand_key": "A",
            "by_dimension": {"products": [{"product_code": "A"}], "atc4_code": ["C10A1"]},
            "metric_history": {"2026-Q2": {"raw_value": 100, "rank": 1}},
        },
        {
            "brand_key": "B",
            "by_dimension": {"products": [{"product_code": "B"}], "atc4_code": ["C10A1"]},
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
        applied_filter={
            "atc4": ["C10A1"],
            "ubist_seller": ["JW중외제약"],
            "ubist_molecule": ["pitavastatin"],
        },
    )

    assert [candidate.dimensions.get("ubist_seller", ()) for candidate in candidates] == [(), ("JW중외제약",), ("경쟁사",)]
    assert [candidate.dimensions.get("ubist_molecule", ()) for candidate in candidates] == [(), ("pitavastatin",), ("atorvastatin",)]
    assert [choice.brand_key for choice in choices] == ["선택", "A"]


def _candidate(brand_key: str, *, rank: int, sales: float, dimensions: dict[str, tuple[str, ...]]) -> BrandCandidate:
    return BrandCandidate(
        meta=BrandMeta(brand_key=brand_key, brand_name=brand_key, product_codes=(brand_key.upper(),), is_jw=False),
        dimensions=dimensions,
        sales_rank=rank,
        sales_value=sales,
    )


def _patch_resolver_db(monkeypatch, calls: list[tuple[str, tuple[object, ...]]]) -> None:
    monkeypatch.setattr(
        "pipeline.scripts.api.brand_activity_brand_resolver.general_molecules_by_product",
        lambda _metas: {},
    )

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        if "mart_general_brand_metric" in sql:
            calls.append(("brand", tuple(params)))
            return [
                _brand_row("리바로", "리바로", "LIVALO", str(params[0]), 1, 100.0),
                _brand_row("리바로젯", "리바로젯", "LIVALOZET", str(params[0]), 2, 80.0),
                _brand_row("경쟁", "경쟁", "COMP", str(params[0]), 3, 50.0),
            ]
        if "mart_general_filter_dimension_metric" in sql:
            return []
        raise AssertionError(f"unexpected sql: {sql}")

    def fake_fetch_one(sql: str, params: tuple[object, ...]) -> dict[str, object]:
        calls.append(("market", tuple(params)))
        return {
            "atc4_code": str(params[0]),
            "atc4_desc": f"Market {params[0]}",
            "brand_ranking": {
                "2026-Q2": [
                    {"brand_key": "리바로", "rank": 1, "raw_value": 100.0},
                    {"brand_key": "리바로젯", "rank": 2, "raw_value": 80.0},
                    {"brand_key": "경쟁", "rank": 3, "raw_value": 50.0},
                ]
            },
        }

    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_all", fake_fetch_all)
    monkeypatch.setattr("pipeline.scripts.api.brand_activity_brand_resolver.db.fetch_one", fake_fetch_one)


def _brand_row(
    brand_key: str,
    brand_name: str,
    product_code: str,
    atc4_code: str,
    rank: int,
    sales: float,
) -> dict[str, object]:
    return {
        "brand_key": brand_key,
        "brand_name": brand_name,
        "by_dimension": {"products": [{"product_code": product_code}], "atc4_code": [atc4_code]},
        "metric_history": {"2026-Q2": {"rank": rank, "raw_value": sales}},
        "audit_code_matrix": {},
    }
