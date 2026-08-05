from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
import requests

from jw_chat_agent_poc.service.runtime_provenance import trace_envelope
from jw_chat_agent_poc.tool_use.reimbursement_evidence import reimbursement_envelope
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    AbsentReimbursementStore,
    CacheLookupStatus,
    CacheStatus,
    HiraReimbursementHttpClient,
    MariaDbReimbursementStore,
    ReimbursementCacheResult,
    ReimbursementCriterion,
    ReimbursementLookupService,
    ReimbursementStoreError,
    configured_reimbursement_store,
)

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class _Store:
    def __init__(self, result: ReimbursementCacheResult) -> None:
        self.result = result
        self.writes: list[ReimbursementCriterion] = []

    def get_reimbursement_criteria(self, brand_name: str) -> ReimbursementCacheResult:
        assert brand_name == "아일리아"
        return self.result

    def put_reimbursement_criteria(self, criterion: ReimbursementCriterion) -> bool:
        self.writes.append(criterion)
        return True


class _WriteFailingStore(_Store):
    def put_reimbursement_criteria(self, criterion: ReimbursementCriterion) -> bool:
        raise ReimbursementStoreError(
            f"crawler storage unavailable for {criterion.brand_name}"
        )


class _Realtime:
    def __init__(
        self,
        criterion: ReimbursementCriterion | None = None,
        error: Exception | None = None,
    ) -> None:
        self.criterion = criterion
        self.error = error
        self.calls: list[str] = []

    def fetch(self, brand_name: str) -> ReimbursementCriterion | None:
        self.calls.append(brand_name)
        if self.error is not None:
            raise self.error
        return self.criterion


def _criterion(*, collected_at: datetime = NOW) -> ReimbursementCriterion:
    return ReimbursementCriterion(
        brand_name="아일리아",
        title="항혈관내피성장인자 주사제 급여기준",
        raw_text="신생혈관성 연령관련 황반변성에서 투여 간격 기준을 적용한다.",
        source_date="2026-06-24",
        collected_at=collected_at,
        notice_number="보건복지부 고시 제2026-101호",
        source_url="https://www.hira.or.kr/rc/example.do",
    )


def test_fresh_cache_is_used_without_realtime_lookup() -> None:
    cached = _criterion(collected_at=NOW - timedelta(hours=12))
    store = _Store(ReimbursementCacheResult(CacheStatus.FRESH, cached, cached.source_date))
    realtime = _Realtime(error=AssertionError("fresh cache must not call HIRA"))

    result = ReimbursementLookupService(
        store=store,
        realtime=realtime,
        now=lambda: NOW,
    ).lookup("아일리아")

    assert result.ok is True
    assert result.cache_status is CacheStatus.FRESH
    assert result.retrieval == "cache"
    assert result.data == cached
    assert realtime.calls == []
    assert store.writes == []


def test_stale_cache_is_returned_and_refresh_is_triggered() -> None:
    cached = _criterion(collected_at=NOW - timedelta(days=3))
    store = _Store(ReimbursementCacheResult(CacheStatus.STALE, cached, cached.source_date))
    realtime = _Realtime(error=AssertionError("stale response must not block on HIRA"))
    refreshes: list[str] = []

    result = ReimbursementLookupService(
        store=store,
        realtime=realtime,
        refresh_trigger=refreshes.append,
        now=lambda: NOW,
    ).lookup("아일리아")

    assert result.ok is True
    assert result.cache_status is CacheStatus.STALE
    assert result.retrieval == "stale_cache"
    assert refreshes == ["아일리아"]
    assert realtime.calls == []


def test_not_found_uses_realtime_and_persists_when_store_supports_it() -> None:
    live = _criterion()
    store = _Store(ReimbursementCacheResult(CacheStatus.NOT_FOUND, None, None))
    realtime = _Realtime(live)

    result = ReimbursementLookupService(
        store=store,
        realtime=realtime,
        now=lambda: NOW,
    ).lookup("아일리아")

    assert result.ok is True
    assert result.cache_status is CacheStatus.NOT_FOUND
    assert result.retrieval == "realtime"
    assert result.data == live
    assert realtime.calls == ["아일리아"]
    assert store.writes == [live]


def test_absent_store_skips_realtime_lookup() -> None:
    realtime = _Realtime(error=AssertionError("absent store must not call HIRA"))

    result = ReimbursementLookupService(
        store=AbsentReimbursementStore(),
        realtime=realtime,
        now=lambda: NOW,
    ).lookup("아일리아")

    assert result.ok is False
    assert result.retrieval == "typed_unavailable"
    assert result.error_code == "NO_EVIDENCE"
    assert realtime.calls == []


def test_cache_write_failure_does_not_discard_verified_realtime_result() -> None:
    live = _criterion()
    store = _WriteFailingStore(ReimbursementCacheResult(CacheStatus.NOT_FOUND, None, None))

    result = ReimbursementLookupService(
        store=store,
        realtime=_Realtime(live),
        now=lambda: NOW,
    ).lookup("아일리아")

    assert result.ok is True
    assert result.data == live
    assert result.cache_write == "failed"


def test_cache_read_failure_falls_back_to_realtime() -> None:
    live = _criterion()
    store = _Store(ReimbursementCacheResult(CacheStatus.NOT_FOUND, None, None))

    def failed_read(_brand_name: str) -> ReimbursementCacheResult:
        raise ReimbursementStoreError("crawler store unavailable")

    store.get_reimbursement_criteria = failed_read  # type: ignore[method-assign]
    result = ReimbursementLookupService(
        store=store,
        realtime=_Realtime(live),
        now=lambda: NOW,
    ).lookup("아일리아")

    assert result.ok is True
    assert result.retrieval == "realtime"
    assert result.data == live


def test_realtime_timeout_returns_typed_unavailable() -> None:
    result = ReimbursementLookupService(
        store=_Store(
            ReimbursementCacheResult(
                CacheStatus.NOT_FOUND,
                None,
                None,
                lookup_status=CacheLookupStatus.ZERO_ROWS,
                schema_name="reimbursement_stage",
            )
        ),
        realtime=_Realtime(error=requests.Timeout("deadline")),
        now=lambda: NOW,
    ).lookup("아일리아")

    assert result.ok is False
    assert result.data is None
    assert result.error_code == "TOOL_TIMEOUT"
    assert result.retrieval == "typed_unavailable"
    assert result.cache_lookup_status is CacheLookupStatus.ZERO_ROWS


def test_absent_store_returns_immediate_typed_notice_without_realtime() -> None:
    realtime = _Realtime(error=requests.Timeout("deadline"))

    result = ReimbursementLookupService(
        store=AbsentReimbursementStore(),
        realtime=realtime,
        now=lambda: NOW,
    ).lookup("로수젯")
    envelope = reimbursement_envelope(result, subject="로수젯")

    assert result.ok is False
    assert result.retrieval == "typed_unavailable"
    assert result.error_code == "NO_EVIDENCE"
    assert result.cache_lookup_status is CacheLookupStatus.STORE_ABSENT
    assert realtime.calls == []
    assert envelope.preview == (
        "급여기준 조회 기능은 현재 준비 중입니다. "
        "심사평가원(HIRA) 사이트에서 직접 확인해 주세요."
    )
    assert envelope.raw["cache_lookup_status"] == "store_absent"

    trace = trace_envelope(
        question="로수젯 급여기준 알려줘",
        result={
            "tool_calls": [
                {
                    "tool": "hira_reimbursement_criteria",
                    "status": "error",
                    "render_data": envelope.model_dump(mode="json"),
                }
            ],
            "markdown_response": {"fact_md": "", "data_md": ""},
        },
        answer=envelope.preview,
        charts=(),
        timing={"stages": []},
        conversation_id="f70-store-absent",
    )

    assert trace["qa_trace"]["final"] == {
        "disposition": "unavailable",
        "body_empty": False,
        "failure_kind": "tool_error",
    }


def test_future_cache_timestamp_is_not_treated_as_fresh() -> None:
    cached = _criterion(collected_at=NOW + timedelta(days=30))
    refreshes: list[str] = []

    result = ReimbursementLookupService(
        store=_Store(ReimbursementCacheResult(CacheStatus.FRESH, cached, cached.source_date)),
        realtime=_Realtime(error=AssertionError("stale cache must be returned")),
        refresh_trigger=refreshes.append,
        now=lambda: NOW,
    ).lookup("아일리아")

    assert result.ok is True
    assert result.cache_status is CacheStatus.STALE
    assert refreshes == ["아일리아"]


def test_cache_entry_without_raw_text_falls_back_to_realtime() -> None:
    empty = ReimbursementCriterion(
        brand_name="아일리아",
        title="파싱 불완전 행",
        raw_text=" \n\t ",
        source_date="2026-06-24",
        collected_at=NOW,
        notice_number=None,
        source_url="https://www.hira.or.kr/rc/example.do",
    )
    live = _criterion()
    realtime = _Realtime(live)

    result = ReimbursementLookupService(
        store=_Store(ReimbursementCacheResult(CacheStatus.FRESH, empty, empty.source_date)),
        realtime=realtime,
        now=lambda: NOW,
    ).lookup("아일리아")

    assert result.ok is True
    assert result.retrieval == "realtime"
    assert result.data == live
    assert realtime.calls == ["아일리아"]


class _DbCursor:
    def __init__(self, rows: dict | None | list[dict | None]) -> None:
        self.rows = list(rows) if isinstance(rows, list) else [rows]
        self.sql = ""
        self.params = None
        self.executions: list[tuple[str, object]] = []

    def execute(self, sql: str, params=None) -> None:
        self.sql = sql
        self.params = params
        self.executions.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class _DbConnection:
    def __init__(self, rows: dict | None | list[dict | None]) -> None:
        self.cursor_instance = _DbCursor(rows)
        self.closed = False

    def cursor(self) -> _DbCursor:
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


def test_mariadb_store_reads_only_authoritative_raw_text() -> None:
    raw_text = "1. 대상\n  가. 신생혈관성 연령관련 황반변성\n2. 투여 기준\n  14회 이내"
    connection = _DbConnection(
        [
            {"brand_match": 1},
            {"notice_match": 1},
            {
            "brand_name": "아일리아",
            "title": "항혈관내피성장인자 주사제",
            "raw_text": raw_text,
            "notice_date": "2026-06-24",
            "collected_at": NOW.replace(hour=9, minute=30, tzinfo=None),
            "notice_no": "보건복지부 고시 제2026-101호",
            "source_url": "https://www.hira.or.kr/rc/example.do",
            },
        ]
    )

    result = MariaDbReimbursementStore(connect=lambda: connection).get_reimbursement_criteria(
        "아일리아"
    )

    assert result.data is not None
    assert result.data.raw_text == raw_text
    assert result.data.collected_at.tzinfo is UTC
    assert result.source_date == "2026-06-24"
    sql = "\n".join(statement for statement, _params in connection.cursor_instance.executions)
    assert "hira_benefit_notice_brand" in sql
    assert "hira_benefit_notice" in sql
    assert "raw_text" in sql
    assert "target_condition" not in sql
    assert "exclusion_rule" not in sql
    assert "dosage_limit" not in sql
    assert [params for _sql, params in connection.cursor_instance.executions] == [
        ("아일리아",),
        ("아일리아",),
        ("아일리아",),
    ]
    assert connection.closed is True


@pytest.mark.parametrize(
    ("rows", "expected"),
    (
        ([None], CacheLookupStatus.BRAND_UNMATCHED),
        ([{"brand_match": 1}, None], CacheLookupStatus.ZERO_ROWS),
        (
            [{"brand_match": 1}, {"notice_match": 1}, None],
            CacheLookupStatus.EMPTY_RAW_TEXT,
        ),
    ),
)
def test_mariadb_store_classifies_each_data_miss(
    rows: list[dict | None],
    expected: CacheLookupStatus,
) -> None:
    result = MariaDbReimbursementStore(
        connect=lambda: _DbConnection(rows),
        schema_name="reimbursement_stage",
    ).get_reimbursement_criteria("아일리아")

    assert result.lookup_status is expected
    assert result.schema_name == "reimbursement_stage"
    assert result.data is None


class _FailingDbCursor(_DbCursor):
    def __init__(self, error: Exception) -> None:
        super().__init__([])
        self.error = error

    def execute(self, sql: str, params=None) -> None:
        raise self.error


class _FailingDbConnection(_DbConnection):
    def __init__(self, error: Exception) -> None:
        self.cursor_instance = _FailingDbCursor(error)
        self.closed = False


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (RuntimeError("connection refused"), CacheLookupStatus.CONNECT_ERROR),
        (RuntimeError(1146, "table missing"), CacheLookupStatus.TABLE_MISSING),
    ),
)
def test_mariadb_store_classifies_read_errors(
    error: Exception,
    expected: CacheLookupStatus,
) -> None:
    with pytest.raises(ReimbursementStoreError) as caught:
        MariaDbReimbursementStore(
            connect=lambda: _FailingDbConnection(error),
            schema_name="reimbursement_stage",
        ).get_reimbursement_criteria("아일리아")

    assert caught.value.lookup_status is expected
    assert caught.value.schema_name == "reimbursement_stage"


def test_mariadb_store_is_read_only_for_realtime_fallback() -> None:
    store = MariaDbReimbursementStore(connect=lambda: _DbConnection(None))

    assert store.put_reimbursement_criteria(_criterion()) is False


def test_configured_store_requires_complete_reimbursement_credentials(monkeypatch) -> None:
    for name in (
        "CHAT_CACHE_DB_HOST",
        "CHAT_CACHE_DB_NAME",
        "CHAT_CACHE_DB_USER",
        "CHAT_CACHE_DB_PASSWORD",
        "CHAT_REIMBURSEMENT_DB_NAME",
    ):
        monkeypatch.delenv(name, raising=False)

    assert isinstance(configured_reimbursement_store(), AbsentReimbursementStore)

    monkeypatch.setenv("CHAT_CACHE_DB_HOST", "db.internal")
    monkeypatch.setenv("CHAT_CACHE_DB_NAME", "jw_mart")
    monkeypatch.setenv("CHAT_CACHE_DB_USER", "chat")
    monkeypatch.setenv("CHAT_CACHE_DB_PASSWORD", "masked-test-value")
    monkeypatch.setenv("CHAT_REIMBURSEMENT_DB_NAME", "reimbursement_stage")

    assert isinstance(configured_reimbursement_store(), MariaDbReimbursementStore)


def test_reimbursement_evidence_exposes_raw_text_without_reconstruction() -> None:
    criterion = _criterion()
    result = ReimbursementLookupService(
        store=_Store(
            ReimbursementCacheResult(CacheStatus.FRESH, criterion, criterion.source_date)
        ),
        realtime=_Realtime(error=AssertionError("fresh cache must not call HIRA")),
        now=lambda: NOW,
    ).lookup("아일리아")

    envelope = reimbursement_envelope(result, subject="아일리아")

    assert envelope.ok is True
    assert len(envelope.evidence) == 1
    fact = envelope.evidence[0]
    assert fact.metric == "HIRA 보험인정기준 원문 (AI 요약·해석·재구성 없음)"
    assert fact.source_locator == criterion.raw_text
    assert "target_condition" not in envelope.raw
    assert "exclusion_rule" not in envelope.raw
    assert "dosage_limit" not in envelope.raw


@pytest.mark.parametrize(
    ("lookup_status", "criterion"),
    (
        (CacheLookupStatus.ZERO_ROWS, None),
        (
            CacheLookupStatus.EMPTY_RAW_TEXT,
            ReimbursementCriterion(
                brand_name="아일리아",
                title="원문 없음",
                raw_text=" ",
                source_date=None,
                collected_at=NOW,
                notice_number=None,
                source_url="https://www.hira.or.kr/rc/example.do",
            ),
        ),
    ),
)
def test_recoverable_cache_miss_reaches_trace_and_keeps_realtime_fallback(
    lookup_status: CacheLookupStatus,
    criterion: ReimbursementCriterion | None,
) -> None:
    live = _criterion()
    result = ReimbursementLookupService(
        store=_Store(
            ReimbursementCacheResult(
                CacheStatus.NOT_FOUND,
                criterion,
                None,
                lookup_status=lookup_status,
                schema_name="reimbursement_stage",
            )
        ),
        realtime=_Realtime(live),
        now=lambda: NOW,
    ).lookup("아일리아")

    envelope = reimbursement_envelope(result, subject="아일리아")

    assert result.retrieval == "realtime"
    assert result.data == live
    assert result.cache_lookup_status is lookup_status
    assert result.cache_schema == "reimbursement_stage"
    assert envelope.raw["cache_lookup_status"] == lookup_status.value
    assert envelope.raw["cache_schema"] == "reimbursement_stage"


def test_brand_unmatched_returns_immediate_typed_notice_without_realtime() -> None:
    live = _criterion()
    realtime = _Realtime(live)
    result = ReimbursementLookupService(
        store=_Store(
            ReimbursementCacheResult(
                CacheStatus.NOT_FOUND,
                None,
                None,
                lookup_status=CacheLookupStatus.BRAND_UNMATCHED,
                schema_name="reimbursement_stage",
            )
        ),
        realtime=realtime,
        now=lambda: NOW,
    ).lookup("아일리아")

    envelope = reimbursement_envelope(result, subject="아일리아")

    assert result.ok is True
    assert result.retrieval == "realtime"
    assert result.error_code is None
    assert result.cache_lookup_status is CacheLookupStatus.BRAND_UNMATCHED
    assert realtime.calls == ["아일리아"]
    assert envelope.preview == "아일리아 HIRA 보험인정기준 원문 확인 (AI 요약·해석·재구성 없음)"
    assert envelope.raw["cache_lookup_status"] == "brand_unmatched"


def test_brand_unmatched_notice_remains_unavailable_in_runtime_trace() -> None:
    result = ReimbursementLookupService(
        store=_Store(
            ReimbursementCacheResult(
                CacheStatus.NOT_FOUND,
                None,
                None,
                lookup_status=CacheLookupStatus.BRAND_UNMATCHED,
                schema_name="reimbursement_stage",
            )
        ),
        realtime=_Realtime(error=AssertionError("policy-disabled lookup must not call HIRA")),
        realtime_allowed=lambda: False,
        now=lambda: NOW,
    ).lookup("아일리아")
    envelope = reimbursement_envelope(result, subject="아일리아")
    tool_call = {
        "tool": "hira_reimbursement_criteria",
        "status": "error",
        "render_data": envelope.model_dump(mode="json"),
    }

    trace = trace_envelope(
        question="아일리아 급여기준 알려줘",
        result={
            "tool_calls": [tool_call],
            "markdown_response": {"fact_md": "", "data_md": ""},
        },
        answer=envelope.preview,
        charts=(),
        timing={"stages": []},
        conversation_id="f46-brand-unmatched",
    )

    assert trace["qa_trace"]["final"] == {
        "disposition": "unavailable",
        "body_empty": False,
        "failure_kind": "tool_error",
    }


def test_cache_lookup_status_contract_keeps_all_seven_values() -> None:
    assert tuple(status.value for status in CacheLookupStatus) == (
        "store_absent",
        "connect_error",
        "table_missing",
        "zero_rows",
        "brand_unmatched",
        "empty_raw_text",
        "hit",
    )


@pytest.mark.parametrize(
    "lookup_status",
    (CacheLookupStatus.CONNECT_ERROR, CacheLookupStatus.TABLE_MISSING),
)
def test_cache_read_error_taxonomy_reaches_trace_without_credentials(
    lookup_status: CacheLookupStatus,
) -> None:
    live = _criterion()
    store = _Store(ReimbursementCacheResult(CacheStatus.NOT_FOUND, None, None))

    def failed_read(_brand_name: str) -> ReimbursementCacheResult:
        raise ReimbursementStoreError(
            "masked cache failure",
            lookup_status=lookup_status,
            schema_name="reimbursement_stage",
        )

    store.get_reimbursement_criteria = failed_read  # type: ignore[method-assign]
    result = ReimbursementLookupService(
        store=store,
        realtime=_Realtime(live),
        now=lambda: NOW,
    ).lookup("아일리아")
    envelope = reimbursement_envelope(result, subject="아일리아")

    assert result.retrieval == "realtime"
    assert result.cache_lookup_status is lookup_status
    assert envelope.raw["cache_lookup_status"] == lookup_status.value
    assert envelope.raw["cache_schema"] == "reimbursement_stage"
    assert "password" not in str(envelope.raw).lower()


def test_absent_store_and_hit_have_distinct_cache_telemetry() -> None:
    live = _criterion()
    absent = ReimbursementLookupService(
        store=AbsentReimbursementStore(),
        realtime=_Realtime(error=AssertionError("absent store must not call HIRA")),
        now=lambda: NOW,
    ).lookup("아일리아")
    hit_realtime = _Realtime(error=AssertionError("cache hit must not call realtime"))
    hit = ReimbursementLookupService(
        store=_Store(
            ReimbursementCacheResult(
                CacheStatus.FRESH,
                live,
                live.source_date,
                lookup_status=CacheLookupStatus.HIT,
                schema_name="reimbursement_stage",
            )
        ),
        realtime=hit_realtime,
        now=lambda: NOW,
    ).lookup("아일리아")

    assert absent.cache_lookup_status is CacheLookupStatus.STORE_ABSENT
    assert absent.retrieval == "typed_unavailable"
    assert hit.cache_lookup_status is CacheLookupStatus.HIT
    assert hit.retrieval == "cache"
    assert hit_realtime.calls == []


def test_reimbursement_store_is_absent_without_dedicated_database_env(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CHAT_CACHE_DB_HOST", "db.internal")
    monkeypatch.setenv("CHAT_CACHE_DB_NAME", "jw_mart")
    monkeypatch.setenv("CHAT_CACHE_DB_USER", "chat")
    monkeypatch.setenv("CHAT_CACHE_DB_PASSWORD", "masked-test-value")
    monkeypatch.delenv("CHAT_REIMBURSEMENT_DB_NAME", raising=False)

    store = configured_reimbursement_store()
    realtime = _Realtime(error=AssertionError("absent store must not call HIRA"))
    result = ReimbursementLookupService(
        store=store,
        realtime=realtime,
        now=lambda: NOW,
    ).lookup("로수젯")
    envelope = reimbursement_envelope(result, subject="로수젯")

    assert isinstance(store, AbsentReimbursementStore)
    assert result.cache_lookup_status is CacheLookupStatus.STORE_ABSENT
    assert result.retrieval == "typed_unavailable"
    assert realtime.calls == []
    assert envelope.preview == (
        "급여기준 조회 기능은 현재 준비 중입니다. "
        "심사평가원(HIRA) 사이트에서 직접 확인해 주세요."
    )


def test_reimbursement_store_uses_dedicated_database_env(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_CACHE_DB_HOST", "db.internal")
    monkeypatch.setenv("CHAT_CACHE_DB_NAME", "jw_mart")
    monkeypatch.setenv("CHAT_CACHE_DB_USER", "chat")
    monkeypatch.setenv("CHAT_CACHE_DB_PASSWORD", "masked-test-value")

    monkeypatch.setenv("CHAT_REIMBURSEMENT_DB_NAME", "reimbursement_stage")
    dedicated = configured_reimbursement_store()

    assert isinstance(dedicated, MariaDbReimbursementStore)
    assert dedicated.schema_name == "reimbursement_stage"
    assert os.environ["CHAT_CACHE_DB_NAME"] == "jw_mart"


class _Response:
    def __init__(self, text: str, *, url: str, status_code: int = 200) -> None:
        self.text = text
        self.url = url
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def test_http_client_uses_bounded_hira_search_and_parses_detail() -> None:
    list_html = """
    <table><tr><td>1</td><td>
      <a href="/rc/insu/insuadtcrtr/InsuAdtCrtrView.do?seq=101">
        항혈관내피성장인자 주사제(아일리아) 급여기준
      </a>
    </td><td>보건복지부 고시 제2026-101호</td><td>2026-06-24</td></tr></table>
    """
    detail_html = """
    <div class="view_cont">
      <h3>항혈관내피성장인자 주사제 급여기준</h3>
      <p>신생혈관성 연령관련 황반변성에서 투여 간격 기준을 적용한다.</p>
    </div>
    """
    session = _Session(
        [
            _Response(
                list_html,
                url="https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrList.do",
            ),
            _Response(
                detail_html,
                url="https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrView.do?seq=101",
            ),
        ]
    )

    result = HiraReimbursementHttpClient(session=session, timeout_s=6.0).fetch("아일리아")

    assert result is not None
    assert result.brand_name == "아일리아"
    assert "투여 간격 기준" in result.raw_text
    assert result.source_date == "2026-06-24"
    assert result.notice_number == "보건복지부 고시 제2026-101호"
    assert len(session.calls) == 2
    search = session.calls[0]
    assert search["params"]["searchCondition"] == "TXTALL"
    assert search["params"]["searchWord"] == "아일리아"
    assert search["params"]["pageIndex"] == 1
    assert search["params"]["pageSize"] == 10
    assert sum(search["timeout"]) == pytest.approx(6.0)
    assert search["allow_redirects"] is False


def test_http_client_parses_observed_hira_popup_link_and_compound_product_name() -> None:
    list_html = """
    <table><tr><td>고시 제2024-235호(약제)</td><td>
      <a href="#none"
         onclick="viewInsuAdtCrtr(1, '20241201', '3', '0011', '1'); return false;"
         title="Aflibercept 주사제(품명: 아일리아주사, 아일리아프리필드시린지 등) 새창으로 열기">
        Aflibercept 주사제(품명: 아일리아주사, 아일리아프리필드시린지 등)
      </a>
    </td><td>2024-12-01</td></tr></table>
    """
    detail_html = """
    <div class="viewCont mt05"><p>아일리아의 공식 보험인정기준 본문입니다.</p></div>
    """
    session = _Session(
        [
            _Response(list_html, url="https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrList.do"),
            _Response(detail_html, url="https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrPopup.do?mtgHmeDd=20241201&sno=3&mtgMtrRegSno=0011"),
        ]
    )

    result = HiraReimbursementHttpClient(session=session, timeout_s=6.0).fetch("아일리아")

    assert result is not None
    assert result.notice_number == "고시 제2024-235호"
    assert session.calls[1]["url"] == (
        "https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrPopup.do"
        "?mtgHmeDd=20241201&sno=3&mtgMtrRegSno=0011"
    )


def test_http_client_applies_one_deadline_across_list_and_detail_requests() -> None:
    list_html = """
    <table><tr><td><a href="/detail">아일리아 급여기준</a></td></tr></table>
    """
    detail_html = """
    <div class="view_cont"><p>아일리아의 공식 보험인정기준 본문입니다.</p></div>
    """
    ticks = iter((100.0, 100.0, 105.5))
    session = _Session(
        [
            _Response(list_html, url="https://www.hira.or.kr/list"),
            _Response(detail_html, url="https://www.hira.or.kr/detail"),
        ]
    )
    client = HiraReimbursementHttpClient(
        session=session,
        timeout_s=6.0,
        monotonic_now=lambda: next(ticks),
    )

    assert client.fetch("아일리아") is not None
    assert sum(session.calls[0]["timeout"]) == pytest.approx(6.0)
    assert sum(session.calls[1]["timeout"]) == pytest.approx(0.5)


def test_http_client_rejects_detail_link_outside_official_hira_host() -> None:
    list_html = """
    <table><tr><td>
      <a href="https://example.invalid/collect?brand=eylea">
        항혈관내피성장인자 주사제(아일리아) 급여기준
      </a>
    </td><td>2026-06-24</td></tr></table>
    """
    session = _Session(
        [
            _Response(
                list_html,
                url="https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrList.do",
            )
        ]
    )

    result = HiraReimbursementHttpClient(session=session, timeout_s=6.0).fetch("아일리아")

    assert result is None
    assert len(session.calls) == 1


def test_http_client_rejects_detail_redirect_outside_official_hira_host() -> None:
    list_html = """
    <table><tr><td>
      <a href="/rc/insu/insuadtcrtr/InsuAdtCrtrView.do?seq=101">
        항혈관내피성장인자 주사제(아일리아) 급여기준
      </a>
    </td><td>2026-06-24</td></tr></table>
    """
    session = _Session(
        [
            _Response(
                list_html,
                url="https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrList.do",
            ),
            _Response(
                "",
                url="https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrView.do?seq=101",
                status_code=302,
            ),
        ]
    )

    result = HiraReimbursementHttpClient(session=session, timeout_s=6.0).fetch("아일리아")

    assert result is None
    assert len(session.calls) == 2


def test_http_client_ignores_similar_longer_brand_name() -> None:
    list_html = """
    <table>
      <tr><td><a href="/wrong">리바로젯 급여기준</a></td><td>2026-06-24</td></tr>
      <tr><td><a href="/right">리바로 급여기준</a></td><td>2026-06-24</td></tr>
    </table>
    """
    detail_html = """
    <div class="view_cont"><p>리바로의 공식 보험인정기준 본문입니다.</p></div>
    """
    session = _Session(
        [
            _Response(list_html, url="https://www.hira.or.kr/list"),
            _Response(detail_html, url="https://www.hira.or.kr/right"),
        ]
    )

    result = HiraReimbursementHttpClient(session=session).fetch("리바로")

    assert result is not None
    assert result.title == "리바로 급여기준"
    assert session.calls[1]["url"] == "https://www.hira.or.kr/right"


def test_http_client_uses_only_verified_detail_container() -> None:
    list_html = """
    <table><tr><td><a href="/detail">아일리아 급여기준</a></td><td>2026-06-24</td></tr></table>
    """
    detail_html = """
    <aside>다른 제품은 매월 14회 투여한다.</aside>
    <div class="view_cont"><p>아일리아는 확인된 투여기준을 적용한다.</p></div>
    """
    session = _Session(
        [
            _Response(list_html, url="https://www.hira.or.kr/list"),
            _Response(detail_html, url="https://www.hira.or.kr/detail"),
        ]
    )

    result = HiraReimbursementHttpClient(session=session).fetch("아일리아")

    assert result is not None
    assert result.raw_text == "아일리아는 확인된 투여기준을 적용한다."


def test_http_client_fails_closed_without_verified_detail_container() -> None:
    list_html = """
    <table><tr><td><a href="/detail">아일리아 급여기준</a></td></tr></table>
    """
    session = _Session(
        [
            _Response(list_html, url="https://www.hira.or.kr/list"),
            _Response(
                "<main><p>구조 미확인 텍스트</p></main>",
                url="https://www.hira.or.kr/detail",
            ),
        ]
    )

    assert HiraReimbursementHttpClient(session=session).fetch("아일리아") is None


@pytest.mark.parametrize("brand", ["", " ", "x" * 81])
def test_http_client_rejects_unbounded_or_blank_brand_without_network(brand: str) -> None:
    session = _Session([])
    client = HiraReimbursementHttpClient(session=session, timeout_s=6.0)

    with pytest.raises(ValueError):
        client.fetch(brand)

    assert session.calls == []


def test_reimbursement_question_routes_to_hira_criteria_capability() -> None:
    classification = classify_question("아일리아 급여기준 알려줘")

    assert classification.source_domain == "hira"
    assert classification.requested_capability == "HIRA_REIMBURSEMENT_CRITERIA"
    assert classification.input_key == "product_name"
