from __future__ import annotations

import json
import os
import sys
import time
import uuid
import urllib.request
from pathlib import Path
from typing import Final

import pymysql

BACKUP_TABLE: Final = "event_brand_scores_bak_v3rescore_20260704_pre5347"
ENDPOINT: Final = "http://workflow-196.llmops.svc.cluster.local:8080/run/v2"
CATALOG_PATH: Final = Path("/tmp/wf196_catalog_text.txt")
OUT_DIR: Final = Path("/tmp/wf196_v3_chunks")
CHUNK_SIZE: Final = 1000
REQUEST_TIMEOUT_SEC: Final = 420
MAX_ATTEMPTS: Final = 3
VALID_TAGS: Final = {"신약/R&D", "정책/규제", "공급/생산", "자본/경영", "외부/트렌드", "기타"}
BRAND_ALIASES: Final = {"위너프A+": "위너프에이플러스", "위너프에이플러스": "위너프A+"}


def chunk_index() -> int:
    return int(os.environ["CHUNK_INDEX"])


def result_path() -> Path:
    return OUT_DIR / f"chunk_{chunk_index():02d}_results.jsonl"


def summary_path() -> Path:
    return OUT_DIR / f"chunk_{chunk_index():02d}_rescore_summary.json"


def tag_events_path() -> Path:
    return OUT_DIR / f"chunk_{chunk_index():02d}_tag_events.jsonl"


def connect_db():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database="jw_mart",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def strip_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```json"):
        value = value[7:]
    elif value.startswith("```"):
        value = value[3:]
    return value[:-3].strip() if value.endswith("```") else value


def find_text(value) -> str | None:
    if isinstance(value, str):
        return value if value.strip().startswith(("{", "[")) or "matches" in value else None
    if isinstance(value, list):
        return next((got for item in value if (got := find_text(item))), None)
    if not isinstance(value, dict):
        return None
    for path in [("data", "text"), ("data", "answer"), ("data", "output"), ("data", "result"), ("data", "response"), ("text",), ("answer",), ("output",), ("result",), ("response",)]:
        cursor = value
        for key in path:
            if not isinstance(cursor, dict) or key not in cursor:
                break
            cursor = cursor[key]
        else:
            if got := find_text(cursor):
                return got
    return next((got for item in value.values() if (got := find_text(item))), None)


def post_json(payload: dict) -> dict:
    request = urllib.request.Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def call_workflow_once(question: str) -> tuple[dict, float, bool]:
    chat_id = f"wf196-v3-rescore-{chunk_index()}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    started = time.time()
    payload = {"question": question, "chatId": chat_id, "sessionId": chat_id, "overrideConfig": {"sessionId": chat_id}}
    first = post_json(payload)
    text = find_text(first)
    status = str(first.get("status") or first.get("state") or "").upper()
    resume_sent = False
    if status == "STOPPED" or not text:
        first = post_json({"chatId": chat_id, "sessionId": chat_id, "humanInput": {"type": "proceed", "startNodeId": "humanInputAgentflow_0", "feedback": "승인"}})
        resume_sent = True
        text = find_text(first)
    return json.loads(strip_fence(text or "")), time.time() - started, resume_sent


def call_workflow(question: str) -> tuple[dict, float, bool, int]:
    last_error: BaseException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            parsed, elapsed, resume_sent = call_workflow_once(question)
            return parsed, elapsed, resume_sent, attempt
        except TimeoutError as exc:
            last_error = exc
            print(json.dumps({"chunk": chunk_index(), "timeout_attempt": attempt, "retrying": attempt < MAX_ATTEMPTS}, ensure_ascii=False), flush=True)
    raise RuntimeError(f"workflow timed out after {MAX_ATTEMPTS} attempts") from last_error


def build_question(catalog_text: str, row: dict) -> str:
    return f"카탈로그:\n{catalog_text}\n\n제목: {row['title']}\n\n내용: {row['body']}\n\nsearch_keyword: {row['keyword']}"


def score_tier(score: int) -> str:
    return "very_weak" if score < 30 else "weak" if score < 50 else "moderate" if score < 70 else "strong" if score < 85 else "very_strong"


def brand_keys(value: str) -> set[str]:
    normalized = value.strip()
    return {normalized, BRAND_ALIASES[normalized]} if normalized in BRAND_ALIASES else {normalized}


def parse_matches(parsed: dict) -> dict[str, tuple[int, str]]:
    raw_matches = parsed.get("matches") or []
    if not isinstance(raw_matches, list):
        raise ValueError("matches is not a list")
    matches: dict[str, tuple[int, str]] = {}
    for item in raw_matches:
        if not isinstance(item, dict):
            continue
        brand = str(item.get("drug") or item.get("jw_brand") or item.get("brand") or item.get("brand_name") or "").strip()
        if brand:
            score = max(0, min(100, int(round(float(item.get("score") or 0)))))
            for key in brand_keys(brand):
                matches[key] = (score, str(item.get("reason") or ""))
    return matches


def fetch_chunk_rows() -> list[dict]:
    offset = chunk_index() * CHUNK_SIZE
    sql = f"""
        SELECT b.id AS row_id, b.news_id, b.brand_canonical AS brand,
               COALESCE(n.title, '') AS title, COALESCE(n.article_text, '') AS body,
               COALESCE(n.search_keyword, '') AS keyword
        FROM {BACKUP_TABLE} b
        JOIN (SELECT news_id FROM {BACKUP_TABLE} GROUP BY news_id ORDER BY SHA2(news_id, 256), news_id LIMIT %s OFFSET %s) chunk
          ON chunk.news_id = b.news_id
        JOIN news_raw n ON n.news_id = b.news_id
        ORDER BY b.news_id, b.id
    """
    conn = connect_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (CHUNK_SIZE, offset))
            return list(cursor.fetchall())
    finally:
        conn.close()


def grouped_rows(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["news_id"]), []).append(row)
    return grouped


def load_checkpoint() -> list[dict]:
    if not result_path().exists():
        return []
    with result_path().open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def append_result(update: dict) -> None:
    with result_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(update, ensure_ascii=False, sort_keys=True) + "\n")


def append_tag_event(event: dict) -> None:
    with tag_events_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def load_tag_events() -> list[dict]:
    if not tag_events_path().exists():
        return []
    with tag_events_path().open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def retry_invalid_tag(news_id: str, question: str, original_tag: str) -> tuple[dict, str, float, bool, int]:
    retry_parsed, retry_elapsed, retry_resume_sent, retry_attempts = call_workflow(question)
    retry_tag = str(retry_parsed.get("tag") or "").strip()
    if retry_tag in VALID_TAGS:
        append_tag_event({"news_id": news_id, "original_tag": original_tag, "retry_tag": retry_tag, "final_tag": retry_tag, "action": "retried"})
        return retry_parsed, retry_tag, retry_elapsed, retry_resume_sent, retry_attempts
    append_tag_event({"news_id": news_id, "original_tag": original_tag, "retry_tag": retry_tag, "final_tag": "기타", "action": "fallback"})
    return retry_parsed, "기타", retry_elapsed, retry_resume_sent, retry_attempts


def collect_updates(catalog_text: str, grouped: dict[str, list[dict]]) -> list[dict]:
    updates = load_checkpoint()
    completed = {str(update["news_id"]) for update in updates}
    for index, (news_id, rows) in enumerate(grouped.items(), start=1):
        if news_id in completed:
            continue
        question = build_question(catalog_text, rows[0])
        parsed, elapsed, resume_sent, attempts = call_workflow(question)
        tag = str(parsed.get("tag") or "").strip()
        if tag not in VALID_TAGS:
            parsed, tag, retry_elapsed, retry_resume_sent, retry_attempts = retry_invalid_tag(news_id, question, tag)
            elapsed += retry_elapsed
            resume_sent = resume_sent or retry_resume_sent
            attempts += retry_attempts
        matches = parse_matches(parsed)
        summary = str(parsed.get("summary") or "")
        meta = json.dumps({"model": None, "tokens_in": None, "tokens_out": None, "duration_sec": elapsed, "cost_usd": None, "rescore": "wf196_v3_rev5347"}, ensure_ascii=False, sort_keys=True)
        for row in rows:
            match = next((matches[key] for key in brand_keys(str(row["brand"])) if key in matches), None)
            score, reason = match if match else (0, None)
            update = {"row_id": int(row["row_id"]), "news_id": news_id, "brand": str(row["brand"]), "score": score, "score_tier": score_tier(score), "reason": reason, "tag": tag, "summary": summary, "llm_meta": meta, "resume_sent": resume_sent, "elapsed_sec": round(elapsed, 3), "match_count": len(matches), "attempts": attempts}
            updates.append(update)
            append_result(update)
        if index % 25 == 0 or index == 1:
            print(json.dumps({"chunk": chunk_index(), "processed_news": index, "updates": len(updates), "last_news_id": news_id}, ensure_ascii=False), flush=True)
    return updates


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = fetch_chunk_rows()
    grouped = grouped_rows(rows)
    if not rows or not grouped:
        raise ValueError(f"empty chunk {chunk_index()}")
    updates = collect_updates(CATALOG_PATH.read_text(encoding="utf-8"), grouped)
    if len(updates) != len(rows):
        raise ValueError(f"checkpoint mismatch rows={len(updates)} expected={len(rows)}")
    tag_events = load_tag_events()
    tag_retried = sum(1 for event in tag_events if event.get("action") == "retried")
    tag_fallback = sum(1 for event in tag_events if event.get("action") == "fallback")
    summary = {"chunk": chunk_index(), "news": len(grouped), "rows": len(rows), "checkpoint_rows": len(updates), "tag_violations": 0, "tag_retried": tag_retried, "tag_fallback": tag_fallback, "tag_events_path": str(tag_events_path()), "result_path": str(result_path())}
    summary_path().write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print("RESCORE_SUMMARY=" + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
