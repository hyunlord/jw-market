"""R16 - the snapshot memo must be invisible except in how long it takes.

These fix the two things that make a cache dangerous rather than fast: an
answer that changes, and a key that lets one question read another's answer.
"""
from __future__ import annotations

import time

import pytest

from jw_chat_agent_poc.tools.query_layer.store import MartRecord, MartSnapshot


def _history(**periods: float) -> dict[str, dict[str, object]]:
    return {period: {"raw_value": value, "status": "OK"} for period, value in periods.items()}


def _record(
    brand: str,
    *,
    ml_id: str = "ml_006",
    source: str = "ubist",
    measure: str = "sales",
    **periods: float,
) -> MartRecord:
    return MartRecord.from_row(
        {
            "ml_id": ml_id,
            "brand_name": brand,
            "source": source,
            "measure": measure,
            "metric_history": _history(**periods),
            "channel_data": {},
            "specialty_data": {},
            "dimension_data": {},
            "by_dimension": {"company": f"{brand}사", "molecule": f"{brand}성분"},
            "unit_label": "KRW",
        }
    )


def _snapshot(records: tuple[MartRecord, ...]) -> MartSnapshot:
    return MartSnapshot(records=records, loaded_at=time.monotonic())


UBIST_A = _record("가브랜드", **{"2026-05": 100.0, "2026-06": 200.0})
UBIST_B = _record("나브랜드", **{"2026-05": 300.0, "2026-06": 100.0})
# Stored under the key "iqvia" normalises to; the request below still says "iqvia",
# so this also shows the alias still reaches the right rows through the memo.
IQVIA_A = _record("가브랜드", source="iqvia_nsa", **{"2026-05": 11.0, "2026-06": 22.0})
VOLUME_A = _record("가브랜드", measure="volume", **{"2026-05": 7.0, "2026-06": 9.0})


def test_repeated_lookup_returns_the_same_answer_and_is_counted_as_a_hit():
    snapshot = _snapshot((UBIST_A, UBIST_B))

    first = snapshot.ranked_brands("ml_006", "2026-06")
    second = snapshot.ranked_brands("ml_006", "2026-06")

    assert first == second
    assert [row["brand"] for row in first] == ["가브랜드", "나브랜드"]
    report = snapshot.memo.observability()
    assert report["hits"] >= 1, "a repeat that is served from the memo must be recorded"
    assert report["misses"] >= 1
    assert report["entries"] >= 1


def test_source_is_part_of_the_key_so_ubist_never_reads_iqvia():
    snapshot = _snapshot((UBIST_A, IQVIA_A))

    ubist = snapshot.market_value_or_none("ml_006", "2026-06", "ubist")
    iqvia = snapshot.market_value_or_none("ml_006", "2026-06", "iqvia")

    assert ubist == 200.0
    assert iqvia == 22.0, "dropping source from the key would serve the UBIST answer here"


def test_measure_is_part_of_the_key_so_sales_never_reads_volume():
    snapshot = _snapshot((UBIST_A, VOLUME_A))

    sales = snapshot.market_value_or_none("ml_006", "2026-06", "ubist", "sales")
    volume = snapshot.market_value_or_none("ml_006", "2026-06", "ubist", "volume")

    assert sales == 200.0
    assert volume == 9.0, "dropping measure from the key would serve the sales answer here"


def test_period_is_part_of_the_key():
    snapshot = _snapshot((UBIST_A, UBIST_B))

    assert snapshot.market_value_or_none("ml_006", "2026-05") == 400.0
    assert snapshot.market_value_or_none("ml_006", "2026-06") == 300.0


def test_market_is_part_of_the_key():
    other = _record("다브랜드", ml_id="ml_009", **{"2026-06": 55.0})
    snapshot = _snapshot((UBIST_A, other))

    assert snapshot.market_value_or_none("ml_006", "2026-06") == 200.0
    assert snapshot.market_value_or_none("ml_009", "2026-06") == 55.0


def test_two_rows_sharing_a_descriptor_keep_their_own_values():
    """The key identifies the row, not a description of it.

    A descriptive key (ml_id/brand/source/measure) would make these two rows
    one entry and hand the first row's value out for the second.
    """
    twin_a = _record("같은이름", **{"2026-06": 1.0})
    twin_b = _record("같은이름", **{"2026-06": 2.0})
    snapshot = _snapshot((twin_a, twin_b))

    assert snapshot.value_or_none(twin_a, "2026-06") == 1.0
    assert snapshot.value_or_none(twin_b, "2026-06") == 2.0


def test_a_caller_annotating_ranked_rows_cannot_reach_the_next_caller():
    snapshot = _snapshot((UBIST_A, UBIST_B))

    first = snapshot.ranked_brands("ml_006", "2026-06")
    first[0]["injected_by_caller"] = True
    first[0]["value"] = -1

    second = snapshot.ranked_brands("ml_006", "2026-06")
    assert "injected_by_caller" not in second[0]
    assert second[0]["value"] == 200.0


def test_a_refreshed_snapshot_does_not_share_its_predecessor_s_memo():
    """A TTL refresh builds a new snapshot, so old answers have nowhere to live."""
    first = _snapshot((UBIST_A, UBIST_B))
    first.ranked_brands("ml_006", "2026-06")

    refreshed = _snapshot((UBIST_A, UBIST_B))
    assert refreshed.memo is not first.memo
    assert refreshed.memo.table is not first.memo.table
    assert refreshed.memo.table == {}
    ranked_keys = [key for key in first.memo.table if key[0] == "ranked_brands"]
    assert ranked_keys, "precondition: the first snapshot answered a ranked_brands call"


def test_a_snapshot_keeps_nothing_it_worked_out_while_assembling_itself():
    """Building the derived index reads every row once, so recording that pass
    would store a million entries that can never be hit -- and would fix the
    rows' values before the snapshot was finished being set up.
    """
    snapshot = _snapshot((UBIST_A, UBIST_B))
    assert snapshot.memo.armed is True
    assert snapshot.memo.table == {}
    assert snapshot.memo.lookups_recorded() == 0


def test_a_period_point_cannot_be_edited_in_place():
    """Why the memo is safe to key on a row: the points themselves are read-only.

    ``MartRecord`` is frozen, and each period in its history is a
    ``MartJsonPoint``, so a loaded value cannot be rewritten underneath a
    cached answer.
    """
    record = _record("고정브랜드", **{"2026-06": 5.0})
    with pytest.raises(TypeError):
        record.metric_history["2026-06"]["raw_value"] = 50.0


def test_a_row_replaced_before_the_first_read_is_reported_as_replaced():
    """The contract the memo depends on: rows settle before questions start."""
    edited = _record("바뀔브랜드", **{"2026-06": 5.0})
    snapshot = _snapshot((edited,))
    edited.metric_history["2026-06"] = {"raw_value": 50.0, "status": "OK"}

    assert snapshot.value_or_none(edited, "2026-06") == 50.0
    assert snapshot.market_value_or_none("ml_006", "2026-06") == 50.0


def test_a_changed_snapshot_reports_the_changed_value():
    """The failure this guards: same arguments, new data, old answer."""
    before = _snapshot((UBIST_A, UBIST_B))
    assert before.market_value_or_none("ml_006", "2026-06") == 300.0

    grown = _record("나브랜드", **{"2026-05": 300.0, "2026-06": 900.0})
    after = _snapshot((UBIST_A, grown))
    assert after.market_value_or_none("ml_006", "2026-06") == 1100.0


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.market_records("ml_006"),
        lambda s: s.periods("ml_006"),
        lambda s: s.record("ml_006", "가브랜드"),
        lambda s: s.market_value_or_none("ml_006", "2026-06"),
        lambda s: s.ranked_brands("ml_006", "2026-06"),
        lambda s: s.value_or_none(UBIST_A, "2026-06"),
        lambda s: s.value_status(UBIST_A, "2026-06"),
    ],
)
def test_every_memoised_lookup_agrees_with_the_uncached_computation(call):
    """Failure injection: force a miss and compare against the memoised answer."""
    warm = _snapshot((UBIST_A, UBIST_B))
    call(warm)
    hits_before = warm.memo.hits
    memoised = call(warm)  # this one is served from the memo
    assert warm.memo.hits > hits_before, "precondition: the repeat must be a hit"

    cold = _snapshot((UBIST_A, UBIST_B))
    cold.memo.table.clear()
    misses_before = cold.memo.misses
    forced_miss = call(cold)

    assert forced_miss == memoised
    assert cold.memo.misses > misses_before, "the forced-miss run must recompute"


def test_missing_row_still_raises_rather_than_being_cached_as_an_answer():
    snapshot = _snapshot((UBIST_A,))
    with pytest.raises(LookupError):
        snapshot.record("ml_006", "없는브랜드")
    with pytest.raises(LookupError):
        snapshot.record("ml_006", "없는브랜드")
