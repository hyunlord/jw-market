"""P0: multi-ATC4 competitor coverage + deterministic market-row selection.

Regression guard for audit ``2552f439``:

* ① A general-view request that selects several ATC4 codes must include
  competitors that live only in a *secondary* selected ATC4 (e.g. C10C0),
  not just the anchor (atc4[0]).
* (b) ``_fetch_market_row`` must be deterministically ordered.
* (a) topic-scope selection must be stable regardless of stored-row order.
"""

from __future__ import annotations

from types import SimpleNamespace

from pipeline.scripts.api import brand_activity_brand_resolver as resolver
from pipeline.scripts.api import brand_activity_topic_matrix as topic_matrix
from pipeline.scripts.api.brand_activity_brand_resolver import resolve_brand_set


# (brand_key, atc4_code, rank, sales)
_CATALOG = (
    ("선택", "C10A1", 3, 1.0),
    ("아토젯", "C10A1", 1, 100.0),
    ("리바로젯", "C10C0", 1, 80.0),  # present ONLY in the secondary ATC4
)


def _brand_row(brand_key: str, atc4_code: str, rank: int, sales: float) -> dict[str, object]:
    return {
        "brand_key": brand_key,
        "brand_name": brand_key,
        "by_dimension": {"products": [{"product_code": brand_key}], "atc4_code": [atc4_code]},
        "overlay_data": {},
        "metric_history": {"2026-Q2": {"rank": rank, "raw_value": sales}},
        "audit_code_matrix": {},
    }


def _patch(monkeypatch, catalog=_CATALOG, *, membership=("C10A1",)) -> list[tuple[object, ...]]:
    monkeypatch.setattr(resolver, "general_molecules_by_product", lambda _metas: {})
    monkeypatch.setattr(resolver, "general_brand_atc4_values", lambda **_kw: membership, raising=False)
    brand_calls: list[tuple[object, ...]] = []

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        if "mart_general_brand_metric" in sql:
            brand_calls.append(tuple(params))
            atc4_ids = {str(value) for value in params[:-2]}  # (*atc4, source, measure)
            rows = [
                _brand_row(brand_key, code, rank, sales)
                for (brand_key, code, rank, sales) in catalog
                if code in atc4_ids
            ]
            rows.sort(key=lambda row: (row["brand_key"], row["by_dimension"]["atc4_code"][0]))
            return rows
        if "mart_general_filter_dimension_metric" in sql:
            return []
        raise AssertionError(f"unexpected sql: {sql}")

    def fake_fetch_one(_sql: str, params: tuple[object, ...]) -> dict[str, object]:
        anchor = str(params[0])
        return {
            "atc4_code": anchor,
            "atc4_desc": f"Market {anchor}",
            "market_size_series": {},
            "brand_ranking": {
                "2026-Q2": [
                    {"brand_key": brand_key, "rank": rank, "raw_value": sales}
                    for (brand_key, code, rank, sales) in catalog
                    if code == anchor
                ]
            },
        }

    monkeypatch.setattr(resolver.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(resolver.db, "fetch_one", fake_fetch_one)
    return brand_calls


def _keys(monkeypatch_result) -> list[str]:
    return [choice.brand_key for choice in monkeypatch_result.choices]


def _resolve(atc4: list[str]):
    return resolve_brand_set(
        view_name="general",
        market_id=atc4[0],
        selected_brand="선택",
        filter_payload={"atc4": atc4},
        ranking_quarters=("2026-Q2",),
    )


def test_multi_atc4_includes_brand_present_only_in_secondary_atc4(monkeypatch) -> None:
    _patch(monkeypatch)

    result = _resolve(["C10A1", "C10C0"])

    assert result is not None
    assert "리바로젯" in _keys(result)


def test_single_vs_multi_atc4_produce_different_brand_sets(monkeypatch) -> None:
    _patch(monkeypatch)

    single = _resolve(["C10A1"])
    multi = _resolve(["C10A1", "C10C0"])

    assert single is not None and multi is not None
    assert "리바로젯" not in _keys(single)
    assert "리바로젯" in _keys(multi)
    assert set(_keys(single)) != set(_keys(multi))


def test_brand_query_binds_all_selected_atc4_codes(monkeypatch) -> None:
    calls = _patch(monkeypatch)

    _resolve(["C10A1", "C10C0"])

    assert any({"C10A1", "C10C0"} <= {str(value) for value in params[:-2]} for params in calls)


def test_multi_atc4_dedupes_brand_present_in_both_markets(monkeypatch) -> None:
    catalog = (*_CATALOG, ("리바로", "C10A1", 2, 60.0), ("리바로", "C10C0", 5, 40.0))
    _patch(monkeypatch, catalog)

    result = _resolve(["C10A1", "C10C0"])

    assert result is not None
    keys = _keys(result)
    assert "리바로젯" in keys  # RED today: secondary-ATC4 brand is dropped
    assert keys.count("리바로") == 1  # brand in both markets appears exactly once


def test_repeated_multi_atc4_calls_are_deterministic(monkeypatch) -> None:
    catalog = (*_CATALOG, ("리바로", "C10A1", 2, 60.0), ("리바로", "C10C0", 5, 40.0))
    _patch(monkeypatch, catalog)

    sequences = {tuple(_keys(_resolve(["C10A1", "C10C0"]))) for _ in range(5)}

    assert len(sequences) == 1


def test_single_atc4_request_is_unchanged(monkeypatch) -> None:
    calls = _patch(monkeypatch)

    result = _resolve(["C10A1"])

    assert result is not None
    assert calls[0][:3] == ("C10A1", "iqvia_nsa", "sales")
    assert _keys(result) == ["선택", "아토젯"]


def test_fetch_market_row_is_deterministically_ordered(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_fetch_one(sql: str, _params: tuple[object, ...]) -> None:
        captured["sql"] = sql
        return None

    monkeypatch.setattr(resolver.db, "fetch_one", fake_fetch_one)

    resolver._fetch_market_row(resolver.view_config("general"), "C10A1")

    assert "ORDER BY" in captured["sql"]


def test_topic_scope_tie_break_is_independent_of_row_order() -> None:
    brand_set = SimpleNamespace(
        view_name="general",
        market_id="C10A1",
        applied_filter={"atc4": ["C10A1", "C10C0"]},
    )
    row_a = {"scope_id": "group:aaa", "atc4_values": ["C10A1", "C10C0", "X99Z9"]}
    row_b = {"scope_id": "group:bbb", "atc4_values": ["C10A1", "C10C0", "Y99Z9"]}

    forward = topic_matrix._topic_scope(brand_set=brand_set, topic_rows=[row_a, row_b])
    reverse = topic_matrix._topic_scope(brand_set=brand_set, topic_rows=[row_b, row_a])

    assert forward["scope_id"] == "group:aaa"
    assert reverse["scope_id"] == "group:aaa"
