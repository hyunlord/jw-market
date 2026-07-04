#!/usr/bin/env python3
"""Score raw news JSONs with workflow 196 and write loader_v2-ready files.

This script never mutates raw corpus JSON files. It writes one sibling
`_scored/<stem>_scored.json` file per input JSON so `corpus_loader_v2.py`
can resolve the original article with `source_path`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_WORKFLOW_ID = 196
DEFAULT_GENOS_URL = "https://jwai-dev.jwhealthcare.com"
SOURCE_PROCESSOR = "workflow_196_optionB_score_v2"
LOG = logging.getLogger("score_v2")


@dataclass(frozen=True)
class ScoreTarget:
    source_path: Path
    scored_path: Path
    schema: str
    legacy_schema: bool


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def catalog_version(catalog_path: Path) -> str:
    raw = catalog_path.read_text(encoding="utf-8")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def load_catalog(catalog_path: Path) -> tuple[dict[str, Any], set[str], str, str]:
    catalog = read_json(catalog_path)
    if not isinstance(catalog, dict):
        raise ValueError(f"catalog must be a JSON object keyed by JW brand: {catalog_path}")
    brands = {str(key).strip() for key in catalog.keys() if str(key).strip()}
    return catalog, brands, catalog_version(catalog_path), compact_json(catalog)


def first_source(news: dict[str, Any]) -> dict[str, Any]:
    sources = news.get("sources")
    if isinstance(sources, list) and sources and isinstance(sources[0], dict):
        return sources[0]
    return {}


def news_title(news: dict[str, Any]) -> str:
    return str(news.get("title") or "").strip()


def news_content(news: dict[str, Any]) -> str:
    for key in ("content", "article_text", "body", "summary"):
        value = news.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def news_search_keyword(news: dict[str, Any]) -> str:
    value = news.get("search_keyword")
    if isinstance(value, str):
        return value.strip()
    keywords = news.get("matched_search_keywords")
    if isinstance(keywords, list):
        return ", ".join(str(item).strip() for item in keywords if str(item).strip())
    return ""


def schema_kind(news: dict[str, Any]) -> tuple[str, bool]:
    if "matched_jw_search_contexts" in news or "article_text" in news or "published_date" in news:
        return "v2", False
    return "legacy_5key", True


def scored_path_for(source_path: Path, output_root: Path | None) -> Path:
    if output_root is not None:
        return output_root / source_path.parent.name / f"{source_path.stem}_scored.json"
    return source_path.parent / "_scored" / f"{source_path.stem}_scored.json"


def iter_json_files(input_paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    skip_names = {"_catalog.json", "orchestrator_report.json", "_log.json"}
    for item in input_paths:
        if item.is_file() and item.suffix.lower() == ".json" and item.name not in skip_names and not item.name.endswith("_scored.json"):
            files.append(item)
        elif item.is_dir():
            files.extend(
                path
                for path in item.rglob("*.json")
                if path.name not in skip_names and not path.name.endswith("_scored.json") and "_scored" not in path.parts
            )
    return sorted(set(files))


def select_targets(
    input_paths: list[Path],
    *,
    output_root: Path | None,
    limit: int | None,
    include_legacy: int,
    prefer_keywords: set[str],
    seed: int,
) -> list[ScoreTarget]:
    rng = random.Random(seed)
    modern: list[ScoreTarget] = []
    legacy: list[ScoreTarget] = []
    preferred: list[ScoreTarget] = []
    for path in iter_json_files(input_paths):
        try:
            news = read_json(path)
        except Exception:
            continue
        if not isinstance(news, dict) or not news_title(news) or not news_content(news):
            continue
        kind, is_legacy = schema_kind(news)
        target = ScoreTarget(path, scored_path_for(path, output_root), kind, is_legacy)
        haystack = f"{news_title(news)} {news_search_keyword(news)} {news_content(news)[:2000]}"
        if any(keyword and keyword in haystack for keyword in prefer_keywords):
            preferred.append(target)
        elif is_legacy:
            legacy.append(target)
        else:
            modern.append(target)
    rng.shuffle(preferred)
    rng.shuffle(modern)
    rng.shuffle(legacy)
    selected: list[ScoreTarget] = []
    selected.extend(legacy[: max(0, include_legacy)])
    remaining = None if limit is None else max(0, limit - len(selected))
    pool = preferred + modern + legacy[max(0, include_legacy) :]
    selected.extend(pool if remaining is None else pool[:remaining])
    return selected


def build_question(catalog_text: str, news: dict[str, Any]) -> str:
    return (
        f"카탈로그:\n{catalog_text}\n\n"
        f"제목: {news_title(news)}\n\n"
        f"내용: {news_content(news)}\n\n"
        f"search_keyword: {news_search_keyword(news)}"
    )


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    stripped = stripped.removeprefix("```json").removeprefix("```").removesuffix("```")
    return stripped.strip()


def parse_workflow_response(response: dict[str, Any]) -> dict[str, Any]:
    text = find_response_text(response)
    if not isinstance(text, str):
        raise ValueError("workflow response missing parseable text field")
    parsed = json.loads(strip_code_fence(text))
    if isinstance(parsed, dict):
        matches = parsed.get("matches")
        return {
            "matches": matches if isinstance(matches, list) else [],
            "summary": parsed.get("summary") or "",
            "tag": parsed.get("tag") or "기타",
        }
    if isinstance(parsed, list):
        return {"matches": parsed, "summary": "", "tag": "기타"}
    raise ValueError("workflow data.text JSON must be object or list")


def find_response_text(value: Any) -> str | None:
    """Find the workflow text payload across GenOS response variants."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("[") or "matches" in stripped:
            return value
        return None
    if isinstance(value, list):
        for item in value:
            found = find_response_text(item)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None
    for key_path in (
        ("data", "text"),
        ("data", "answer"),
        ("data", "output"),
        ("data", "result"),
        ("data", "response"),
        ("text",),
        ("answer",),
        ("output",),
        ("result",),
        ("response",),
    ):
        current: Any = value
        for key in key_path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        found = find_response_text(current)
        if found:
            return found
    for item in value.values():
        found = find_response_text(item)
        if found:
            return found
    return None


def normalize_score(value: Any) -> int:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.0
    return max(0, min(100, int(round(score))))


def normalize_matches(matches: list[Any], jw_brands: set[str]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in matches:
        if not isinstance(item, dict):
            continue
        brand = str(item.get("jw_brand") or item.get("drug") or item.get("brand") or item.get("brand_name") or "").strip()
        if brand not in jw_brands or brand in seen:
            continue
        seen.add(brand)
        normalized.append(
            {
                "drug": brand,
                "jw_brand": brand,
                "score": normalize_score(item.get("score", item.get("importance", 0))),
                "reason": str(item.get("reason") or "").strip(),
                "derivation": "llm_direct",
            }
        )
    return normalized


def call_workflow(
    session: requests.Session,
    *,
    genos_url: str,
    workflow_id: int,
    direct_run_url: str | None,
    two_step_human_input: bool,
    question: str,
    timeout: int,
    retries: int,
    backoff: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    url = direct_run_url or f"{genos_url.rstrip('/')}/api/gateway/workflow/{workflow_id}/run/v2"
    last_error: Exception | None = None
    for attempt in range(1, retries + 2):
        started = time.monotonic()
        try:
            if two_step_human_input:
                chat_id = f"score-v2-{int(time.time())}-{uuid.uuid4().hex[:10]}"
                first_payload = {
                    "question": question,
                    "chatId": chat_id,
                    "sessionId": chat_id,
                    "overrideConfig": {"sessionId": chat_id},
                }
                response = session.post(url, json=first_payload, timeout=timeout)
                response.raise_for_status()
                first_response = response.json()
                first_status = ""
                if isinstance(first_response, dict):
                    first_status = str(first_response.get("status") or first_response.get("state") or "").upper()
                    if first_response.get("code") not in (None, 0):
                        raise RuntimeError(
                            "workflow application error "
                            f"code={first_response.get('code')} error_code={first_response.get('error_code')} "
                            f"errMsg={first_response.get('errMsg')}"
                        )
                # wf196 deployments differ by revision: some stop at a humanInput node,
                # while the current direct pod can finish on the first request. Resume only
                # when the first response is actually waiting; resuming a FINISHED run causes
                # Flowise to return "Only executions in STOPPED state can be resumed."
                if first_status != "STOPPED" and find_response_text(first_response):
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    meta = {
                        "http_status": response.status_code,
                        "attempt": attempt,
                        "elapsed_ms": elapsed_ms,
                        "two_step_human_input": True,
                        "resume_sent": False,
                        "first_code": first_response.get("code") if isinstance(first_response, dict) else None,
                        "first_status": first_status or None,
                    }
                    return first_response, meta
                resume_payload = {
                    "chatId": chat_id,
                    "sessionId": chat_id,
                    "humanInput": {
                        "type": "proceed",
                        "startNodeId": "humanInputAgentflow_0",
                        "feedback": "승인",
                    },
                }
                response = session.post(url, json=resume_payload, timeout=timeout)
            else:
                first_response = None
                response = session.post(url, json={"question": question}, timeout=timeout)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("code") not in (None, 0):
                raise RuntimeError(
                    "workflow application error "
                    f"code={payload.get('code')} error_code={payload.get('error_code')} "
                    f"errMsg={payload.get('errMsg')}"
                )
            meta = {"http_status": response.status_code, "attempt": attempt, "elapsed_ms": elapsed_ms}
            if two_step_human_input:
                meta["two_step_human_input"] = True
                meta["resume_sent"] = True
                if isinstance(first_response, dict):
                    meta["first_code"] = first_response.get("code")
                    meta["first_status"] = first_response.get("status") or first_response.get("state")
            return payload, meta
        except Exception as exc:
            last_error = exc
            if attempt > retries:
                break
            sleep_for = backoff * (2 ** (attempt - 1))
            LOG.warning("workflow call failed attempt=%s retry_in=%.1fs error=%s", attempt, sleep_for, exc)
            time.sleep(sleep_for)
    raise RuntimeError(f"workflow call failed after retries: {last_error}")


def build_scored_payload(
    *,
    news: dict[str, Any],
    source_path: Path,
    source_root: Path,
    response_result: dict[str, Any],
    response_meta: dict[str, Any],
    jw_brands: set[str],
    workflow_id: int,
    catalog_version_value: str,
    genos_url: str,
    serving_id: str | None,
    model_hint: str | None,
) -> dict[str, Any]:
    kind, is_legacy = schema_kind(news)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rel_source = os.path.relpath(source_path, source_root)
    source = first_source(news)
    matches = normalize_matches(response_result.get("matches") or [], jw_brands)
    payload = {
        "matches": matches,
        "summary": response_result.get("summary") or "",
        "tag": response_result.get("tag") or "기타",
        "scored_at": now,
        "workflow_id": workflow_id,
        "catalog_version": catalog_version_value,
        "llm_meta": {
            "genos_url": genos_url,
            "serving_id": serving_id,
            "model": model_hint,
            "source_processor": SOURCE_PROCESSOR,
            **response_meta,
        },
        "source_path": rel_source,
        "batch_path": rel_source,
        "source_file_name": source_path.name,
        "schema": kind,
        "legacy_schema": is_legacy,
        "cross_match_input_absent": not bool(news.get("matched_jw_search_contexts")),
        "matched_search_keywords": news.get("matched_search_keywords") or [],
        "matched_jw_search_contexts": news.get("matched_jw_search_contexts") or [],
        "sources": news.get("sources") or ([source] if source else []),
        "article_url": source.get("url") or news.get("article_url") or news.get("url"),
        "title": news_title(news),
    }
    return payload


def process_targets(args: argparse.Namespace) -> dict[str, Any]:
    catalog, jw_brands, version, catalog_text = load_catalog(args.catalog)
    source_root = args.source_root.resolve() if args.source_root else common_root(args.inputs)
    targets = select_targets(
        [path.resolve() for path in args.inputs],
        output_root=args.output_root.resolve() if args.output_root else None,
        limit=args.limit,
        include_legacy=args.include_legacy,
        prefer_keywords=set(args.prefer_keyword or []),
        seed=args.seed,
    )
    token = os.environ.get(args.token_env)
    if not token and not args.mock_response and not args.direct_run_url:
        raise SystemExit(f"missing token env: {args.token_env}")
    session = requests.Session()
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})
    run_log: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "workflow_id": args.workflow_id,
        "catalog_version": version,
        "source_root": str(source_root),
        "limit": args.limit,
        "targets_selected": len(targets),
        "created": 0,
        "skipped": 0,
        "failed": 0,
        "items": [],
    }
    mock_result = None
    if args.mock_response:
        mock_result = read_json(args.mock_response)
    for idx, target in enumerate(targets, start=1):
        item = {
            "index": idx,
            "source_path": str(target.source_path),
            "scored_path": str(target.scored_path),
            "schema": target.schema,
            "legacy_schema": target.legacy_schema,
        }
        if target.scored_path.exists() and not args.force:
            item["status"] = "skipped_existing"
            run_log["skipped"] += 1
            run_log["items"].append(item)
            continue
        try:
            news = read_json(target.source_path)
            if mock_result is not None:
                result = mock_result
                meta = {"mock": True, "http_status": None, "attempt": 0, "elapsed_ms": 0}
            else:
                raw_response, meta = call_workflow(
                    session,
                    genos_url=args.genos_url,
                    workflow_id=args.workflow_id,
                    direct_run_url=args.direct_run_url,
                    two_step_human_input=args.two_step_human_input,
                    question=build_question(catalog_text, news),
                    timeout=args.timeout,
                    retries=args.retries,
                    backoff=args.backoff,
                )
                result = parse_workflow_response(raw_response)
            scored = build_scored_payload(
                news=news,
                source_path=target.source_path,
                source_root=source_root,
                response_result=result,
                response_meta=meta,
                jw_brands=jw_brands,
                workflow_id=args.workflow_id,
                catalog_version_value=version,
                genos_url=args.genos_url,
                serving_id=args.serving_id,
                model_hint=args.model,
            )
            write_json(target.scored_path, scored)
            item.update(
                {
                    "status": "created",
                    "match_count": len(scored["matches"]),
                    "tag": scored["tag"],
                    "cross_match_input_absent": scored["cross_match_input_absent"],
                }
            )
            run_log["created"] += 1
            LOG.info("created %s matches=%s legacy=%s", target.scored_path, len(scored["matches"]), target.legacy_schema)
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = str(exc)
            run_log["failed"] += 1
            LOG.error("failed source=%s error=%s", target.source_path, exc)
        run_log["items"].append(item)
    run_log["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if args.run_log:
        write_json(args.run_log, run_log)
    return run_log


def common_root(paths: list[Path]) -> Path:
    resolved = [str(path.resolve()) for path in paths]
    return Path(os.path.commonpath(resolved)) if resolved else Path.cwd()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="Raw JSON files or directories to scan")
    parser.add_argument("--catalog", type=Path, default=Path("docs/crawl/_catalog.json"))
    parser.add_argument("--source-root", type=Path, help="Root used for relative source_path/batch_path")
    parser.add_argument("--output-root", type=Path, help="Optional central output root; default writes sibling _scored dirs")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--include-legacy", type=int, default=5)
    parser.add_argument("--prefer-keyword", action="append", default=[])
    parser.add_argument("--seed", type=int, default=196)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--genos-url", default=os.environ.get("GENOS_URL", DEFAULT_GENOS_URL))
    parser.add_argument("--direct-run-url", default=os.environ.get("WF196_DIRECT_RUN_URL"))
    parser.add_argument("--two-step-human-input", action="store_true")
    parser.add_argument("--workflow-id", type=int, default=int(os.environ.get("WF196_WORKFLOW_ID", DEFAULT_WORKFLOW_ID)))
    parser.add_argument("--serving-id", default=os.environ.get("WF196_SERVING_ID", "163"))
    parser.add_argument("--model", default=os.environ.get("WF196_MODEL", "flash-3.1-lite"))
    parser.add_argument("--token-env", default="GENOS_TOKEN")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--backoff", type=float, default=5.0)
    parser.add_argument("--run-log", type=Path)
    parser.add_argument("--mock-response", type=Path, help="Local contract test hook; bypasses network and writes scored payloads")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(message)s")
    result = process_targets(args)
    print(json.dumps({k: result[k] for k in ["targets_selected", "created", "skipped", "failed"]}, ensure_ascii=False))
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
