"""Runtime adapters for the post-crawl Agent2 hook."""

from __future__ import annotations

import hashlib
import json
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from pipeline.scripts.crawler.agent2_hook import (
    Agent2DetectionResult,
    detect_increased_brands_from_rows,
)
from pipeline.scripts.crawler.agent2_hook_receipt import write_detection_receipt
from pipeline.scripts.crawler.crawl_temporal_contract import (
    BASELINE_SCHEMA,
    CrawlDailyInput,
)


BRAND_SQL = """
SELECT brand_key, brand_name, raw_value_history
FROM mart_general_brand_metric
WHERE brand_key IS NOT NULL AND brand_key <> ''
  AND brand_name IS NOT NULL AND brand_name <> ''
"""
SCORE_SQL = """
SELECT s.news_id, s.brand_canonical, s.brand_name,
       s.mirrored_from_jw_brands, s.source_processor, s.derivation,
       s.tag, s.score, n.news_id AS joined_news_id, n.published_date
FROM event_brand_scores s
LEFT JOIN news_raw n ON s.news_id = n.news_id
"""


class QueryCursor(Protocol):
    def execute(self, sql: str) -> None: ...

    def fetchall(self) -> list[dict[str, object]]: ...


class QueryConnection(Protocol):
    def cursor(self) -> AbstractContextManager[QueryCursor]: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Agent2CommandRequest:
    repo_root: Path
    state_root: Path
    content_sha256: str
    brand_keys: tuple[str, ...]
    snapshot_at: str


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fetch_all(conn: QueryConnection, sql: str) -> list[dict[str, object]]:
    with conn.cursor() as cursor:
        cursor.execute(sql)
        return list(cursor.fetchall())


def load_baseline_news_ids(
    *,
    state_root: Path,
    run_id: str,
) -> dict[str, frozenset[str]]:
    """Load and verify the immutable pre-crawl baseline for one run."""

    pointer_path = state_root / "runs" / run_id / "baseline.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    snapshot_path = Path(str(pointer["snapshot_path"]))
    encoded = snapshot_path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != pointer.get("content_sha256"):
        raise RuntimeError("crawl exposure baseline hash mismatch")
    snapshot = json.loads(encoded)
    if snapshot.get("schema") != BASELINE_SCHEMA:
        raise RuntimeError("crawl exposure baseline schema mismatch")
    return {
        str(item["brand_canonical"]): frozenset(
            str(news_id) for news_id in item["news_ids"]
        )
        for item in snapshot["brands"]
    }


def query_detection_rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Read the current mart universe and score evidence without mutation."""

    from pipeline.scripts.crawler.tier2_full_scoring_runner import connect_from_env

    conn = connect_from_env()
    try:
        return _fetch_all(conn, BRAND_SQL), _fetch_all(conn, SCORE_SQL)
    finally:
        conn.rollback()
        conn.close()


def detect_and_write_receipt(
    config: CrawlDailyInput,
    *,
    snapshot_date: date | None = None,
) -> tuple[Agent2DetectionResult, dict[str, object]]:
    """Run central selection after refresh and persist its audit receipt."""

    state_root = Path(config.state_root)
    baseline = load_baseline_news_ids(
        state_root=state_root,
        run_id=config.run_id,
    )
    brand_rows, score_rows = query_detection_rows()
    result = detect_increased_brands_from_rows(
        brand_rows=brand_rows,
        score_rows=score_rows,
        baseline_news_ids_by_brand=baseline,
        snapshot_date=snapshot_date or datetime.now(UTC).date(),
    )
    pointer = write_detection_receipt(
        state_root=state_root,
        run_id=config.run_id,
        result=result,
    )
    return result, pointer


def build_agent2_command(
    request: Agent2CommandRequest,
) -> list[str]:
    """Build the exact-key wf217 command; it never performs a live cache swap."""

    if len(request.content_sha256) != 64:
        raise ValueError("content_sha256 must be a full SHA-256")
    input_dir = (
        request.state_root
        / "agent2-hook"
        / "inputs"
        / request.content_sha256
    )
    input_dir.mkdir(parents=True, exist_ok=True)
    keys_path = input_dir / "brand_keys.json"
    encoded_keys = json.dumps(
        list(request.brand_keys),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    if keys_path.exists() and keys_path.read_text(encoding="utf-8") != encoded_keys:
        raise RuntimeError("Agent2 brand-key input changed for an existing receipt")
    keys_path.write_text(encoded_keys, encoding="utf-8")
    return [
        sys.executable,
        str(
            request.repo_root
            / "pipeline/scripts/ai_analysis/agent2_regen_orchestrator.py"
        ),
        "--dry-run",
        "--brand-source",
        "general-density",
        "--brand-keys-file",
        str(keys_path),
        "--bundle-kind",
        "general",
        "--snapshot-at",
        request.snapshot_at,
        "--work-dir",
        str(request.state_root / "agent2-hook" / "generation"),
    ]
