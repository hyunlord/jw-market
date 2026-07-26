from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from scripts.f21_probe.models import load_question_set
from scripts.f21_probe.runner import run_probe
from scripts.f21_probe.types import RunOptions, TargetIdentity


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUESTION_SET = PROJECT_ROOT / "eval" / "f21_probe_questions.v1.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture raw chat responses in F21-compatible format."
    )
    parser.add_argument(
        "--question-set",
        type=Path,
        default=DEFAULT_QUESTION_SET,
        help="Versioned JSON question set.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("F21_PROBE_BASE_URL"),
        required=os.getenv("F21_PROBE_BASE_URL") is None,
    )
    parser.add_argument(
        "--stream-path",
        default=os.getenv(
            "F21_PROBE_STREAM_PATH",
            "/api/v1/market/socket-lab/stream",
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--target-commit",
        default=os.getenv("F21_PROBE_TARGET_COMMIT"),
        required=os.getenv("F21_PROBE_TARGET_COMMIT") is None,
    )
    parser.add_argument(
        "--target-generation",
        default=os.getenv("F21_PROBE_TARGET_GENERATION"),
        required=os.getenv("F21_PROBE_TARGET_GENERATION") is None,
    )
    parser.add_argument(
        "--target-digest",
        default=os.getenv("F21_PROBE_TARGET_DIGEST"),
        required=os.getenv("F21_PROBE_TARGET_DIGEST") is None,
    )
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=360.0)
    parser.add_argument(
        "--header-env",
        action="append",
        default=[],
        metavar="HEADER=ENV_VAR",
        help="Read an HTTP header value from an environment variable.",
    )
    parser.add_argument(
        "--cleanup-url",
        default=os.getenv("F21_PROBE_CLEANUP_URL"),
        help="Optional session cleanup endpoint; disabled when omitted.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.concurrency <= 4:
        raise ValueError("--concurrency must be between 1 and 4")
    if args.interval_seconds < 0:
        raise ValueError("--interval-seconds must be non-negative")
    if args.request_timeout_seconds <= 0:
        raise ValueError("--request-timeout-seconds must be positive")

    headers, header_sources = _header_environment(args.header_env)
    question_set = load_question_set(args.question_set)
    options = RunOptions(
        base_url=args.base_url,
        stream_path=args.stream_path,
        output=args.output,
        question_set_path=args.question_set,
        target=TargetIdentity(
            commit=args.target_commit,
            generation=args.target_generation,
            digest=args.target_digest,
        ),
        headers=headers,
        header_sources=header_sources,
        concurrency=args.concurrency,
        interval_seconds=args.interval_seconds,
        request_timeout_seconds=args.request_timeout_seconds,
        cleanup_url=args.cleanup_url,
    )
    return run_probe(question_set, options)


def _header_environment(items: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    headers: dict[str, str] = {}
    sources: dict[str, str] = {}
    for item in items:
        header, separator, environment_name = item.partition("=")
        if not separator or not header.strip() or not environment_name.strip():
            raise ValueError("--header-env values must use HEADER=ENV_VAR")
        environment_name = environment_name.strip()
        if environment_name not in os.environ:
            raise ValueError(f"missing header environment variable: {environment_name}")
        header = header.strip()
        headers[header] = os.environ[environment_name]
        sources[header] = environment_name
    return headers, sources


if __name__ == "__main__":
    raise SystemExit(main())
