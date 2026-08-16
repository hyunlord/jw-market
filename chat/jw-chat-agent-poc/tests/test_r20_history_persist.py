"""R20 - a turn's bookkeeping must not lose the turn, and must not lie about it.

Three defects are pinned here, each measured before it was fixed:

* a trace grew to 13.6 MB (``claim_ir_shadow`` alone 12.1 MB of it) and the
  write carrying it took 11.5-50.9 s server-side against a 5 s client budget;
* the connection did not autocommit, so a client timeout discarded a row the
  server had already finished -- ``Rows_affected: 1`` in the slow log, and no
  row in the table;
* the failure reached the log and stopped there, so a user whose turn was never
  recorded was told nothing.
"""
from __future__ import annotations

import json
import zlib

import pymysql
import pytest

from jw_chat_agent_poc.service import trace_codec
from jw_chat_agent_poc.service.conversation_history import (
    DEFAULT_READ_TIMEOUT_SECONDS,
    PERSIST_STATUS_FAILED,
    PERSIST_STATUS_PERSISTED,
    PERSIST_STATUS_UNCONFIRMED,
    READ_TIMEOUT_ENV,
    MySQLConversationHistoryStore,
    TurnPersistOutcome,
    _DbConfig,
    _json_object,
    read_timeout_seconds,
)
from jw_chat_agent_poc.service.trace_codec import (
    COMPRESS_ENABLED_ENV,
    COMPRESS_THRESHOLD_ENV,
    MAGIC,
    TraceDecodeError,
    decode_trace,
    encode_trace,
)


def _big_trace(rows: int = 3000) -> dict:
    """Shaped like the live payload: one dominant shadow key, and Korean text."""
    return {
        "v4": True,
        "claim_ir_shadow": [
            {"statement": f"리바로 {index}월 매출은 179.33억원입니다", "evidence": list(range(40))}
            for index in range(rows)
        ],
        "tool_results": {"mart": [{"brand": "리바로", "value": 17933000000}]},
    }


def _config() -> _DbConfig:
    return _DbConfig("db", 3306, "jw_mart", "user", "password")


class _Cursor:
    def __init__(self, *, fetch: object = (1,)) -> None:
        self.statements: list[tuple[str, tuple | None]] = []
        self.lastrowid = 0
        self._fetch = fetch

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.statements.append((sql, params))
        if "INSERT INTO" in sql:
            self.lastrowid = 41

    def fetchone(self):
        return self._fetch


class _Connection:
    def __init__(self, cursor: _Cursor | None = None) -> None:
        self.cursor_instance = cursor or _Cursor()
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1


# --------------------------------------------------------------------------
# the codec: nothing is lost, and an old row still reads
# --------------------------------------------------------------------------


def test_a_large_trace_survives_the_round_trip_unchanged():
    """F4 in one assertion: what goes in is what comes out."""
    payload = _big_trace()
    stored = encode_trace(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    assert decode_trace(stored) == payload


def test_a_compressed_row_is_still_valid_json_for_the_column_check():
    """The column is ``longtext … CHECK (json_valid(trace_json))``.

    A JSON string scalar satisfies it -- verified against the live server, where
    ``json_valid('"jwtz1:…"')`` returned 1 -- so the compressed form needs no
    schema change. Here we can only hold up our end: what we hand the driver
    must parse as JSON, and must be the string form rather than an object.
    """
    stored = encode_trace(json.dumps(_big_trace(), ensure_ascii=False, separators=(",", ":")))

    parsed = json.loads(stored)
    assert isinstance(parsed, str)
    assert parsed.startswith(MAGIC)


def test_the_trace_gets_materially_smaller():
    raw = json.dumps(_big_trace(), ensure_ascii=False, separators=(",", ":"))
    stored = encode_trace(raw)

    assert len(stored.encode()) * 4 < len(raw.encode()), "compression must be worth doing"


def test_a_row_written_before_this_codec_reads_back_the_same_way():
    """No migration: the magic prefix is what tells the two apart."""
    legacy = json.dumps({"tools_called": ["get_metric"], "_conversation_slots": {"anchor_brand": "리바로"}})

    assert decode_trace(legacy) == {
        "tools_called": ["get_metric"],
        "_conversation_slots": {"anchor_brand": "리바로"},
    }
    assert _json_object(legacy)["_conversation_slots"] == {"anchor_brand": "리바로"}


def test_a_small_trace_is_left_plain_so_sql_can_still_read_it(monkeypatch):
    monkeypatch.delenv(COMPRESS_THRESHOLD_ENV, raising=False)
    raw = json.dumps({"tools_called": []}, separators=(",", ":"))

    assert encode_trace(raw) == raw


def test_the_conversation_context_read_path_decodes_a_compressed_row():
    """``_json_object`` feeds slot restoration; a compressed row must not read
    as an empty trace, which is how a silent context loss would look."""
    payload = {"_conversation_slots": {"anchor_brand": "리바로", "market": "ml_006"}, "bulk": "x" * 400_000}
    stored = encode_trace(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    assert stored.startswith('"' + MAGIC)
    assert _json_object(stored)["_conversation_slots"] == {"anchor_brand": "리바로", "market": "ml_006"}


def test_a_damaged_compressed_trace_is_reported_not_silently_emptied():
    """Clause 2: a trace that announces itself compressed and will not decode is
    a lost trace. Returning {} would present it as a turn that simply had none."""
    with pytest.raises(TraceDecodeError):
        decode_trace(json.dumps(MAGIC + "not-base64-at-all!!"))

    with pytest.raises(TraceDecodeError):
        decode_trace(json.dumps(MAGIC + trace_codec.base64.b64encode(b"not zlib").decode()))


# --------------------------------------------------------------------------
# F1 - turn compression off and the oversized write comes back
# --------------------------------------------------------------------------


def test_f1_compression_can_be_switched_off_and_the_payload_returns(monkeypatch):
    raw = json.dumps(_big_trace(), ensure_ascii=False, separators=(",", ":"))

    monkeypatch.setenv(COMPRESS_ENABLED_ENV, "false")
    disabled = encode_trace(raw)
    assert disabled == raw, "with the fix off, the full payload is what would be written"

    monkeypatch.setenv(COMPRESS_ENABLED_ENV, "true")
    enabled = encode_trace(raw)
    assert len(enabled) < len(disabled), "with the fix on, the write shrinks"
    assert decode_trace(enabled) == json.loads(disabled), "and both carry the same trace"


# --------------------------------------------------------------------------
# F2 - an extreme trace fails with a reason attached, never silently
# --------------------------------------------------------------------------


def _store_with_timeout(monkeypatch, *, verify_finds_row: bool | None) -> MySQLConversationHistoryStore:
    store = MySQLConversationHistoryStore(_config())
    calls: list[str] = []

    def _connect(*, read_timeout: int | None = None):
        if not calls:
            calls.append("write")
            raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query (timed out)")
        calls.append("verify")
        if verify_finds_row is None:
            raise pymysql.err.OperationalError(2003, "Can't connect")
        return _Connection(_Cursor(fetch=(99,) if verify_finds_row else None))

    monkeypatch.setattr(store, "_connect", _connect)
    return store


def _record(store: MySQLConversationHistoryStore, trace: dict) -> TurnPersistOutcome:
    return store.record_turn(
        session_id=None,
        conversation_id="conversation-r20",
        question_text="리바로 매출 알려줘",
        answer_text="리바로 매출은 179.33억원입니다.",
        trace=trace,
        timing={"total_elapsed_ms": 91430},
        sources=("UBIST",),
        projection_context=None,
    )


def test_f2_an_extreme_trace_that_times_out_is_reported_with_a_reason(monkeypatch):
    store = _store_with_timeout(monkeypatch, verify_finds_row=False)

    outcome = _record(store, _big_trace(rows=20000))

    assert outcome.status == PERSIST_STATUS_UNCONFIRMED
    assert outcome.reason == "write_timeout"
    assert outcome.recorded is False


def test_f2b_a_write_the_server_finished_after_the_timeout_is_not_called_a_loss(monkeypatch):
    """The autocommit half of the fix, seen from the caller: the client gave up,
    the row is there anyway, and the user is told nothing because nothing is wrong."""
    store = _store_with_timeout(monkeypatch, verify_finds_row=True)

    outcome = _record(store, _big_trace())

    assert outcome.status == PERSIST_STATUS_PERSISTED
    assert outcome.recorded is True


def test_f2c_an_unverifiable_timeout_says_so_rather_than_guessing(monkeypatch):
    store = _store_with_timeout(monkeypatch, verify_finds_row=None)

    outcome = _record(store, _big_trace())

    assert outcome.status == PERSIST_STATUS_UNCONFIRMED
    assert outcome.reason == "write_timeout_unverifiable"


# --------------------------------------------------------------------------
# the commit fix itself
# --------------------------------------------------------------------------


def test_the_connection_autocommits_so_finished_server_work_is_kept(monkeypatch):
    """Before: ``autocommit=False`` plus a 5 s client budget meant the server
    could log ``Rows_affected: 1`` after 50.9 s and the row still not exist."""
    captured: dict = {}

    def _fake_connect(**kwargs):
        captured.update(kwargs)
        return _Connection()

    monkeypatch.setattr(pymysql, "connect", _fake_connect)
    store = MySQLConversationHistoryStore(_config())
    store._connect()

    assert captured["autocommit"] is True


def test_the_write_budget_is_configurable_and_defends_its_default(monkeypatch):
    monkeypatch.delenv(READ_TIMEOUT_ENV, raising=False)
    assert read_timeout_seconds() == DEFAULT_READ_TIMEOUT_SECONDS

    monkeypatch.setenv(READ_TIMEOUT_ENV, "20")
    assert read_timeout_seconds() == 20

    for unusable in ("", "   ", "abc", "0", "-5"):
        monkeypatch.setenv(READ_TIMEOUT_ENV, unusable)
        assert read_timeout_seconds() == DEFAULT_READ_TIMEOUT_SECONDS


def test_the_verification_read_is_deterministic_and_bounded(monkeypatch):
    """Clause 3: no ORDER BY-less LIMIT 1, and it must not become a second wait."""
    store = MySQLConversationHistoryStore(_config())
    cursor = _Cursor(fetch=(7,))
    seen: dict = {}

    def _connect(*, read_timeout: int | None = None):
        seen["read_timeout"] = read_timeout
        return _Connection(cursor)

    monkeypatch.setattr(store, "_connect", _connect)

    assert store._turn_was_written("conversation-r20", None) is True
    statement, params = cursor.statements[-1]
    assert "ORDER BY id DESC" in statement
    assert "LIMIT 1" in statement
    assert params[0] == "conversation-r20"
    assert seen["read_timeout"] is not None and seen["read_timeout"] < DEFAULT_READ_TIMEOUT_SECONDS


def test_a_turn_stores_the_encoded_trace_and_returns_persisted(monkeypatch):
    store = MySQLConversationHistoryStore(_config())
    cursor = _Cursor()
    monkeypatch.setattr(store, "_connect", lambda **_kwargs: _Connection(cursor))

    outcome = _record(store, _big_trace())

    assert outcome.status == PERSIST_STATUS_PERSISTED
    insert = [entry for entry in cursor.statements if "INSERT INTO" in entry[0]][-1]
    stored_trace = insert[1][-1]
    assert json.loads(stored_trace).startswith(MAGIC)
    restored = decode_trace(stored_trace)
    assert restored["claim_ir_shadow"] == _big_trace()["claim_ir_shadow"]
    assert "_conversation_slots" in restored, "slots are added on write and must survive"


# --------------------------------------------------------------------------
# F3 - the user is told
# --------------------------------------------------------------------------


def _final_answer(text: str = "리바로 매출은 179.33억원입니다."):
    from jw_chat_agent_poc.service.app import FinalAnswer

    return FinalAnswer(
        text=text,
        charts=[],
        timing={},
        trace={"v4": True},
        sources=("UBIST",),
        conversation_id="conversation-r20",
    )


def test_f3_a_turn_that_was_not_recorded_says_so_in_the_answer():
    from jw_chat_agent_poc.service.app import _answer_with_history_notice

    amended = _answer_with_history_notice(
        _final_answer(), TurnPersistOutcome(status=PERSIST_STATUS_UNCONFIRMED, reason="write_timeout")
    )

    assert "리바로 매출은 179.33억원입니다." in amended.text, "the answer itself is not lost"
    assert "기록 저장을 확인하지 못했습니다" in amended.text
    assert amended.trace["history_persist"]["status"] == PERSIST_STATUS_UNCONFIRMED


def test_f3b_a_recorded_turn_says_nothing_extra():
    from jw_chat_agent_poc.service.app import _answer_with_history_notice

    amended = _answer_with_history_notice(_final_answer(), TurnPersistOutcome())

    assert amended.text == "리바로 매출은 179.33억원입니다."
    assert amended.trace["history_persist"]["status"] == PERSIST_STATUS_PERSISTED


def test_f3c_the_notice_is_deterministic_and_leaks_no_internal_code():
    from jw_chat_agent_poc.service.app import _answer_with_history_notice

    amended = _answer_with_history_notice(
        _final_answer(),
        TurnPersistOutcome(status=PERSIST_STATUS_FAILED, reason="error", detail="OperationalError"),
    )

    assert "기록에 저장되지 않았습니다" in amended.text
    for leak in ("pymysql", "OperationalError", "2013", "Traceback"):
        assert leak not in amended.text
    assert amended.trace["history_persist"]["detail"] == "OperationalError", "the detail stays in the trace"


def test_f3d_a_store_that_raises_still_produces_an_answer_and_a_notice():
    """Invariant 3: bookkeeping may fail; the answer may not be withheld."""
    from jw_chat_agent_poc.service.app import _answer_with_history_notice, _record_conversation_history

    class _Exploding:
        def record_turn(self, **_kwargs):
            raise RuntimeError("db is gone")

    outcome = _record_conversation_history(
        _Exploding(), session_id=None, question="리바로 매출 알려줘", final_answer=_final_answer()
    )
    amended = _answer_with_history_notice(_final_answer(), outcome)

    assert outcome.status == PERSIST_STATUS_FAILED
    assert "리바로 매출은 179.33억원입니다." in amended.text
    assert "기록에 저장되지 않았습니다" in amended.text


def test_a_store_predating_the_outcome_contract_is_treated_as_success():
    from jw_chat_agent_poc.service.app import _record_conversation_history

    class _Legacy:
        def record_turn(self, **_kwargs):
            return None

    assert _record_conversation_history(
        _Legacy(), session_id=None, question="q", final_answer=_final_answer()
    ).recorded is True


def test_the_sse_stream_carries_the_failure_as_its_own_event():
    from jw_chat_agent_poc.service.app import _answer_with_history_notice, _sse_events_from_final_answer

    amended = _answer_with_history_notice(
        _final_answer(), TurnPersistOutcome(status=PERSIST_STATUS_UNCONFIRMED, reason="write_timeout")
    )
    events = "".join(_sse_events_from_final_answer(amended))
    assert "event: history_persist" in events

    quiet = "".join(_sse_events_from_final_answer(_answer_with_history_notice(_final_answer(), TurnPersistOutcome())))
    assert "event: history_persist" not in quiet


def test_zlib_is_what_the_server_side_measurement_used():
    """The 10.5-27.4x ratios in the round's evidence came from MariaDB
    ``COMPRESS()``, which is zlib. Keeping the same algorithm is what makes
    those numbers a prediction rather than an analogy."""
    payload = json.dumps(_big_trace(), ensure_ascii=False, separators=(",", ":")).encode()
    stored = encode_trace(payload.decode())
    packed = trace_codec.base64.b64decode(json.loads(stored)[len(MAGIC) :])

    assert zlib.decompress(packed) == payload
