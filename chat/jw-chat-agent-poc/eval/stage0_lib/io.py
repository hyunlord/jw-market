from __future__ import annotations

import json
from pathlib import Path
from typing import assert_never

import yaml

from .model import EvalQuestion, GoldKey, JsonValue, RawResult


def _as_mapping(value: JsonValue) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return value
    msg = "Expected a mapping in YAML/JSON input"
    raise TypeError(msg)


def _as_string(value: JsonValue, field_name: str) -> str:
    if isinstance(value, str):
        return value
    msg = f"Expected string for {field_name}"
    raise TypeError(msg)


def _load_yaml(path: Path) -> dict[str, JsonValue]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if isinstance(loaded, dict):
        return loaded
    msg = f"{path} must contain a YAML mapping"
    raise TypeError(msg)


def load_questions(base_path: Path, pl_path: Path) -> list[EvalQuestion]:
    """Load base questions and optional PL questions."""
    records: list[EvalQuestion] = []
    for path in (base_path, pl_path):
        payload = _load_yaml(path)
        raw_questions = payload.get("questions", [])
        if not isinstance(raw_questions, list):
            msg = f"{path} questions must be a list"
            raise TypeError(msg)
        for raw in raw_questions:
            mapping = _as_mapping(raw)
            keys: list[GoldKey] = []
            raw_keys = mapping.get("gold_keys", [])
            if isinstance(raw_keys, list):
                for raw_key in raw_keys:
                    key_map = _as_mapping(raw_key)
                    keys.append(
                        GoldKey(
                            label=_as_string(key_map.get("label"), "label"),
                            key=_as_string(key_map.get("key"), "key"),
                            kind=_as_string(key_map.get("kind"), "kind"),
                        )
                    )
            records.append(
                EvalQuestion(
                    question_id=_as_string(mapping.get("id"), "id"),
                    category=_as_string(mapping.get("category"), "category"),
                    question=_as_string(mapping.get("question"), "question"),
                    gold_note=_as_string(mapping.get("gold_note"), "gold_note"),
                    expected_behavior=_as_string(
                        mapping.get("expected_behavior"), "expected_behavior"
                    ),
                    gold_keys=tuple(keys),
                )
            )
    return records


def load_raw_results(path: Path) -> dict[str, RawResult]:
    """Load JSON or JSONL raw answer records keyed by question id."""
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    if path.suffix == ".jsonl":
        payloads = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        parsed = json.loads(text)
        match parsed:
            case list():
                payloads = parsed
            case dict():
                payloads = list(parsed.values())
            case unreachable:
                assert_never(unreachable)

    results: dict[str, RawResult] = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        question_id = str(payload.get("id") or payload.get("question_id") or "")
        result = payload.get("result", {})
        if not isinstance(result, dict):
            result = {"answer": str(result)}
        ok = bool(payload.get("ok", True))
        error = payload.get("error")
        results[question_id] = RawResult(
            question_id=question_id,
            ok=ok,
            result=result,
            error=str(error) if error is not None else None,
        )
    return results


def write_json(path: Path, payload: JsonValue) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
