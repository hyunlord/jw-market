from __future__ import annotations

from dataclasses import replace
from datetime import date
import hashlib
import json
import re
import sqlite3

import pytest

from pipeline.etl.io.mart.agent2_eligibility import (
    AGENT2_ALLOWED_DERIVATIONS,
    AGENT2_ALLOWED_PROCESSORS,
    AGENT2_ELIGIBILITY_REVISION,
    Agent2ScoreRow,
    OrphanNewsError,
    agent2_eligibility_revision_payload,
    agent2_eligibility_sql_predicate,
    eligible_agent2_events,
    eligible_agent2_news_ids,
    is_agent2_eligible,
)
from pipeline.etl.io.mart.event_score_policy import event_score_policy


def _score_row(
    *,
    news_id: str = "news-1",
    source_processor: str = "workflow_196_optionB",
    derivation: str = "llm_direct",
    tag: str = "자본/경영",
    score: int = 100,
    news_exists: bool = True,
) -> Agent2ScoreRow:
    return Agent2ScoreRow(
        news_id=news_id,
        source_processor=source_processor,
        derivation=derivation,
        tag=tag,
        score=score,
        published_date=date(2026, 7, 1),
        news_exists=news_exists,
    )


def _sql_accepts(row: Agent2ScoreRow) -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE scores (news_id TEXT, source_processor TEXT, "
            "derivation TEXT, tag TEXT, score REAL)"
        )
        connection.execute("CREATE TABLE news (news_id TEXT)")
        connection.execute(
            "INSERT INTO scores VALUES (?, ?, ?, ?, ?)",
            (row.news_id, row.source_processor, row.derivation, row.tag, float(row.score)),
        )
        if row.news_exists:
            connection.execute("INSERT INTO news VALUES (?)", (row.news_id,))
        predicate, params = agent2_eligibility_sql_predicate("s", "n")
        sqlite_predicate = predicate.replace("%s", "?")
        result = connection.execute(
            "SELECT COUNT(*) FROM scores s "
            "LEFT JOIN news n ON s.news_id = n.news_id "
            f"WHERE {sqlite_predicate}",
            params,
        ).fetchone()
        return bool(result and result[0])
    finally:
        connection.close()


def test_python_and_sql_predicates_match_every_allowed_policy_boundary() -> None:
    checked = 0
    for processor in AGENT2_ALLOWED_PROCESSORS:
        policy = event_score_policy(processor)
        derivation = "cross_match" if processor == "cross_match_adapter_v1" else "llm_direct"
        for tag, cutoff in policy.category_cutoffs.items():
            for score in (cutoff - 1, cutoff, cutoff + 1):
                row = _score_row(
                    source_processor=processor,
                    derivation=derivation,
                    tag=tag,
                    score=score,
                )
                assert _sql_accepts(row) is is_agent2_eligible(row)
                checked += 1

    assert checked == len(AGENT2_ALLOWED_PROCESSORS) * 5 * 3


@pytest.mark.parametrize(
    "row",
    [
        _score_row(source_processor="future_unknown_processor"),
        _score_row(source_processor="tier2_exact_rule_v1"),
        _score_row(derivation="future_unknown_derivation"),
        _score_row(tag="기타"),
        _score_row(tag="future_unknown_category"),
    ],
)
def test_unknown_or_excluded_rows_fail_closed_in_python_and_sql(row: Agent2ScoreRow) -> None:
    assert not is_agent2_eligible(row)
    assert not _sql_accepts(row)


def test_orphan_news_is_a_hard_failure_and_sql_rejects_it() -> None:
    row = _score_row(news_exists=False)

    with pytest.raises(OrphanNewsError, match="news-1"):
        is_agent2_eligible(row)

    assert not _sql_accepts(row)


def test_eligible_event_projection_preserves_rows_but_news_identity_is_distinct() -> None:
    first = _score_row(news_id="shared", score=60)
    duplicate = replace(first, score=70)
    excluded = _score_row(news_id="excluded", tag="기타")

    events = eligible_agent2_events((first, duplicate, excluded))

    assert [event.news_id for event in events] == ["shared", "shared"]
    assert eligible_agent2_news_ids(events) == frozenset({"shared"})


def test_eligibility_revision_is_content_addressed_and_stable() -> None:
    payload = agent2_eligibility_revision_payload()

    assert payload["allowed_processors"] == sorted(AGENT2_ALLOWED_PROCESSORS)
    assert payload["allowed_derivations"] == sorted(AGENT2_ALLOWED_DERIVATIONS)
    assert payload["orphan_news"] == "hard_fail"
    assert payload["identity"] == "distinct_news_id"
    assert re.fullmatch(r"[0-9a-f]{64}", AGENT2_ELIGIBILITY_REVISION)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert AGENT2_ELIGIBILITY_REVISION == hashlib.sha256(canonical).hexdigest()


def test_eligibility_sql_predicate_rejects_unsafe_aliases() -> None:
    with pytest.raises(ValueError):
        agent2_eligibility_sql_predicate("s;DROP", "n")
    with pytest.raises(ValueError):
        agent2_eligibility_sql_predicate("s", "n;DROP")
