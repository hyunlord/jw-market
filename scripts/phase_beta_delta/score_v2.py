#!/usr/bin/env python3
"""Score crawled news JSON files through GenOS workflow 196.

Output contract:
- Keep the original news JSON as the source of truth and add only score
  marker metadata.
- Write detailed LLM output to _scored/<YYYY-MM>/<news_id>_scored.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


DEFAULT_WORKFLOW_ID = 196
DEFAULT_TIMEOUT_SEC = 60
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_WORKERS = 4
ASSUMED_MODEL_FOR_COST = "gpt-4o-mini"
ASSUMED_INPUT_USD_PER_1M = 0.150
ASSUMED_OUTPUT_USD_PER_1M = 0.600
VALID_TAGS = {"신약/R&D", "정책/규제", "공급/생산", "자본/경영", "외부/트렌드", "기타"}


@dataclass(frozen=True)
class RuntimeConfig:
    genos_url: str
    genos_token: str
    workflow_id: int
    timeout_sec: int
    max_retries: int
    max_workers: int
    output_root: Path
    output_dir: Path | None
    mark_original: bool
    dry_run: bool


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        json.dump(obj, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def load_catalog(path: Path) -> tuple[dict[str, str], str, set[str]]:
    raw = path.read_text(encoding="utf-8")
    catalog = json.loads(raw)
    if not isinstance(catalog, dict):
        raise ValueError(f"catalog must be a JSON object keyed by JW brand: {path}")
    return catalog, hashlib.sha1(raw.encode("utf-8")).hexdigest(), set(catalog.keys())


def strip_code_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_workflow_response(response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse known GenOS workflow response shapes.

    The archive score.py expects response["data"]["text"] containing a JSON
    string. This parser also accepts direct dict responses for safer testing.
    """

    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    data = response.get("data")
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        parsed = json.loads(strip_code_fence(data["text"]))
        if isinstance(parsed, list):
            parsed = {"matches": parsed, "summary": "", "tag": "관련 없음"}
        if not isinstance(parsed, dict):
            raise ValueError("workflow data.text JSON must be an object or list")
        return parsed, usage
    if isinstance(response.get("text"), str):
        parsed = json.loads(strip_code_fence(response["text"]))
        if isinstance(parsed, list):
            parsed = {"matches": parsed, "summary": "", "tag": "관련 없음"}
        if not isinstance(parsed, dict):
            raise ValueError("workflow text JSON must be an object or list")
        return parsed, usage
    if isinstance(response.get("matches"), list):
        return response, usage
    raise ValueError(f"unexpected workflow response shape: keys={sorted(response.keys())}")


def normalize_match(match: dict[str, Any], jw25: set[str]) -> dict[str, Any]:
    brand = match.get("drug") or match.get("jw_brand") or match.get("brand")
    if brand not in jw25:
        raise ValueError(f"non-JW or unknown brand in LLM output: {brand!r}")

    score = match.get("score", match.get("importance", 0))
    try:
        score = float(score)
    except (TypeError, ValueError):
        raise ValueError(f"score must be numeric for {brand!r}: {score!r}")
    if not 0 <= score <= 100:
        raise ValueError(f"score out of range for {brand!r}: {score}")
    reason = match.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"reason must be non-empty for {brand!r}")

    return {
        "drug": brand,
        "score": int(score) if score.is_integer() else score,
        "reason": reason.strip(),
    }


def validate_result(result: dict[str, Any], jw25: set[str]) -> dict[str, Any]:
    matches = result.get("matches")
    if not isinstance(matches, list):
        raise ValueError("result.matches must be a list")
    return {
        "matches": [normalize_match(m, jw25) for m in matches if isinstance(m, dict)],
        "summary": result.get("summary") or "",
        "tag": result.get("tag") if result.get("tag") in VALID_TAGS else "기타",
    }


def infer_output_root(batch_dir: Path) -> Path:
    parts = batch_dir.resolve().parts
    if "_batches" in parts:
        idx = parts.index("_batches")
        return Path(*parts[:idx])
    return batch_dir.resolve().parent


def resolve_source_path(batch_file: Path, output_root: Path) -> Path:
    """Map _batches/<YYYY-MM>/<site>__<file>.json back to site source file."""

    try:
        batch_file.relative_to(output_root / "_batches")
    except ValueError:
        return batch_file

    name = batch_file.name
    if "__" not in name:
        return batch_file
    site, original_name = name.split("__", 1)
    candidate = output_root / site / f"news_5years_{site}" / original_name
    return candidate if candidate.exists() else batch_file


def relative_to_root(path: Path, output_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(output_root.resolve()))
    except ValueError:
        return str(path)


def build_payload(news: dict[str, Any], catalog: dict[str, str], workflow_id: int) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "input": {
            "news": {
                "title": news.get("title", ""),
                "content": news.get("content", ""),
                "date": news.get("date"),
                "sources": news.get("sources", []),
                "search_keyword": news.get("search_keyword"),
                "matched_jw_search_contexts": news.get("matched_jw_search_contexts", []),
                "matched_search_keywords": news.get("matched_search_keywords", []),
            },
            "catalog": catalog,
        },
        # The archive workflow accepts question-only payloads; this richer input
        # is kept explicit for v2 contract compatibility.
        "question": (
            "카탈로그:\n"
            f"{json.dumps(catalog, ensure_ascii=False)}\n\n"
            f"제목: {news.get('title', '')}\n\n"
            f"내용: {news.get('content', '')}\n\n"
            f"matched_search_keywords: {news.get('matched_search_keywords', [])}\n"
            f"matched_jw_search_contexts: {news.get('matched_jw_search_contexts', [])}"
        ),
    }


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for mixed Korean and English text."""

    return max(1, len(text) // 3)


def first_present_number(mapping: dict[str, Any], keys: tuple[str, ...]) -> int | float | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def build_llm_meta(payload: dict[str, Any], result: dict[str, Any], usage: dict[str, Any], duration: float) -> dict[str, Any]:
    tokens_in_actual = first_present_number(usage, ("input_tokens", "prompt_tokens", "tokens_in"))
    tokens_out_actual = first_present_number(usage, ("output_tokens", "completion_tokens", "tokens_out"))
    cost_actual = first_present_number(usage, ("cost_usd", "total_cost_usd"))

    question = payload.get("question")
    if not isinstance(question, str):
        question = json.dumps(payload, ensure_ascii=False)
    tokens_in_estimated = estimate_tokens(question)
    tokens_out_estimated = estimate_tokens(json.dumps(result, ensure_ascii=False))
    cost_estimated = (
        tokens_in_estimated * ASSUMED_INPUT_USD_PER_1M / 1_000_000
        + tokens_out_estimated * ASSUMED_OUTPUT_USD_PER_1M / 1_000_000
    )

    return {
        "model": usage.get("model") or ASSUMED_MODEL_FOR_COST,
        "duration_sec": duration,
        "tokens_in_actual": tokens_in_actual,
        "tokens_out_actual": tokens_out_actual,
        "tokens_in_estimated": tokens_in_estimated,
        "tokens_out_estimated": tokens_out_estimated,
        "cost_usd_actual": cost_actual,
        "cost_usd_estimated": cost_estimated,
        "model_assumed": ASSUMED_MODEL_FOR_COST,
        # Backward-compatible aliases for existing summary scripts.
        "tokens_in": tokens_in_actual if tokens_in_actual is not None else tokens_in_estimated,
        "tokens_out": tokens_out_actual if tokens_out_actual is not None else tokens_out_estimated,
        "cost_usd": cost_actual if cost_actual is not None else cost_estimated,
    }


def call_workflow(
    session: requests.Session,
    config: RuntimeConfig,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], float]:
    url = f"{config.genos_url.rstrip('/')}/api/gateway/workflow/{config.workflow_id}/run/v2"
    last_error: Exception | None = None
    for attempt in range(config.max_retries):
        started = time.time()
        try:
            response = session.post(url, json=payload, timeout=config.timeout_sec)
            response.raise_for_status()
            parsed, usage = parse_workflow_response(response.json())
            return parsed, usage, time.time() - started
        except Exception as exc:  # noqa: BLE001 - recorded for retry diagnostics
            last_error = exc
            logging.warning("workflow attempt %s/%s failed: %s", attempt + 1, config.max_retries, exc)
            if attempt + 1 < config.max_retries:
                time.sleep(2**attempt)
    raise RuntimeError(f"workflow failed after {config.max_retries} attempts: {last_error}")


def mark_news(
    path: Path,
    *,
    scored: bool,
    status: str,
    scored_at: str | None = None,
    score_file: str | None = None,
    workflow_id: int | None = None,
    error: str | None = None,
) -> None:
    news = read_json(path)
    news["scored"] = scored
    news["score_status"] = status
    if scored_at:
        news["scored_at"] = scored_at
    if score_file:
        news["score_file"] = score_file
    if workflow_id is not None:
        news["workflow_id"] = workflow_id
    if error:
        news["last_error"] = error
        news["last_attempt_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    atomic_write_json(path, news)


def score_one(
    batch_file: Path,
    catalog: dict[str, str],
    catalog_sha1: str,
    jw25: set[str],
    config: RuntimeConfig,
    session: requests.Session,
) -> dict[str, Any]:
    source_path = resolve_source_path(batch_file, config.output_root)
    source_news = read_json(source_path)
    if source_news.get("scored") is True and source_news.get("score_status") == "ok":
        return {"news_id": source_path.stem, "status": "skipped", "source_path": str(source_path)}

    yyyymm = (source_news.get("date") or "unknown")[:7] or "unknown"
    scored_dir = config.output_dir or (config.output_root / "_scored" / yyyymm)
    scored_path = scored_dir / f"{source_path.stem}_scored.json"
    scored_rel = relative_to_root(scored_path, config.output_root)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if config.dry_run and not config.genos_token:
        payload = build_payload(source_news, catalog, config.workflow_id)
        fake = {
            "matches": [],
            "summary": "DRY RUN ONLY: no GENOS_TOKEN provided, workflow not called.",
            "tag": "dry_run",
        }
        usage: dict[str, Any] = {}
        duration = 0.0
    else:
        payload = build_payload(source_news, catalog, config.workflow_id)
        raw_result, usage, duration = call_workflow(session, config, payload)
        fake = validate_result(raw_result, jw25)

    scored_obj = {
        "news_id": source_path.stem,
        "source_path": relative_to_root(source_path, config.output_root),
        "batch_path": relative_to_root(batch_file, config.output_root),
        "scored_at": now,
        "workflow_id": config.workflow_id,
        "catalog_version": catalog_sha1,
        "matches": fake["matches"],
        "summary": fake["summary"],
        "tag": fake["tag"],
        "llm_meta": build_llm_meta(payload, fake, usage, duration),
    }
    atomic_write_json(scored_path, scored_obj)

    if config.mark_original:
        mark_news(
            source_path,
            scored=True,
            status="ok",
            scored_at=now,
            score_file=scored_rel,
            workflow_id=config.workflow_id,
        )
        if batch_file != source_path:
            mark_news(
                batch_file,
                scored=True,
                status="ok",
                scored_at=now,
                score_file=scored_rel,
                workflow_id=config.workflow_id,
            )

    return {
        "news_id": source_path.stem,
        "status": "ok",
        "source_path": str(source_path),
        "score_file": str(scored_path),
        "duration_sec": duration,
    }


def score_one_with_failure_marking(*args: Any, **kwargs: Any) -> dict[str, Any]:
    batch_file = args[0]
    config = args[4]
    source_path = resolve_source_path(batch_file, config.output_root)
    try:
        return score_one(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - persisted as batch status
        logging.exception("score failed for %s", batch_file)
        if config.mark_original:
            for path in {source_path, batch_file}:
                if path.exists():
                    mark_news(path, scored=False, status="failed", workflow_id=config.workflow_id, error=str(exc))
        return {
            "news_id": source_path.stem,
            "status": "failed",
            "source_path": str(source_path),
            "error": str(exc),
        }


def collect_news_files(batch_dir: Path, limit: int) -> list[Path]:
    files = sorted(p for p in batch_dir.glob("*.json") if p.name != "_log.json")
    return files[:limit] if limit else files


def write_run_log(output_root: Path, batch_name: str, results: list[dict[str, Any]]) -> Path:
    scored_dir = output_root / "_scored" / batch_name
    scored_dir.mkdir(parents=True, exist_ok=True)
    log_path = scored_dir / "_log.json"
    summary = {
        "batch": batch_name,
        "total": len(results),
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "skipped": sum(1 for r in results if r.get("status") == "skipped"),
        "last_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
    }
    atomic_write_json(log_path, summary)
    return log_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--output-root", type=Path, help="Defaults to parent before _batches in --batch-dir.")
    parser.add_argument("--output-dir", type=Path, help="Directory where *_scored.json files are written.")
    parser.add_argument("--limit", type=int, default=0, help="0 means all files.")
    parser.add_argument("--dry-run", action="store_true", help="Process at most 5 files unless --limit is smaller.")
    parser.add_argument("--no-mark-original", action="store_true", help="Write _scored files only.")
    parser.add_argument("--genos-url", default=os.environ.get("GENOS_URL", ""))
    parser.add_argument("--genos-token", default=os.environ.get("GENOS_TOKEN", ""))
    parser.add_argument("--workflow-id", type=int, default=int(os.environ.get("WORKFLOW_ID", DEFAULT_WORKFLOW_ID)))
    parser.add_argument("--max-workers", type=int, default=int(os.environ.get("MAX_WORKERS", DEFAULT_MAX_WORKERS)))
    parser.add_argument("--timeout-sec", type=int, default=int(os.environ.get("TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)))
    parser.add_argument("--max-retries", type=int, default=int(os.environ.get("MAX_RETRIES", DEFAULT_MAX_RETRIES)))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    batch_dir = args.batch_dir.resolve()
    if not batch_dir.exists():
        raise FileNotFoundError(batch_dir)
    output_root = (args.output_root or infer_output_root(batch_dir)).resolve()
    catalog, catalog_sha1, jw25 = load_catalog(args.catalog)

    limit = args.limit
    if args.dry_run:
        limit = min(limit, 5) if limit else 5
    news_files = collect_news_files(batch_dir, limit)

    if not args.dry_run and (not args.genos_url or not args.genos_token):
        raise RuntimeError("GENOS_URL and GENOS_TOKEN are required unless --dry-run is used")

    config = RuntimeConfig(
        genos_url=args.genos_url,
        genos_token=args.genos_token,
        workflow_id=args.workflow_id,
        timeout_sec=args.timeout_sec,
        max_retries=args.max_retries,
        max_workers=max(1, args.max_workers),
        output_root=output_root,
        output_dir=args.output_dir.resolve() if args.output_dir else None,
        mark_original=not args.no_mark_original,
        dry_run=args.dry_run,
    )

    logging.info("batch=%s files=%s output_root=%s", batch_dir, len(news_files), output_root)
    results: list[dict[str, Any]] = []
    with requests.Session() as session:
        if config.genos_token:
            session.headers.update({"Authorization": f"Bearer {config.genos_token}"})
        with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
            futures = {
                executor.submit(
                    score_one_with_failure_marking,
                    path,
                    catalog,
                    catalog_sha1,
                    jw25,
                    config,
                    session,
                ): path
                for path in news_files
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                logging.info("%s %s", result.get("status"), result.get("news_id"))

    log_path = write_run_log(output_root, batch_dir.name, results)
    summary = read_json(log_path)
    print(json.dumps({k: summary[k] for k in ("batch", "total", "ok", "failed", "skipped", "last_at")}, ensure_ascii=False, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
