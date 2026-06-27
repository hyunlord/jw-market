#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyyaml",
# ]
# ///

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from stage0_lib.io import load_questions

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jw_chat_agent_poc import ChatAgent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect chat answers for the Stage evaluation set.")
    parser.add_argument("--questions", type=Path, default=Path("eval/stage0_questions.yaml"))
    parser.add_argument("--pl-questions", type=Path, default=Path("eval/pl_questions.yaml"))
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--external-mode", default="fixture")
    return parser


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def main() -> int:
    args = _build_parser().parse_args()
    questions = load_questions(args.questions, args.pl_questions)
    agent = ChatAgent(external_mode=args.external_mode)
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("w", encoding="utf-8") as handle:
        for question in questions:
            try:
                result = agent.answer(question.question)
                payload = {"id": question.question_id, "ok": True, "result": _jsonable(result)}
            except (LookupError, TypeError, ValueError) as exc:
                payload = {
                    "id": question.question_id,
                    "ok": False,
                    "result": {"answer": ""},
                    "error": str(exc),
                }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    print(args.output_jsonl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
