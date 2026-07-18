from __future__ import annotations

import json

from pipeline.scripts.etl import build_cache_cause, build_cache_deep_analysis
from pipeline.scripts.etl.cache_refresh import cache_deep_analysis_events_update


def _market() -> dict[str, object]:
    return {
        "analyze_class": 1,
        "analyze_molecule": 1,
        "analyze_dosage_form": 0,
        "analyze_strength_pack": 0,
        "analyze_nhi_type": 0,
        "analyze_ox_gx": 0,
    }


def _row(*, generic: str | None, class_1: str | None, class_2: str | None) -> dict[str, object]:
    return {
        "brand_name": "확장브랜드",
        "source": "ubist",
        "measure": "sales",
        "metric_history": json.dumps({"2026-01": {"raw_value": 100.0}}),
        "by_dimension": json.dumps(
            {"class": generic, "class_1": class_1, "class_2": class_2},
            ensure_ascii=False,
        ),
        "dimension_data": "{}",
        "dimension_channel_data": "{}",
        "dimension_specialty_data": "{}",
        "channel_data": "{}",
        "overlay_data": "{}",
    }


def test_phase30_eligibility_depends_on_inputs_not_brand_membership(monkeypatch) -> None:
    monkeypatch.setattr(build_cache_deep_analysis, "build_phase30_forecast_brand_entry", object())
    monkeypatch.setattr(build_cache_deep_analysis, "build_phase30_market_forecast", object())
    monkeypatch.setattr(build_cache_deep_analysis, "phase30_forecast_steps", object())
    row = _row(generic=None, class_1=None, class_2=None)

    available, reason = build_cache_deep_analysis._phase30_input_status(row, [row])

    assert available is True
    assert reason is None


def test_phase30_missing_history_is_explicitly_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(build_cache_deep_analysis, "build_phase30_forecast_brand_entry", object())
    monkeypatch.setattr(build_cache_deep_analysis, "build_phase30_market_forecast", object())
    monkeypatch.setattr(build_cache_deep_analysis, "phase30_forecast_steps", object())
    row = _row(generic=None, class_1=None, class_2=None)
    row["metric_history"] = "{}"

    available, reason = build_cache_deep_analysis._phase30_input_status(row, [row])
    payload = build_cache_deep_analysis.empty_combo_payload(
        "UBIST",
        "sales",
        "확장브랜드",
        row,
        reason=reason,
    )

    assert available is False
    assert reason == "missing_target_history"
    assert payload["baseline"]["value_recent"] is None
    assert payload["availability"] == {
        "status": "not_generated",
        "reason": "missing_target_history",
    }


def test_phase30_null_only_history_is_not_treated_as_zero_input(monkeypatch) -> None:
    monkeypatch.setattr(build_cache_deep_analysis, "build_phase30_forecast_brand_entry", object())
    monkeypatch.setattr(build_cache_deep_analysis, "build_phase30_market_forecast", object())
    monkeypatch.setattr(build_cache_deep_analysis, "phase30_forecast_steps", object())
    row = _row(generic=None, class_1=None, class_2=None)
    row["metric_history"] = json.dumps({"2026-01": {"raw_value": None}})

    available, reason = build_cache_deep_analysis._phase30_input_status(row, [row])

    assert available is False
    assert reason == "missing_target_history"


def test_events_are_built_for_noncanonical_brand(monkeypatch) -> None:
    calls: list[str] = []

    def fake_build(_conn: object, brand: str) -> dict[str, object]:
        calls.append(brand)
        return {"cut_a": [], "cut_b": [], "meta": {"lookback_months": 6}}

    monkeypatch.setattr(build_cache_deep_analysis, "build_events_for_cache", fake_build)

    events, meta = build_cache_deep_analysis._rebuild_events_payload_for_brand(object(), "확장브랜드")

    assert calls == ["확장브랜드"]
    assert events == []
    assert meta == {
        "status": "no_news",
        "reason": "no_events_for_brand",
        "generation_status": "generated",
    }


def test_event_refresh_verifier_ignores_event_owned_metadata() -> None:
    before = json.dumps({"data": {"forecast": {"value": 1}, "events": []}})
    after = json.dumps(
        {
            "data": {
                "forecast": {"value": 1},
                "events": [{"id": "event-1"}],
                "events_meta": {"status": "available"},
            }
        }
    )

    assert cache_deep_analysis_events_update.strip_event_fields_from_raw(before) == (
        cache_deep_analysis_events_update.strip_event_fields_from_raw(after)
    )


def test_events_only_refresh_writes_events_and_metadata(monkeypatch) -> None:
    inserted: list[tuple[object, ...]] = []

    class FakeCursor:
        def execute(self, sql: str) -> None:
            self.sql = sql

        def fetchall(self) -> list[dict[str, object]]:
            if "SELECT brand, market_id" not in self.sql:
                return []
            return [
                {
                    "brand": "확장브랜드",
                    "market_id": "ml_x",
                    "response_json": json.dumps({"data": {"forecast": {"value": 1}}}),
                    "brand_factors": None,
                }
            ]

        def executemany(self, _sql: str, rows: list[tuple[object, ...]]) -> None:
            inserted.extend(rows)

        def close(self) -> None:
            return None

    class FakeConnection:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def commit(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(build_cache_deep_analysis, "mariadb_connect", FakeConnection)
    monkeypatch.setattr(build_cache_deep_analysis, "ensure_events_raw_synced", lambda _conn: None)
    monkeypatch.setattr(
        build_cache_deep_analysis,
        "_rebuild_events_payload_for_brand",
        lambda _conn, _brand: ([{"id": "event-1"}], {"status": "available"}),
    )

    build_cache_deep_analysis.update_events_only("cache_deep_analysis_stage")

    payload = json.loads(str(inserted[0][2]))
    assert payload["data"]["forecast"] == {"value": 1}
    assert payload["data"]["events"] == [{"id": "event-1"}]
    assert payload["data"]["events_meta"] == {"status": "available"}


def test_split_class_plan_preserves_independent_generic_axis() -> None:
    rows = [_row(generic="ARB", class_1="Statin/CCB", class_2="Statin/CCB")]

    levels = build_cache_cause._response_levels(_market(), None, rows)

    assert levels == ["Class", "Class 1", "Molecule", "Brand"]


def test_split_class_plan_keeps_class2_alias_contract() -> None:
    rows = [_row(generic="TNF-a", class_1="Biologics", class_2="TNF-a")]

    levels = build_cache_cause._response_levels(_market(), None, rows)

    assert levels == ["Class 1", "Class 2", "Molecule", "Brand"]


def test_split_class_plan_preserves_class1_when_it_is_the_only_split_axis() -> None:
    rows = [_row(generic=None, class_1="Biologics", class_2=None)]

    levels = build_cache_cause._response_levels(_market(), None, rows)

    assert levels == ["Class 1", "Molecule", "Brand"]


def test_split_class_plan_preserves_class2_when_it_is_the_only_split_axis() -> None:
    rows = [_row(generic=None, class_1=None, class_2="TNF-a")]

    levels = build_cache_cause._response_levels(_market(), None, rows)

    assert levels == ["Class 2", "Molecule", "Brand"]


def test_split_class_plan_preserves_all_independent_axes() -> None:
    rows = [_row(generic="Generic", class_1="Primary", class_2="Secondary")]

    levels = build_cache_cause._response_levels(_market(), None, rows)

    assert levels == ["Class", "Class 1", "Class 2", "Molecule", "Brand"]


def test_class1_rows_are_not_silently_dropped() -> None:
    rows = [
        _row(generic="ARB", class_1=f"split-{index % 7}", class_2=f"split-{index % 7}")
        for index in range(85)
    ]

    payload = build_cache_cause._build_analysis_levels_from_mart(
        rows=rows,
        source="UBIST",
        market=_market(),
        view_source_id=None,
        target_name=None,
        fallback_level_top5={},
        channels_override=["전체"],
    )

    assert "Class 1" in payload["levels"]
    assert payload["data"]["Class 1"]["segments"] == [
        "전체",
        "split-0",
        "split-1",
        "split-2",
        "split-3",
        "split-4",
        "split-5",
        "split-6",
    ]


def test_analysis_level_builder_reuses_resolved_plan(monkeypatch) -> None:
    rows = [_row(generic="ARB", class_1="Statin/CCB", class_2="Statin/CCB")]
    calls = {"levels": 0, "periods": 0}

    def fail_levels(*_args: object, **_kwargs: object) -> list[str]:
        calls["levels"] += 1
        raise AssertionError("resolved levels should be reused")

    def fail_periods(*_args: object, **_kwargs: object) -> list[str]:
        calls["periods"] += 1
        raise AssertionError("resolved periods should be reused")

    monkeypatch.setattr(build_cache_cause, "_strategic_levels", fail_levels)
    monkeypatch.setattr(build_cache_cause, "_history_periods", fail_periods)

    payload = build_cache_cause._build_analysis_levels_from_mart(
        rows=rows,
        source="UBIST",
        market=_market(),
        view_source_id=None,
        target_name=None,
        fallback_level_top5={},
        channels_override=["전체"],
        resolved_levels={"Class", "Class 1", "Molecule"},
        resolved_periods=["2026-01"],
    )

    assert payload["periods_monthly"] == ["2026-01"]
    assert calls == {"levels": 0, "periods": 0}
