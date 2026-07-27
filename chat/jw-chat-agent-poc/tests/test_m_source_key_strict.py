"""_source_key must not fold unknown source labels onto UBIST.

The fold returned a populated set of UBIST rows for a source nobody requested, and the
padded IQVIA forms returned UBIST rows while render.source_label tagged them IQVIA. These
contracts pin the accepted set, the rejection, and the fact that the two known labels are
untouched.
"""

from __future__ import annotations

import pytest

from jw_chat_agent_poc.tools.query_layer.render import source_label
from jw_chat_agent_poc.tools.query_layer.store import (
    MartRecord,
    MartSnapshot,
    _source_key,
)

_UBIST_ROW = {
    "ml_id": "ml_006",
    "brand_name": "리바로",
    "source": "ubist",
    "measure": "sales",
    "unit_label": "KRW",
    "metric_history": '{"2026-05":{"raw_value":8038598793.61,"ms":3.76,"rank":6}}',
    "channel_data": "{}",
    "specialty_data": "{}",
    "dimension_data": "{}",
    "by_dimension": "{}",
}
_IQVIA_ROW = {**_UBIST_ROW, "source": "iqvia_nsa", "metric_history": '{"2026-Q2":{"raw_value":1.0}}'}


def _snapshot() -> MartSnapshot:
    rows = (_UBIST_ROW, _IQVIA_ROW)
    return MartSnapshot(tuple(MartRecord.from_row(dict(row)) for row in rows), 0.0)


# (a) and (b): the two stored labels keep their existing behaviour exactly.


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ubist", "ubist"),
        ("iqvia_nsa", "iqvia_nsa"),
        ("iqvia", "iqvia_nsa"),
    ],
)
def test_known_labels_are_unchanged(value: str, expected: str) -> None:
    assert _source_key(value) == expected


# (d) and (e): normalisation is kept for case and strengthened for whitespace.


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("UBIST", "ubist"),
        ("IQVIA_NSA", "iqvia_nsa"),
        ("IQVIA", "iqvia_nsa"),
        (" ubist ", "ubist"),
        ("\tubist\n", "ubist"),
    ],
)
def test_case_and_whitespace_are_normalised(value: str, expected: str) -> None:
    assert _source_key(value) == expected


def test_padded_iqvia_no_longer_folds_onto_ubist() -> None:
    """The worst pre-fix case: UBIST rows served under an IQVIA heading.

    _source_key had no strip(), so any padded IQVIA label missed the set and fell through
    to ubist. source_label uses startswith("iqvia"), which trailing padding does not
    disturb - so a TRAILING-only pad produced ubist rows still tagged IQVIA. Leading
    padding broke startswith too, giving the merely-folded UBIST/UBIST pair. Stripping in
    _source_key removes the disagreement in both directions.
    """
    assert source_label("iqvia_nsa ") == "IQVIA"  # the label was never the problem here
    assert source_label(" iqvia_nsa") == "UBIST"  # leading pad also defeated the label
    assert _source_key("iqvia_nsa ") == "iqvia_nsa"
    assert _source_key(" iqvia ") == "iqvia_nsa"
    assert _source_key("  iqvia_nsa  ") == "iqvia_nsa"


# (c), (f), (g), (h): unknown, empty and None must not pass silently.


@pytest.mark.parametrize("value", ["nsa", "ubsit", "IMS", "nhis", "iqvia-nsa", "ubist,iqvia"])
def test_unknown_labels_raise_instead_of_folding(value: str) -> None:
    with pytest.raises(ValueError, match="source not found"):
        _source_key(value)


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_and_none_raise_instead_of_defaulting(value: str | None) -> None:
    with pytest.raises(ValueError, match="source not found"):
        _source_key(value)  # type: ignore[arg-type]


def test_rejection_message_maps_to_the_typed_source_absent_reason() -> None:
    """The wording is load-bearing: _query_failure_reason keys off it.

    A message that stopped containing "source not found" would silently downgrade the
    published reason_code to "unknown", so the mapping is pinned here rather than left to
    the caller's test.
    """
    from jw_chat_agent_poc.orchestrator.agent import (
        QueryFailureReason,
        _query_failure_reason,
    )

    with pytest.raises(ValueError) as excinfo:
        _source_key("nsa")
    assert _query_failure_reason(excinfo.value) is QueryFailureReason.SOURCE_ABSENT


def test_unknown_source_no_longer_returns_ubist_rows() -> None:
    """The fold's real damage was at the record filter, not the string."""
    snapshot = _snapshot()
    assert [record.source for record in snapshot.market_records("ml_006", "ubist", "sales")] == ["ubist"]
    assert [record.source for record in snapshot.market_records("ml_006", "iqvia_nsa", "sales")] == ["iqvia_nsa"]
    with pytest.raises(ValueError, match="source not found"):
        snapshot.market_records("ml_006", "nsa", "sales")


def test_known_sources_still_select_their_own_rows_only() -> None:
    """Guards the §7(4) requirement that the two sources' output is untouched."""
    snapshot = _snapshot()
    ubist = snapshot.market_records("ml_006", "ubist", "sales")
    iqvia = snapshot.market_records("ml_006", "iqvia_nsa", "sales")
    assert len(ubist) == 1
    assert len(iqvia) == 1
    assert ubist[0].source != iqvia[0].source
