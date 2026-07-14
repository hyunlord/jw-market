"""Score Tier2 body matches with the GA scoring workflow and replace exact-rule rows safely.

This runner is the canonical batch path for Tier2 LLM scoring. It consumes
``tier2_match_staging`` and writes workflow results to a separate staging table.
Live ``event_brand_scores`` is changed only by the explicit ``replace-live``
command, after staging validation has passed and an exact-rule backup exists.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Sequence

import pymysql

DEFAULT_MATCH_TABLE = "tier2_match_staging"
DEFAULT_WORKFLOW_URL = "http://workflow-337.llmops.svc.cluster.local:8080/run/v2"
DEFAULT_WORKFLOW_ID = 337
DEFAULT_WORKFLOW_REV = 5671
DEFAULT_DEPLOYMENT_ID = 1453
DEFAULT_SOURCE_PROCESSOR = "tier2_llm_v1"
PENDING_SOURCE_PROCESSOR = "tier2_llm_v2_rev5671"
TIER2_EXACT_PROCESSOR = "tier2_exact_rule_v1"
TIER1_PROCESSORS = ("workflow_196_optionB", "workflow_196_rev5674", "cross_match_adapter_v1")
MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_TIMEOUT_SECONDS = 420
DEFAULT_BATCH_SIZE = 200
WORKFLOW_CALL_COST_KRW = 3.39

CATEGORY_CODE_BY_LABEL = {
    "신약/R&D": "rd",
    "자본/경영": "capital",
    "정책/규제": "policy",
    "외부/트렌드": "external",
    "공급/생산": "supply",
    "기타": "external",
}


def connect_from_env() -> pymysql.connections.Connection:
    """Open the d2 connection without depending on another repository module."""

    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "jw_mart_d2_stage_20260630_r2"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```json"):
        value = value[7:]
    elif value.startswith("```"):
        value = value[3:]
    if value.endswith("```"):
        value = value[:-3]
    return value.strip()


@dataclass(frozen=True)
class MatchedBrand:
    brand_key: str
    brand_name: str
    match_source: str
    matched_keywords: tuple[str, ...]


@dataclass(frozen=True)
class NewsScoringInput:
    news_id: str
    title: str
    body: str
    source_name: str
    article_url: str
    published_date: str
    collected_at: dt.datetime | None
    expire_at: dt.datetime | None
    brands: tuple[MatchedBrand, ...]


@dataclass(frozen=True)
class ParsedTier2Score:
    brand_key: str
    brand_name: str
    score: int
    reason: str


@dataclass(frozen=True)
class ParsedWf324Response:
    tag: str
    category_label: str
    category_code: str
    summary: str
    scores: tuple[ParsedTier2Score, ...]


@dataclass(frozen=True)
class WorkflowCallResult:
    parsed: ParsedWf324Response
    raw_response: dict[str, Any]
    elapsed_sec: float
    attempts: int
    resume_sent: bool


@dataclass(frozen=True)
class StagedScoreRow:
    event_id: str
    news_id: str
    brand_name: str
    brand_canonical: str
    score: int
    score_tier: str
    reason: str
    tag: str
    summary: str
    llm_meta: str
    collected_at: dt.datetime | None
    expire_at: dt.datetime | None


def score_tier(score: int) -> str:
    return (
        "very_weak"
        if score < 30
        else "weak"
        if score < 50
        else "moderate"
        if score < 70
        else "strong"
        if score < 85
        else "very_strong"
    )


def make_staging_table_name() -> str:
    return "event_brand_scores_tier2_staging_" + dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")


def scoped_event_id(news_id: str, source_processor: str) -> str:
    """Return a deterministic event id that can coexist with the exact-rule row."""

    if source_processor != PENDING_SOURCE_PROCESSOR:
        raise ValueError(f"unsupported append processor={source_processor!r}")
    readable = f"{news_id}:t2v2r5671"
    if len(readable) <= 64:
        return readable
    digest = hashlib.sha256(f"{source_processor}\t{news_id}".encode()).hexdigest()
    return f"t2v2:{digest[:58]}"


def build_workflow_payload(
    *,
    news_id: str,
    title: str,
    body: str,
    source_name: str,
    article_url: str,
    published_date: str,
    brands: Sequence[MatchedBrand],
) -> dict[str, Any]:
    return {
        "article": {
            "news_id": news_id,
            "title": title,
            "body": body,
            "source_name": source_name,
            "article_url": article_url,
            "published_date": published_date,
        },
        "target_brands": [
            {
                "brand_key": brand.brand_key,
                "brand_name": brand.brand_name,
                "match_source": brand.match_source,
                "matched_keywords": list(brand.matched_keywords),
            }
            for brand in brands
        ],
    }


def _nested_texts(value: object) -> Iterable[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in {"question", "input", "inputQuestion"}:
                continue
            yield from _nested_texts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_texts(child)


def find_workflow_text(response: dict[str, Any]) -> str:
    data = response.get("data")
    if isinstance(data, dict):
        text = data.get("text")
        if isinstance(text, str) and text.strip():
            return text
        executed = data.get("agentFlowExecutedData")
        if isinstance(executed, list):
            for item in reversed(executed):
                for text_value in _nested_texts(item):
                    candidate = text_value.strip()
                    if candidate.startswith("{") or candidate.startswith("```"):
                        return candidate
    for text_value in _nested_texts(response):
        candidate = text_value.strip()
        if candidate.startswith("{") or candidate.startswith("```"):
            return candidate
    return ""


def _score_from_value(value: object) -> int:
    try:
        score = int(round(float(value if value is not None else 0)))
    except (TypeError, ValueError):
        raise ValueError(f"invalid score value={value!r}") from None
    return max(0, min(100, score))


def parse_wf324_response(raw_text: str, brands: Sequence[MatchedBrand]) -> ParsedWf324Response:
    payload = json.loads(strip_json_fence(raw_text))
    if not isinstance(payload, dict):
        raise ValueError("scoring workflow response must be a JSON object")

    tag = str(payload.get("tag") or payload.get("category_label") or "").strip()
    category_label = str(payload.get("category_label") or tag).strip()
    category_code = str(payload.get("category_code") or "").strip()
    if tag not in CATEGORY_CODE_BY_LABEL:
        raise ValueError(f"invalid tag={tag!r}")
    if category_label != tag:
        raise ValueError(f"category_label must match tag: tag={tag!r} label={category_label!r}")
    expected_code = CATEGORY_CODE_BY_LABEL[tag]
    if category_code != expected_code:
        raise ValueError(
            f"category_code mismatch for tag={tag!r}: expected={expected_code!r} actual={category_code!r}"
        )

    rows = payload.get("brand_scores")
    if not isinstance(rows, list):
        raise ValueError("scoring workflow response must contain brand_scores list")

    allowed = {brand.brand_key: brand for brand in brands}
    seen: set[str] = set()
    parsed_rows: list[ParsedTier2Score] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("scoring workflow brand_scores item must be an object")
        brand_key = str(row.get("brand_key") or "").strip()
        if brand_key not in allowed:
            raise ValueError(f"scoring workflow returned out-of-candidate brand_key={brand_key!r}")
        if brand_key in seen:
            raise ValueError(f"scoring workflow returned duplicate brand_key={brand_key!r}")
        seen.add(brand_key)
        parsed_rows.append(
            ParsedTier2Score(
                brand_key=brand_key,
                brand_name=str(row.get("brand_name") or allowed[brand_key].brand_name).strip(),
                score=_score_from_value(row.get("score")),
                reason=str(row.get("reason") or row.get("evidence") or "").strip(),
            )
        )

    missing = set(allowed) - seen
    if missing:
        raise ValueError(f"scoring workflow omitted candidate brand_key(s): {sorted(missing)}")

    return ParsedWf324Response(
        tag=tag,
        category_label=category_label,
        category_code=category_code,
        summary=str(payload.get("summary") or "").strip(),
        scores=tuple(parsed_rows),
    )


def post_json(url: str, payload: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def call_workflow_once(
    payload: dict[str, Any],
    *,
    workflow_url: str,
    timeout_seconds: int,
) -> tuple[dict[str, Any], float, bool]:
    chat_id = f"tier2-wf337-{payload['article']['news_id']}-{uuid.uuid4().hex[:8]}"
    question = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    started = time.time()
    raw = post_json(
        workflow_url,
        {
            "question": question,
            "chatId": chat_id,
            "sessionId": chat_id,
            "overrideConfig": {"sessionId": chat_id},
        },
        timeout_seconds=timeout_seconds,
    )
    text = find_workflow_text(raw)
    status = str(raw.get("status") or raw.get("state") or "").upper()
    resume_sent = False
    if status == "STOPPED" or not text:
        raw = post_json(
            workflow_url,
            {
                "chatId": chat_id,
                "sessionId": chat_id,
                "humanInput": {
                    "type": "proceed",
                    "startNodeId": "humanInputAgentflow_0",
                    "feedback": "승인",
                },
            },
            timeout_seconds=timeout_seconds,
        )
        resume_sent = True
    return raw, time.time() - started, resume_sent


def call_workflow(
    item: NewsScoringInput,
    *,
    workflow_url: str,
    timeout_seconds: int,
    max_attempts: int,
) -> WorkflowCallResult:
    payload = build_workflow_payload(
        news_id=item.news_id,
        title=item.title,
        body=item.body,
        source_name=item.source_name,
        article_url=item.article_url,
        published_date=item.published_date,
        brands=item.brands,
    )
    last_error: BaseException | None = None
    elapsed_total = 0.0
    resume_sent = False
    for attempt in range(1, max_attempts + 1):
        try:
            raw, elapsed, did_resume = call_workflow_once(
                payload,
                workflow_url=workflow_url,
                timeout_seconds=timeout_seconds,
            )
            text = find_workflow_text(raw)
            if not text:
                raise ValueError("workflow response text is empty")
            parsed = parse_wf324_response(text, item.brands)
            return WorkflowCallResult(
                parsed=parsed,
                raw_response=raw,
                elapsed_sec=elapsed_total + elapsed,
                attempts=attempt,
                resume_sent=resume_sent or did_resume,
            )
        except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                break
            if attempt < max_attempts:
                time.sleep(5 * attempt)
            elapsed_total += 0.0
    raise RuntimeError(f"scoring workflow failed for news_id={item.news_id}: {last_error}") from last_error


def _json_keywords(value: object) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if str(item or "").strip())


def load_scoring_inputs(
    conn: pymysql.connections.Connection,
    *,
    match_table: str,
    offset: int,
    limit: int | None,
) -> list[NewsScoringInput]:
    limit_sql = "" if limit is None else " LIMIT %s OFFSET %s"
    params: list[object] = []
    if limit is not None:
        params.extend([limit, offset])
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT m.news_id,
                   m.brand_key,
                   m.brand_canonical,
                   m.match_source,
                   m.matched_keywords,
                   COALESCE(n.title, e.title, '') AS title,
                   COALESCE(n.article_text, e.body_full, '') AS body,
                   COALESCE(n.source_name, e.source_name, '') AS source_name,
                   COALESCE(n.article_url, e.source_url, '') AS article_url,
                   COALESCE(CAST(n.published_date AS CHAR), CAST(e.date AS CHAR), '') AS published_date,
                   COALESCE(e.collected_at, n.collected_at) AS collected_at,
                   COALESCE(e.expire_at, n.expire_at) AS expire_at
            FROM (
                SELECT news_id
                FROM `{match_table}`
                GROUP BY news_id
                ORDER BY news_id
                {limit_sql}
            ) target
            JOIN `{match_table}` m ON m.news_id = target.news_id
            LEFT JOIN news_raw n ON n.news_id = m.news_id
            LEFT JOIN events e ON e.news_id = m.news_id
            ORDER BY m.news_id, m.brand_canonical, m.brand_key
            """,
            params,
        )
        rows = cursor.fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["news_id"]), []).append(row)

    out: list[NewsScoringInput] = []
    for news_id, news_rows in grouped.items():
        first = news_rows[0]
        brands = tuple(
            MatchedBrand(
                brand_key=str(row["brand_key"]),
                brand_name=str(row["brand_canonical"]),
                match_source=str(row["match_source"]),
                matched_keywords=_json_keywords(row.get("matched_keywords")),
            )
            for row in news_rows
        )
        out.append(
            NewsScoringInput(
                news_id=news_id,
                title=str(first.get("title") or ""),
                body=str(first.get("body") or ""),
                source_name=str(first.get("source_name") or ""),
                article_url=str(first.get("article_url") or ""),
                published_date=str(first.get("published_date") or ""),
                collected_at=first.get("collected_at"),
                expire_at=first.get("expire_at"),
                brands=brands,
            )
        )
    return out


def load_pending_exact_inputs(
    conn: pymysql.connections.Connection,
    *,
    source_processor: str,
    target_processor: str,
    limit: int,
) -> list[NewsScoringInput]:
    """Load exact-rule article/brand pairs that lack the target marker."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.news_id,
                   COALESCE(g.brand_key, s.brand_canonical) AS brand_key,
                   s.brand_canonical,
                   'tier2_exact_rule_v1' AS match_source,
                   JSON_ARRAY(s.brand_canonical) AS matched_keywords,
                   COALESCE(n.title, '') AS title,
                   COALESCE(n.article_text, '') AS body,
                   COALESCE(n.source_name, '') AS source_name,
                   COALESCE(n.article_url, '') AS article_url,
                   COALESCE(CAST(n.published_date AS CHAR), '') AS published_date,
                   n.collected_at,
                   n.expire_at
            FROM (
                SELECT MIN(candidate.id) AS first_id, candidate.news_id
                FROM event_brand_scores candidate
                WHERE candidate.source_processor = %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM event_brand_scores scored
                      WHERE scored.news_id = candidate.news_id
                        AND scored.brand_canonical = candidate.brand_canonical
                        AND scored.source_processor = %s
                  )
                GROUP BY candidate.news_id
                ORDER BY first_id
                LIMIT %s
            ) pending
            JOIN event_brand_scores s
              ON s.news_id = pending.news_id
             AND s.source_processor = %s
            JOIN news_raw n ON n.news_id = s.news_id
            LEFT JOIN (
                SELECT REPLACE(brand_name, ' ', '') AS normalized_brand,
                       MIN(brand_key) AS brand_key
                FROM mart_general_brand_metric
                GROUP BY REPLACE(brand_name, ' ', '')
            ) g
              ON g.normalized_brand COLLATE utf8mb4_unicode_ci =
                 REPLACE(s.brand_canonical, ' ', '') COLLATE utf8mb4_unicode_ci
            WHERE NOT EXISTS (
                SELECT 1
                FROM event_brand_scores scored
                WHERE scored.news_id = s.news_id
                  AND scored.brand_canonical = s.brand_canonical
                  AND scored.source_processor = %s
            )
            ORDER BY pending.first_id, s.brand_canonical, brand_key
            """,
            (source_processor, target_processor, limit, source_processor, target_processor),
        )
        rows = cursor.fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["news_id"]), []).append(row)

    items: list[NewsScoringInput] = []
    for news_id, news_rows in grouped.items():
        first = news_rows[0]
        items.append(
            NewsScoringInput(
                news_id=news_id,
                title=str(first.get("title") or ""),
                body=str(first.get("body") or ""),
                source_name=str(first.get("source_name") or ""),
                article_url=str(first.get("article_url") or ""),
                published_date=str(first.get("published_date") or ""),
                collected_at=first.get("collected_at"),
                expire_at=first.get("expire_at"),
                brands=tuple(
                    MatchedBrand(
                        brand_key=str(row["brand_key"]),
                        brand_name=str(row["brand_canonical"]),
                        match_source=str(row["match_source"]),
                        matched_keywords=_json_keywords(row.get("matched_keywords")),
                    )
                    for row in news_rows
                ),
            )
        )
    return items


def create_staging_table(conn: pymysql.connections.Connection, table_name: str, *, replace: bool) -> None:
    with conn.cursor() as cursor:
        if replace:
            cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
        cursor.execute(f"CREATE TABLE IF NOT EXISTS `{table_name}` LIKE event_brand_scores")
    conn.commit()


def make_llm_meta(
    *,
    elapsed_sec: float,
    attempts: int,
    resume_sent: bool,
    news_id: str,
    brand_count: int,
) -> str:
    return json.dumps(
        {
            "workflow_id": DEFAULT_WORKFLOW_ID,
            "workflow_rev": DEFAULT_WORKFLOW_REV,
            "deployment_id": DEFAULT_DEPLOYMENT_ID,
            "duration_sec": round(elapsed_sec, 3),
            "attempts": attempts,
            "resume_sent": resume_sent,
            "tier2_multibrand": True,
            "news_id": news_id,
            "brand_count": brand_count,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def rows_from_result(item: NewsScoringInput, result: WorkflowCallResult) -> list[StagedScoreRow]:
    score_by_key = {row.brand_key: row for row in result.parsed.scores}
    llm_meta = make_llm_meta(
        elapsed_sec=result.elapsed_sec,
        attempts=result.attempts,
        resume_sent=result.resume_sent,
        news_id=item.news_id,
        brand_count=len(item.brands),
    )
    rows: list[StagedScoreRow] = []
    for brand in item.brands:
        parsed = score_by_key[brand.brand_key]
        rows.append(
            StagedScoreRow(
                event_id=item.news_id,
                news_id=item.news_id,
                brand_name=brand.brand_name,
                brand_canonical=brand.brand_name,
                score=parsed.score,
                score_tier=score_tier(parsed.score),
                reason=parsed.reason,
                tag=result.parsed.tag,
                summary=result.parsed.summary,
                llm_meta=llm_meta,
                collected_at=item.collected_at,
                expire_at=item.expire_at,
            )
        )
    return rows


def insert_staged_rows(
    conn: pymysql.connections.Connection,
    *,
    staging_table: str,
    rows: Sequence[StagedScoreRow],
    batch_size: int,
) -> int:
    if not rows:
        return 0
    sql = f"""
        INSERT INTO `{staging_table}`
          (event_id, brand_name, brand_canonical, is_jw, score, score_tier, reason,
           source_processor, generated_at, news_id, derivation, tag, summary,
           workflow_id, llm_meta, tier, collected_at, expire_at)
        VALUES
          (%s, %s, %s, 0, %s, %s, %s,
           %s, CURRENT_TIMESTAMP(), %s, 'llm_direct', %s, %s,
           %s, %s, 2, COALESCE(%s, CURRENT_TIMESTAMP()), %s)
        ON DUPLICATE KEY UPDATE
          brand_name = VALUES(brand_name),
          score = VALUES(score),
          score_tier = VALUES(score_tier),
          reason = VALUES(reason),
          source_processor = VALUES(source_processor),
          tag = VALUES(tag),
          summary = VALUES(summary),
          workflow_id = VALUES(workflow_id),
          llm_meta = VALUES(llm_meta),
          tier = VALUES(tier),
          collected_at = VALUES(collected_at),
          expire_at = VALUES(expire_at)
    """
    inserted = 0
    with conn.cursor() as cursor:
        for offset in range(0, len(rows), batch_size):
            chunk = rows[offset : offset + batch_size]
            cursor.executemany(
                sql,
                [
                    (
                        row.event_id,
                        row.brand_name,
                        row.brand_canonical,
                        row.score,
                        row.score_tier,
                        row.reason,
                        DEFAULT_SOURCE_PROCESSOR,
                        row.news_id,
                        row.tag,
                        row.summary,
                        DEFAULT_WORKFLOW_ID,
                        row.llm_meta,
                        row.collected_at,
                        row.expire_at,
                    )
                    for row in chunk
                ],
            )
            inserted += len(chunk)
            conn.commit()
    return inserted


def insert_live_rows(
    conn: pymysql.connections.Connection,
    *,
    rows: Sequence[StagedScoreRow],
    source_processor: str,
) -> int:
    """Append new processor rows without updating an existing score row."""

    if not rows:
        return 0
    sql = """
        INSERT INTO event_brand_scores
          (event_id, brand_name, brand_canonical, is_jw, score, score_tier, reason,
           source_processor, generated_at, news_id, derivation, tag, summary,
           workflow_id, llm_meta, tier, collected_at, expire_at)
        VALUES
          (%s, %s, %s, 0, %s, %s, %s,
           %s, CURRENT_TIMESTAMP(), %s, 'llm_direct', %s, %s,
           %s, %s, 2, COALESCE(%s, CURRENT_TIMESTAMP()), %s)
    """
    with conn.cursor() as cursor:
        cursor.executemany(
            sql,
            [
                (
                    scoped_event_id(row.news_id, source_processor),
                    row.brand_name,
                    row.brand_canonical,
                    row.score,
                    row.score_tier,
                    row.reason,
                    source_processor,
                    row.news_id,
                    row.tag,
                    row.summary,
                    DEFAULT_WORKFLOW_ID,
                    row.llm_meta,
                    row.collected_at,
                    row.expire_at,
                )
                for row in rows
            ],
        )
        inserted = int(cursor.rowcount)
    conn.commit()
    return inserted


def processor_snapshot(
    conn: pymysql.connections.Connection,
    source_processor: str,
) -> dict[str, int]:
    """Return the row and article counts for an immutable processor generation."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS rows_count, COUNT(DISTINCT news_id) AS news_count
            FROM event_brand_scores
            WHERE source_processor = %s
            """,
            (source_processor,),
        )
        row = cursor.fetchone()
    return {"rows": int(row["rows_count"]), "news": int(row["news_count"])}


def events_raw_gap(conn: pymysql.connections.Connection) -> int:
    """Return source articles that have not been cloned into ``events_raw``."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS gap
            FROM news_raw n
            LEFT JOIN events_raw e ON e.news_id = n.news_id
            WHERE e.news_id IS NULL
            """
        )
        return int(cursor.fetchone()["gap"] or 0)


def sync_missing_events_raw(
    conn: pymysql.connections.Connection,
    *,
    retries: int = 1,
) -> dict[str, int]:
    """Insert missing source rows and hard-gate the loader on a zero gap."""

    total_inserted = 0
    last_gap = 0
    for _attempt in range(retries + 1):
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO events_raw (
                    news_id, source_name, published_date, title, summary,
                    body, url, created_at, ingested_at
                )
                SELECT
                    n.news_id,
                    n.source_name,
                    n.published_date,
                    n.title,
                    LEFT(COALESCE(n.article_text, ''), 1000),
                    n.article_text,
                    n.article_url,
                    n.ingested_at,
                    n.ingested_at
                FROM news_raw n
                LEFT JOIN events_raw e ON e.news_id = n.news_id
                WHERE e.news_id IS NULL
                """
            )
            total_inserted += int(cursor.rowcount or 0)
        conn.commit()
        last_gap = events_raw_gap(conn)
        if last_gap == 0:
            return {"inserted": total_inserted, "gap": 0}
    raise RuntimeError(
        f"events_raw sync gap remains after {retries + 1} attempt(s): {last_gap}"
    )


def run_append_live(
    conn: pymysql.connections.Connection,
    *,
    source_processor: str,
    target_processor: str,
    workflow_url: str,
    timeout_seconds: int,
    daily_call_limit: int,
    max_cost_krw: float,
) -> dict[str, object]:
    """Score pending exact rows and append a fail-closed processor generation."""

    if target_processor != PENDING_SOURCE_PROCESSOR:
        raise ValueError(f"unsupported target processor={target_processor!r}")
    estimated_cost = daily_call_limit * WORKFLOW_CALL_COST_KRW
    if estimated_cost > max_cost_krw + 1e-9:
        raise ValueError(
            f"daily call limit exceeds cost guard: calls={daily_call_limit} "
            f"estimated_krw={estimated_cost:.2f} max_krw={max_cost_krw:.2f}"
        )

    source_before = processor_snapshot(conn, source_processor)
    target_before = processor_snapshot(conn, target_processor)
    items = load_pending_exact_inputs(
        conn,
        source_processor=source_processor,
        target_processor=target_processor,
        limit=daily_call_limit,
    )
    failures: list[dict[str, object]] = []
    inserted_rows = 0
    workflow_calls = 0
    consecutive_failures = 0
    started = time.time()

    for item in items:
        try:
            result = call_workflow(
                item,
                workflow_url=workflow_url,
                timeout_seconds=timeout_seconds,
                max_attempts=1,
            )
            workflow_calls += result.attempts
            inserted_rows += insert_live_rows(
                conn,
                rows=rows_from_result(item, result),
                source_processor=target_processor,
            )
            consecutive_failures = 0
        except (RuntimeError, pymysql.MySQLError) as exc:
            conn.rollback()
            workflow_calls += 1
            failures.append({"news_id": item.news_id, "error": str(exc)})
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError(
                    f"aborting after {consecutive_failures} consecutive append failures"
                ) from exc

    source_after = processor_snapshot(conn, source_processor)
    if source_before != source_after:
        raise RuntimeError(
            f"source processor changed during append: before={source_before} after={source_after}"
        )
    return {
        "source_processor": source_processor,
        "target_processor": target_processor,
        "pending_news_selected": len(items),
        "workflow_calls": workflow_calls,
        "inserted_rows": inserted_rows,
        "failures": failures,
        "estimated_cost_krw": round(workflow_calls * WORKFLOW_CALL_COST_KRW, 2),
        "source_before": source_before,
        "source_after": source_after,
        "target_before": target_before,
        "target_after": processor_snapshot(conn, target_processor),
        "elapsed_sec": round(time.time() - started, 3),
    }


def validate_staging(
    conn: pymysql.connections.Connection,
    *,
    match_table: str,
    staging_table: str,
) -> dict[str, object]:
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS cnt, COUNT(DISTINCT news_id) AS news FROM `{match_table}`")
        match_counts = cursor.fetchone()
        cursor.execute(f"SELECT COUNT(*) AS cnt, COUNT(DISTINCT news_id) AS news FROM `{staging_table}`")
        staging_counts = cursor.fetchone()
        cursor.execute(
            f"""
            SELECT COUNT(*) AS cnt
            FROM `{staging_table}` s
            LEFT JOIN `{match_table}` m
              ON m.news_id = s.news_id
             AND m.brand_key = s.brand_id
            WHERE s.source_processor = %s
              AND m.brand_key IS NULL
            """,
            (DEFAULT_SOURCE_PROCESSOR,),
        )
        # brand_id is intentionally unused by this runner, so fall back to canonical validation below.
        brand_id_outside = int(cursor.fetchone()["cnt"])
        cursor.execute(
            f"""
            SELECT COUNT(*) AS cnt
            FROM `{staging_table}` s
            LEFT JOIN `{match_table}` m
              ON m.news_id = s.news_id
             AND m.brand_canonical = s.brand_canonical
            WHERE s.source_processor = %s
              AND m.news_id IS NULL
            """,
            (DEFAULT_SOURCE_PROCESSOR,),
        )
        outside = int(cursor.fetchone()["cnt"])
        cursor.execute(
            f"""
            SELECT brand_canonical, news_id, COUNT(*) AS cnt
            FROM `{staging_table}`
            GROUP BY event_id, brand_canonical
            HAVING cnt > 1
            LIMIT 1
            """
        )
        duplicate = cursor.fetchone()
        cursor.execute(
            f"""
            SELECT tag, COUNT(*) AS cnt
            FROM `{staging_table}`
            WHERE source_processor = %s
            GROUP BY tag
            ORDER BY cnt DESC
            """,
            (DEFAULT_SOURCE_PROCESSOR,),
        )
        tag_counts = {str(row["tag"]): int(row["cnt"]) for row in cursor.fetchall()}
        cursor.execute(
            f"""
            SELECT
              AVG(score) AS avg_score,
              SUM(score >= 60) AS ge60,
              COUNT(*) AS cnt
            FROM `{staging_table}`
            WHERE source_processor = %s
            """,
            (DEFAULT_SOURCE_PROCESSOR,),
        )
        score_stats = cursor.fetchone()

    expected_rows = int(match_counts["cnt"])
    expected_news = int(match_counts["news"])
    staged_rows = int(staging_counts["cnt"])
    staged_news = int(staging_counts["news"])
    valid = (
        expected_rows == staged_rows
        and expected_news == staged_news
        and outside == 0
        and duplicate is None
    )
    return {
        "valid": valid,
        "expected_rows": expected_rows,
        "expected_news": expected_news,
        "staged_rows": staged_rows,
        "staged_news": staged_news,
        "outside_by_brand_id_probe": brand_id_outside,
        "outside_candidates": outside,
        "duplicate_event_brand": duplicate,
        "tag_counts": tag_counts,
        "score_stats": {
            "avg_score": float(score_stats["avg_score"] or 0),
            "ge60": int(score_stats["ge60"] or 0),
            "rows": int(score_stats["cnt"] or 0),
        },
    }


def hash_exact_rule(conn: pymysql.connections.Connection, table_name: str) -> str:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT SHA2(GROUP_CONCAT(row_hash ORDER BY row_hash SEPARATOR ''), 256) AS digest
            FROM (
              SELECT SHA2(CONCAT_WS('|',
                id, event_id, COALESCE(news_id,''), brand_name, COALESCE(brand_canonical,''),
                score, COALESCE(score_tier,''), COALESCE(reason,''), COALESCE(tag,''),
                COALESCE(summary,''), COALESCE(source_processor,''), tier
              ), 256) AS row_hash
              FROM `{table_name}`
              WHERE source_processor = %s
            ) h
            """,
            (TIER2_EXACT_PROCESSOR,),
        )
        return str(cursor.fetchone()["digest"] or "")


def backup_exact_rule(conn: pymysql.connections.Connection, backup_table: str) -> dict[str, object]:
    with conn.cursor() as cursor:
        cursor.execute(f"DROP TABLE IF EXISTS `{backup_table}`")
        cursor.execute(
            f"""
            CREATE TABLE `{backup_table}` AS
            SELECT *
            FROM event_brand_scores
            WHERE source_processor = %s
            """,
            (TIER2_EXACT_PROCESSOR,),
        )
        cursor.execute(f"SELECT COUNT(*) AS cnt FROM `{backup_table}`")
        rows = int(cursor.fetchone()["cnt"])
    conn.commit()
    digest = hash_exact_rule(conn, backup_table)
    return {"backup_table": backup_table, "rows": rows, "hash": digest}


def _tier1_processor_placeholders() -> str:
    return ", ".join(["%s"] * len(TIER1_PROCESSORS))


def update_tier2_only_event_categories(
    conn: pymysql.connections.Connection,
    *,
    staging_table: str,
) -> int:
    placeholders = _tier1_processor_placeholders()
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE events e
            JOIN (
              SELECT news_id, MIN(tag) AS category_label,
                     CASE MIN(tag)
                       WHEN '신약/R&D' THEN 'rd'
                       WHEN '자본/경영' THEN 'capital'
                       WHEN '정책/규제' THEN 'policy'
                       WHEN '공급/생산' THEN 'supply'
                       ELSE 'external'
                     END AS category_code
              FROM `{staging_table}`
              WHERE source_processor = %s
              GROUP BY news_id
            ) s ON s.news_id = e.news_id
            LEFT JOIN (
              SELECT DISTINCT news_id
              FROM event_brand_scores
              WHERE source_processor IN ({placeholders})
                AND news_id IS NOT NULL
            ) tier1 ON tier1.news_id = e.news_id
            SET e.category = s.category_code,
                e.category_label = s.category_label,
                e.processed_by = %s,
                e.processed_at = CURRENT_TIMESTAMP()
            WHERE tier1.news_id IS NULL
            """,
            (DEFAULT_SOURCE_PROCESSOR, *TIER1_PROCESSORS, DEFAULT_SOURCE_PROCESSOR),
        )
        updated = int(cursor.rowcount)
    conn.commit()
    return updated


def update_live_tier2_categories(conn: pymysql.connections.Connection) -> int:
    """Refresh categories for Tier2-only news from approved live score generations."""

    tier1_placeholders = _tier1_processor_placeholders()
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE events e
            JOIN (
              SELECT news_id, MIN(tag) AS category_label,
                     CASE MIN(tag)
                       WHEN '신약/R&D' THEN 'rd'
                       WHEN '자본/경영' THEN 'capital'
                       WHEN '정책/규제' THEN 'policy'
                       WHEN '공급/생산' THEN 'supply'
                       ELSE 'external'
                     END AS category_code
              FROM event_brand_scores
              WHERE source_processor IN (%s, %s)
                AND news_id IS NOT NULL
              GROUP BY news_id
            ) tier2 ON tier2.news_id = e.news_id
            LEFT JOIN (
              SELECT DISTINCT news_id
              FROM event_brand_scores
              WHERE source_processor IN ({tier1_placeholders})
                AND news_id IS NOT NULL
            ) tier1 ON tier1.news_id = e.news_id
            SET e.category = tier2.category_code,
                e.category_label = tier2.category_label,
                e.processed_by = %s,
                e.processed_at = CURRENT_TIMESTAMP()
            WHERE tier1.news_id IS NULL
            """,
            (
                DEFAULT_SOURCE_PROCESSOR,
                PENDING_SOURCE_PROCESSOR,
                *TIER1_PROCESSORS,
                PENDING_SOURCE_PROCESSOR,
            ),
        )
        updated = int(cursor.rowcount)
    conn.commit()
    return updated


def controlled_replace(
    conn: pymysql.connections.Connection,
    *,
    staging_table: str,
    backup_table: str,
) -> dict[str, object]:
    validation = validate_staging(conn, match_table=DEFAULT_MATCH_TABLE, staging_table=staging_table)
    if not validation["valid"]:
        raise ValueError(f"staging validation failed before replace: {validation}")
    backup = backup_exact_rule(conn, backup_table)
    placeholders = _tier1_processor_placeholders()
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) AS cnt FROM event_brand_scores WHERE source_processor IN ({placeholders})",
            TIER1_PROCESSORS,
        )
        tier1_before = int(cursor.fetchone()["cnt"])
        cursor.execute(
            "DELETE FROM event_brand_scores WHERE source_processor = %s",
            (TIER2_EXACT_PROCESSOR,),
        )
        deleted = int(cursor.rowcount)
        cursor.execute(
            f"""
            INSERT INTO event_brand_scores
              (event_id, brand_name, brand_canonical, brand_id, ml_id, cd_id, is_jw,
               score, score_tier, reason, source_processor, generated_at, news_id,
               derivation, mirrored_from_jw_brands, tag, summary, workflow_id,
               catalog_version, llm_meta, tier, collected_at, expire_at)
            SELECT event_id, brand_name, brand_canonical, brand_id, ml_id, cd_id, is_jw,
                   score, score_tier, reason, source_processor, generated_at, news_id,
                   derivation, mirrored_from_jw_brands, tag, summary, workflow_id,
                   catalog_version, llm_meta, tier, collected_at, expire_at
            FROM `{staging_table}`
            WHERE source_processor = %s
            """,
            (DEFAULT_SOURCE_PROCESSOR,),
        )
        inserted = int(cursor.rowcount)
        cursor.execute(
            f"SELECT COUNT(*) AS cnt FROM event_brand_scores WHERE source_processor IN ({placeholders})",
            TIER1_PROCESSORS,
        )
        tier1_after = int(cursor.fetchone()["cnt"])
    conn.commit()
    if tier1_before != tier1_after:
        raise RuntimeError(f"tier1 row count changed: before={tier1_before} after={tier1_after}")
    updated_event_categories = update_tier2_only_event_categories(conn, staging_table=staging_table)
    return {
        "backup": backup,
        "deleted_exact_rule": deleted,
        "inserted_tier2_llm": inserted,
        "updated_tier2_only_event_categories": updated_event_categories,
        "tier1_before": tier1_before,
        "tier1_after": tier1_after,
    }


def run_score_staging(
    conn: pymysql.connections.Connection,
    *,
    match_table: str,
    staging_table: str,
    replace_staging: bool,
    limit: int | None,
    offset: int,
    workflow_url: str,
    timeout_seconds: int,
    max_attempts: int,
    batch_size: int,
) -> dict[str, object]:
    create_staging_table(conn, staging_table, replace=replace_staging)
    items = load_scoring_inputs(conn, match_table=match_table, offset=offset, limit=limit)
    failures: list[dict[str, object]] = []
    processed_news = 0
    inserted_rows = 0
    workflow_calls = 0
    consecutive_failures = 0
    started = time.time()

    for item in items:
        try:
            result = call_workflow(
                item,
                workflow_url=workflow_url,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
            rows = rows_from_result(item, result)
            inserted_rows += insert_staged_rows(
                conn,
                staging_table=staging_table,
                rows=rows,
                batch_size=batch_size,
            )
            workflow_calls += result.attempts
            processed_news += 1
            consecutive_failures = 0
        except Exception as exc:
            failures.append(
                {
                    "news_id": item.news_id,
                    "brand_count": len(item.brands),
                    "error": str(exc),
                }
            )
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError(
                    f"aborting after {consecutive_failures} consecutive scoring workflow failures"
                ) from exc
        if processed_news == 1 or processed_news % 25 == 0:
            print(
                json.dumps(
                    {
                        "processed_news": processed_news,
                        "inserted_rows": inserted_rows,
                        "workflow_calls": workflow_calls,
                        "failures": len(failures),
                        "last_news_id": item.news_id,
                        "elapsed_sec": round(time.time() - started, 1),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )

    validation = validate_staging(conn, match_table=match_table, staging_table=staging_table)
    return {
        "staging_table": staging_table,
        "input_news": len(items),
        "processed_news": processed_news,
        "inserted_rows": inserted_rows,
        "workflow_calls": workflow_calls,
        "failures": failures,
        "elapsed_sec": round(time.time() - started, 3),
        "validation": validation,
    }


def make_backup_table_name() -> str:
    return "event_brand_scores_bak_tier2_exact_" + dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    score_parser = subparsers.add_parser("score-staging")
    score_parser.add_argument("--match-table", default=DEFAULT_MATCH_TABLE)
    score_parser.add_argument("--staging-table", default=make_staging_table_name())
    score_parser.add_argument("--replace-staging", action="store_true")
    score_parser.add_argument("--limit", type=int)
    score_parser.add_argument("--offset", type=int, default=0)
    score_parser.add_argument(
        "--workflow-url",
        default=os.getenv("WF337_URL", os.getenv("WF324_URL", DEFAULT_WORKFLOW_URL)),
    )
    score_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    score_parser.add_argument("--max-attempts", type=int, default=2)
    score_parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)

    validate_parser = subparsers.add_parser("validate-staging")
    validate_parser.add_argument("--match-table", default=DEFAULT_MATCH_TABLE)
    validate_parser.add_argument("--staging-table", required=True)

    replace_parser = subparsers.add_parser("replace-live")
    replace_parser.add_argument("--staging-table", required=True)
    replace_parser.add_argument("--backup-table", default=make_backup_table_name())

    append_parser = subparsers.add_parser("append-live")
    append_parser.add_argument("--source-processor", default=TIER2_EXACT_PROCESSOR)
    append_parser.add_argument(
        "--target-processor",
        default=PENDING_SOURCE_PROCESSOR,
        choices=(PENDING_SOURCE_PROCESSOR,),
    )
    append_parser.add_argument(
        "--workflow-url",
        default=os.getenv("WF337_URL", DEFAULT_WORKFLOW_URL),
    )
    append_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    append_parser.add_argument("--daily-call-limit", type=int, default=60)
    append_parser.add_argument("--max-cost-krw", type=float, default=203.40)

    sync_parser = subparsers.add_parser("sync-events-raw")
    sync_parser.add_argument("--retries", type=int, default=1)

    subparsers.add_parser("refresh-live-categories")

    args = parser.parse_args()
    conn = connect_from_env()
    try:
        if args.command == "score-staging":
            summary = run_score_staging(
                conn,
                match_table=args.match_table,
                staging_table=args.staging_table,
                replace_staging=args.replace_staging,
                limit=args.limit,
                offset=args.offset,
                workflow_url=args.workflow_url,
                timeout_seconds=args.timeout_seconds,
                max_attempts=args.max_attempts,
                batch_size=args.batch_size,
            )
        elif args.command == "validate-staging":
            summary = validate_staging(
                conn,
                match_table=args.match_table,
                staging_table=args.staging_table,
            )
        elif args.command == "replace-live":
            summary = controlled_replace(
                conn,
                staging_table=args.staging_table,
                backup_table=args.backup_table,
            )
        elif args.command == "append-live":
            summary = run_append_live(
                conn,
                source_processor=args.source_processor,
                target_processor=args.target_processor,
                workflow_url=args.workflow_url,
                timeout_seconds=args.timeout_seconds,
                daily_call_limit=args.daily_call_limit,
                max_cost_krw=args.max_cost_krw,
            )
        elif args.command == "sync-events-raw":
            summary = sync_missing_events_raw(conn, retries=args.retries)
        elif args.command == "refresh-live-categories":
            summary = {
                "updated_event_categories": update_live_tier2_categories(conn),
                "processors": [DEFAULT_SOURCE_PROCESSOR, PENDING_SOURCE_PROCESSOR],
            }
        else:  # pragma: no cover - argparse enforces commands.
            raise ValueError(f"unknown command={args.command!r}")
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
