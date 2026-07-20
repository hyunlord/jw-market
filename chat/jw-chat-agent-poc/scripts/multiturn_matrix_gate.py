#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import statistics
import time
from typing import Any, Literal
from urllib.parse import urlencode
from uuid import uuid4

import requests


STREAM_PATH = "/api/v1/market/socket-lab/stream"
FILES_PATH = "/api/v1/market/socket-lab/market/files"
SESSION_DELETE_PATH = "/api/v1/rnd/chat/session/delete"
BRIDGE_UPLOAD_PATH = "/upload"
BRIDGE_COMMIT_PATH = "/commit"
BRIDGE_DOCUMENTS_PATH = "/documents"
BRIDGE_DELETE_PATH = "/documents/delete"
WORKFLOW_ID = 301
FILE_VDB_ID = 139
TEMP_DOCUMENT_LOGICAL_NAME = re.compile(r"^doc_([1-9][0-9]*)(?:_|$)")


@dataclass(frozen=True, order=True)
class DocumentIdentity:
    field: Literal["document_id", "temp_document_id"]
    value: int

    def __post_init__(self) -> None:
        if self.field not in {"document_id", "temp_document_id"}:
            raise ValueError(f"unsupported document identity field: {self.field}")
        if type(self.value) is not int or self.value <= 0:
            raise ValueError("document identity must be a positive integer")

    def as_dict(self) -> dict[str, int | str]:
        return {"field": self.field, "value": self.value}


def make_conversation_id(mode: str, scenario_id: str, repeat: int) -> str:
    mode_code = "b" if mode == "baseline" else "c"
    scenario_code = scenario_id.removeprefix("MT-").lower()
    return f"mt-{mode_code}-{scenario_code}-r{repeat}-{uuid4().hex[:12]}"


def load_matrix(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("multiturn matrix version must be 1")
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 9:
        raise ValueError("multiturn matrix must contain exactly nine scenarios")
    identifiers = [item.get("id") for item in scenarios if isinstance(item, dict)]
    if identifiers != [f"MT-{index:02d}" for index in range(1, 10)]:
        raise ValueError("multiturn scenario IDs must be contiguous MT-01..MT-09")
    repeats = payload.get("repeats")
    if not isinstance(repeats, int) or repeats < 3:
        raise ValueError("multiturn matrix requires at least three repeats")
    for scenario in scenarios:
        turns = scenario.get("turns") if isinstance(scenario, dict) else None
        if not isinstance(turns, list) or not 2 <= len(turns) <= 3:
            raise ValueError(f"invalid turn count for {scenario.get('id') if isinstance(scenario, dict) else 'unknown'}")
        if not all(isinstance(turn, dict) and str(turn.get("question") or "").strip() for turn in turns):
            raise ValueError(f"blank question in {scenario.get('id')}")
    return payload


def parse_sse(raw: str) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
        events.append({"event": name, "data": "\n".join(data_lines)})
    return tuple(events)


def assemble_answer(events: Iterable[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for event in events:
        name = event.get("event")
        data = str(event.get("data") or "")
        if name == "delta":
            parts.append(data)
        elif name == "markdown_block":
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("markdown"), str):
                parts.append(payload["markdown"])
    return "".join(parts)


def latest_json_event(events: Iterable[Mapping[str, Any]], name: str) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for event in events:
        if event.get("event") != name:
            continue
        try:
            payload = json.loads(str(event.get("data") or ""))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            selected = payload
    return selected


def evaluate_turn(turn: Mapping[str, Any], capture: Mapping[str, Any]) -> list[str]:
    expected = turn.get("expected")
    contract = expected if isinstance(expected, Mapping) else {}
    answer = str(capture.get("answer") or "")
    trace = capture.get("trace")
    trace_items = trace if isinstance(trace, Mapping) else {}
    qa = trace_items.get("qa_trace")
    qa_items = qa if isinstance(qa, Mapping) else {}
    conversation = qa_items.get("conversation")
    conversation_items = conversation if isinstance(conversation, Mapping) else {}
    resolved_question = str(conversation_items.get("resolved_question") or "")
    resolved_slots = conversation_items.get("resolved_slots")
    slots = resolved_slots if isinstance(resolved_slots, Mapping) else {}
    final = qa_items.get("final")
    final_items = final if isinstance(final, Mapping) else {}
    failures: list[str] = []

    if not conversation_items:
        failures.append("conversation_trace_missing")
    equals = contract.get("resolved_question_equals")
    if isinstance(equals, str) and resolved_question != equals:
        failures.append(f"resolved_question_not_equal:{equals}")
    for token in _strings(contract.get("resolved_question_contains")):
        if token.casefold() not in resolved_question.casefold():
            failures.append(f"resolved_question_missing:{token}")
    expected_slots = contract.get("slots")
    if isinstance(expected_slots, Mapping):
        for key, choices in expected_slots.items():
            actual = str(slots.get(str(key)) or "")
            if not any(choice.casefold() in actual.casefold() for choice in _strings(choices)):
                failures.append(f"slot_mismatch:{key}:{actual or '<empty>'}")
    for token in _strings(contract.get("answer_contains")):
        if token.casefold() not in answer.casefold():
            failures.append(f"answer_missing:{token}")
    contains_any = _strings(contract.get("answer_contains_any"))
    if contains_any and not any(token.casefold() in answer.casefold() for token in contains_any):
        failures.append("answer_missing_any:" + "|".join(contains_any))
    for token in _strings(contract.get("answer_excludes")):
        if token.casefold() in answer.casefold():
            failures.append(f"answer_forbidden:{token}")
    disposition = str(final_items.get("disposition") or "")
    expected_disposition = contract.get("disposition")
    if isinstance(expected_disposition, str) and disposition != expected_disposition:
        failures.append(f"disposition:{disposition or '<empty>'}")
    disposition_any = _strings(contract.get("disposition_any"))
    if disposition_any and disposition not in disposition_any:
        failures.append(f"disposition_not_allowed:{disposition or '<empty>'}")
    if final_items.get("body_empty") is True or not answer.strip():
        failures.append("body_empty")
    return failures


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item))
    return ()


def _logical_names(row: Mapping[str, Any]) -> list[str]:
    tables = row.get("sql_tables")
    if not isinstance(tables, list):
        return []
    return [
        str(table.get("logical_name"))
        for table in tables
        if isinstance(table, Mapping) and isinstance(table.get("logical_name"), str)
    ]


def _document_identity(row: Mapping[str, Any]) -> DocumentIdentity | None:
    document_id = row.get("document_id")
    if type(document_id) is int and document_id > 0:
        return DocumentIdentity(field="document_id", value=document_id)
    temp_document_id = row.get("temp_document_id")
    if type(temp_document_id) is int and temp_document_id > 0:
        return DocumentIdentity(field="temp_document_id", value=temp_document_id)
    temp_ids = {
        int(match.group(1))
        for logical_name in _logical_names(row)
        for match in [TEMP_DOCUMENT_LOGICAL_NAME.match(logical_name)]
        if match is not None
    }
    if len(temp_ids) > 1:
        raise RuntimeError(
            "file bridge document has ambiguous temp document identity: "
            + ",".join(str(value) for value in sorted(temp_ids))
        )
    if temp_ids:
        return DocumentIdentity(field="temp_document_id", value=next(iter(temp_ids)))
    return None


def _bridge_payload_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    temp_documents = payload.get("temp_documents")
    return {
        "keys": sorted(str(key) for key in payload),
        "error_count": len(_strings(payload.get("errors"))),
        "temp_document_count": len(temp_documents) if isinstance(temp_documents, list) else 0,
    }


def _bridge_commit_evidence(commit: Mapping[str, Any]) -> dict[str, Any]:
    rows = commit.get("documents")
    documents = rows if isinstance(rows, list) else []
    return {
        "keys": sorted(str(key) for key in commit),
        "error_count": len(_strings(commit.get("errors"))),
        "file_only_ready": commit.get("file_only_ready") is True,
        "documents": [
            {
                "file_name": row.get("file_name"),
                "status": row.get("status"),
                "route": row.get("route"),
                "chunk_count": row.get("chunk_count"),
                "logical_names": _logical_names(row),
            }
            for row in documents
            if isinstance(row, Mapping)
        ],
    }


def portal_tokens(auth_url: str, timeout_s: float) -> tuple[str, str]:
    response = requests.post(auth_url, json={}, timeout=timeout_s)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    token_source = data if isinstance(data, dict) else payload
    if not isinstance(token_source, dict):
        raise RuntimeError("portal login response is not an object")
    portal_token = token_source.get("portal_token") or token_source.get("portalToken")
    access_token = token_source.get("access_token") or token_source.get("accessToken")
    if not isinstance(portal_token, str) or not portal_token.strip():
        raise RuntimeError("portal login response has no portalToken")
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError("portal login response has no accessToken")
    return portal_token.strip(), access_token.strip()


class PortalClient:
    def __init__(
        self,
        *,
        stream_url: str,
        portal_base: str,
        timeout_s: float,
        portal_token: str,
        access_token: str,
        file_bridge_base: str = "",
    ) -> None:
        self.stream_url = stream_url
        self.portal_base = portal_base.rstrip("/")
        self.file_bridge_base = file_bridge_base.rstrip("/")
        self.timeout_s = timeout_s
        self.portal_token = portal_token
        self.access_token = access_token
        self._upload_evidence: dict[str, dict[str, Any]] = {}

    @property
    def auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.portal_token:
            headers["Authorization"] = f"Bearer {self.portal_token}"
        if self.access_token:
            headers["Authorization-Access-Token"] = self.access_token
        return headers

    def capture(self, question: str, conversation_id: str) -> dict[str, Any]:
        started = time.monotonic()
        response = requests.post(
            self.stream_url,
            json={"question": question, "conversationId": conversation_id},
            headers={"Accept": "text/event-stream", **self.auth_headers},
            timeout=self.timeout_s,
        )
        elapsed_s = round(time.monotonic() - started, 3)
        response.raise_for_status()
        raw = response.content.decode("utf-8")
        events = parse_sse(raw)
        return {
            "answer": assemble_answer(events),
            "trace": latest_json_event(events, "trace"),
            "timing": latest_json_event(events, "timing"),
            "elapsed_s": elapsed_s,
            "raw": raw,
        }

    def upload(self, conversation_id: str, fixture: Path) -> list[DocumentIdentity]:
        if self.file_bridge_base:
            return self._bridge_upload(conversation_id, fixture)
        self._upload_evidence[conversation_id] = {"fixture": fixture.name, "path": "portal"}
        with fixture.open("rb") as handle:
            response = requests.post(
                self.portal_base + FILES_PATH,
                params={"sessionId": conversation_id},
                headers=self.auth_headers,
                files=[("files", (fixture.name, handle))],
                timeout=max(self.timeout_s, 1200),
            )
        response.raise_for_status()
        deadline = time.monotonic() + max(self.timeout_s, 1200)
        while time.monotonic() < deadline:
            rows = self.documents(conversation_id)
            matches = [
                row for row in rows
                if row.get("file_name") == fixture.name
                and (int(row.get("chunk_count") or 0) > 0 or bool(row.get("sql_tables")))
            ]
            identifiers = [
                DocumentIdentity(field="document_id", value=int(row["document_id"]))
                for row in matches
                if type(row.get("document_id")) is int and int(row["document_id"]) > 0
            ]
            if identifiers:
                self._upload_evidence[conversation_id]["identities"] = [
                    identity.as_dict() for identity in identifiers
                ]
                return identifiers
            time.sleep(2)
        raise TimeoutError("uploaded fixture did not become queryable")

    def _bridge_upload(self, conversation_id: str, fixture: Path) -> list[DocumentIdentity]:
        evidence: dict[str, Any] = {"fixture": fixture.name}
        self._upload_evidence[conversation_id] = evidence
        with fixture.open("rb") as handle:
            response = requests.post(
                self.file_bridge_base + BRIDGE_UPLOAD_PATH,
                data={
                    "workflow_id": str(WORKFLOW_ID),
                    "app_session_id": conversation_id,
                    "vdb_id": str(FILE_VDB_ID),
                    "return_when": "complete",
                },
                files=[("files", (fixture.name, handle))],
                timeout=max(self.timeout_s, 1200),
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("file bridge upload response is not an object")
        evidence["upload"] = _bridge_payload_evidence(payload)
        errors = _strings(payload.get("errors"))
        if errors:
            raise RuntimeError("file bridge upload failed: " + "; ".join(errors))
        commit = payload.get("commit")
        if not isinstance(commit, Mapping):
            temp_documents = payload.get("temp_documents")
            if not isinstance(temp_documents, list) or not temp_documents:
                raise RuntimeError("file bridge upload returned no commit or temp documents")
            commit_response = requests.post(
                self.file_bridge_base + BRIDGE_COMMIT_PATH,
                json={
                    "workflow_id": WORKFLOW_ID,
                    "vdb_id": FILE_VDB_ID,
                    "app_session_id": conversation_id,
                    "temp_documents": temp_documents,
                },
                timeout=max(self.timeout_s, 1200),
            )
            commit_response.raise_for_status()
            commit = commit_response.json()
        if not isinstance(commit, Mapping):
            raise RuntimeError("file bridge commit response is not an object")
        evidence["commit"] = _bridge_commit_evidence(commit)
        commit_errors = _strings(commit.get("errors"))
        if commit_errors:
            raise RuntimeError("file bridge commit failed: " + "; ".join(commit_errors))
        if commit.get("file_only_ready") is not True:
            raise RuntimeError("file bridge commit did not become queryable")
        rows = commit.get("documents")
        documents = rows if isinstance(rows, list) else []
        identifiers: list[DocumentIdentity] = []
        for row in documents:
            if not isinstance(row, Mapping):
                continue
            identity = _document_identity(row)
            if identity is None:
                raise RuntimeError("file bridge commit returned no deletable document identity")
            identifiers.append(identity)
        if not identifiers:
            raise RuntimeError("file bridge commit returned no deletable document identity")
        identities = sorted(set(identifiers))
        evidence["identities"] = [identity.as_dict() for identity in identities]
        return identities

    def upload_evidence(self, conversation_id: str) -> dict[str, Any]:
        evidence = self._upload_evidence.get(conversation_id, {})
        return json.loads(json.dumps(evidence, ensure_ascii=False))

    def documents(self, conversation_id: str) -> list[dict[str, Any]]:
        if self.file_bridge_base:
            response = requests.get(
                self.file_bridge_base + BRIDGE_DOCUMENTS_PATH,
                params={
                    "workflow_id": WORKFLOW_ID,
                    "app_session_id": conversation_id,
                    "vdb_id": FILE_VDB_ID,
                },
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("documents") if isinstance(payload, Mapping) else None
            return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
        response = requests.get(
            self.portal_base + FILES_PATH,
            params={"sessionId": conversation_id},
            headers=self.auth_headers,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("documents") if isinstance(payload, dict) else None
        return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def cleanup(
        self,
        conversation_id: str,
        document_ids: Iterable[DocumentIdentity],
    ) -> list[str]:
        failures: list[str] = []
        if self.file_bridge_base:
            owned_ids = {value for value in document_ids if isinstance(value, DocumentIdentity)}
            try:
                for row in self.documents(conversation_id):
                    try:
                        identity = _document_identity(row)
                    except RuntimeError as exc:
                        failures.append(f"bridge_document_identity:{exc}")
                        continue
                    if identity is None:
                        failures.append(
                            f"bridge_document_identity_missing:{row.get('file_name') or '<unknown>'}"
                        )
                        continue
                    owned_ids.add(identity)
            except Exception as exc:
                failures.append(f"bridge_documents:{type(exc).__name__}:{exc}")
            for identity in sorted(owned_ids):
                delete_payload: dict[str, Any] = {
                    "workflow_id": WORKFLOW_ID,
                    "vdb_id": FILE_VDB_ID,
                    "app_session_id": conversation_id,
                    identity.field: identity.value,
                }
                response = requests.post(
                    self.file_bridge_base + BRIDGE_DELETE_PATH,
                    json=delete_payload,
                    timeout=60,
                )
                if response.status_code >= 400:
                    failures.append(
                        f"document_delete_http_{response.status_code}:{identity.field}:{identity.value}"
                    )
                    continue
                payload = response.json()
                status = payload.get("status") if isinstance(payload, Mapping) else None
                errors = _strings(payload.get("errors")) if isinstance(payload, Mapping) else ()
                if errors or status not in {"deleted", "already_deleted", "not_found"}:
                    failures.append(
                        f"document_delete_failed:{identity.field}:{identity.value}:{status or '<empty>'}"
                    )
            try:
                residuals = self.documents(conversation_id)
                if residuals:
                    failures.append(f"document_residual:{len(residuals)}")
            except Exception as exc:
                failures.append(f"bridge_residual_check:{type(exc).__name__}:{exc}")
        else:
            for identity in document_ids:
                response = requests.delete(
                    f"{self.portal_base}{FILES_PATH}/{identity.value}",
                    params={"sessionId": conversation_id},
                    headers=self.auth_headers,
                    timeout=60,
                )
                if response.status_code >= 400:
                    failures.append(f"document_delete_http_{response.status_code}:{document_id}")
        response = requests.put(
            self.portal_base + SESSION_DELETE_PATH,
            headers={"Content-Type": "application/json", **self.auth_headers},
            json={"uid": conversation_id},
            timeout=60,
        )
        if response.status_code >= 400:
            failures.append(f"session_delete_http_{response.status_code}")
        return failures


def run_matrix(
    matrix: Mapping[str, Any],
    client: PortalClient,
    output: Path,
    *,
    repeats: int,
    mode: str,
    fixture: Path | None,
    expected_commit: str,
    expected_digest: str,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    raw_dir = output / "raw"
    raw_dir.mkdir(exist_ok=True)
    runs: list[dict[str, Any]] = []
    cleanup_failures: list[str] = []
    capture_failures: list[str] = []
    for repeat in range(1, repeats + 1):
        for scenario in matrix["scenarios"]:
            scenario_id = str(scenario["id"])
            conversation_id = make_conversation_id(mode, scenario_id, repeat)
            document_ids: list[DocumentIdentity] = []
            setup_evidence: dict[str, Any] = {}
            turn_rows: list[dict[str, Any]] = []
            can_capture = True
            try:
                if scenario.get("file_fixture") is True:
                    if fixture is None:
                        capture_failures.append(f"{scenario_id}/r{repeat}:file_fixture_missing")
                        can_capture = False
                    else:
                        try:
                            document_ids = client.upload(conversation_id, fixture)
                        except Exception as exc:
                            capture_failures.append(
                                f"{scenario_id}/r{repeat}:upload:{type(exc).__name__}:{exc}"
                            )
                            can_capture = False
                        finally:
                            evidence_reader = getattr(client, "upload_evidence", None)
                            if callable(evidence_reader):
                                setup_evidence = evidence_reader(conversation_id)
                if can_capture:
                    for turn_index, turn in enumerate(scenario["turns"], start=1):
                        key = f"{scenario_id}_r{repeat}_t{turn_index}"
                        try:
                            capture = client.capture(str(turn["question"]), conversation_id)
                        except Exception as exc:
                            capture_failures.append(f"{key}:{type(exc).__name__}:{exc}")
                            break
                        raw = str(capture.pop("raw"))
                        (raw_dir / f"{key}.sse").write_text(raw, encoding="utf-8")
                        failures = evaluate_turn(turn, capture)
                        trace = capture.get("trace")
                        version = trace.get("version") if isinstance(trace, Mapping) else None
                        version_items = version if isinstance(version, Mapping) else {}
                        if expected_commit and version_items.get("git_sha") != expected_commit:
                            failures.append("commit_mismatch")
                        if expected_digest and version_items.get("image_digest") != expected_digest:
                            failures.append("digest_mismatch")
                        row = {
                            "key": key,
                            "question": turn["question"],
                            "answer": capture["answer"],
                            "elapsed_s": capture["elapsed_s"],
                            "trace": trace,
                            "timing": capture.get("timing"),
                            "failures": failures,
                        }
                        turn_rows.append(row)
                        (output / f"{key}.json").write_text(
                            json.dumps(row, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
            finally:
                cleanup_failures.extend(
                    f"{scenario_id}/r{repeat}:{item}"
                    for item in client.cleanup(conversation_id, document_ids)
                )
            runs.append(
                {
                    "scenario_id": scenario_id,
                    "scenario_name": scenario["name"],
                    "repeat": repeat,
                    "conversation_id": conversation_id,
                    "setup_evidence": setup_evidence,
                    "turns": turn_rows,
                }
            )
    scenario_summary = _scenario_summary(runs, matrix)
    failures = [
        f"{turn['key']}:{failure}"
        for run in runs
        for turn in run["turns"]
        for failure in turn["failures"]
    ]
    speed_failures = [
        item["scenario_id"] for item in scenario_summary
        if item["followup_not_slower"] is False
    ]
    accuracy_passed = not failures and not capture_failures and not cleanup_failures
    speed_target_passed = not speed_failures
    summary = {
        "captured_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "repeats": repeats,
        "scenario_count": len(matrix["scenarios"]),
        "run_count": len(runs),
        "turn_count": sum(len(run["turns"]) for run in runs),
        "runs": runs,
        "scenario_summary": scenario_summary,
        "contract_failures": failures,
        "speed_failures": speed_failures,
        "capture_failures": capture_failures,
        "cleanup_failures": cleanup_failures,
        "accuracy_passed": accuracy_passed,
        "speed_target_passed": speed_target_passed,
        "passed": accuracy_passed,
    }
    (output / "multiturn_matrix_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _scenario_summary(runs: list[dict[str, Any]], matrix: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scenario in matrix["scenarios"]:
        scenario_runs = [run for run in runs if run["scenario_id"] == scenario["id"]]
        turn_count = len(scenario["turns"])
        medians: list[float | None] = []
        for index in range(turn_count):
            elapsed = [float(run["turns"][index]["elapsed_s"]) for run in scenario_runs if len(run["turns"]) > index]
            medians.append(round(statistics.median(elapsed), 3) if elapsed else None)
        first = medians[0] if medians else None
        followups = [value for value in medians[1:] if isinstance(value, float)]
        rows.append(
            {
                "scenario_id": scenario["id"],
                "turn_median_s": medians,
                "followup_not_slower": (
                    isinstance(first, float)
                    and len(followups) == turn_count - 1
                    and all(value <= first for value in followups)
                ),
                "contract_failure_count": sum(
                    len(turn["failures"])
                    for run in scenario_runs
                    for turn in run["turns"]
                ),
            }
        )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the permanent nine-scenario portal-equivalent multiturn gate")
    parser.add_argument("--matrix", type=Path, default=Path("tests/fixtures/multiturn_matrix.json"))
    parser.add_argument("--stream-url", required=True)
    parser.add_argument("--portal-base", required=True)
    parser.add_argument("--file-bridge-base", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--expected-commit", default="")
    parser.add_argument("--expected-digest", default="")
    parser.add_argument("--timeout-s", type=float, default=360.0)
    parser.add_argument("--auth-url", default="")
    parser.add_argument("--portal-token", default="")
    parser.add_argument("--access-token", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    matrix = load_matrix(args.matrix)
    repeats = args.repeats or int(matrix["repeats"])
    if repeats < 3:
        raise SystemExit("at least three repeats are required")
    if args.fixture is not None and not args.fixture.is_file():
        raise SystemExit(f"file fixture not found: {args.fixture}")
    portal_token = args.portal_token
    access_token = args.access_token
    if args.auth_url:
        portal_token, access_token = portal_tokens(args.auth_url, min(args.timeout_s, 60.0))
    client = PortalClient(
        stream_url=args.stream_url,
        portal_base=args.portal_base,
        file_bridge_base=args.file_bridge_base,
        timeout_s=args.timeout_s,
        portal_token=portal_token,
        access_token=access_token,
    )
    summary = run_matrix(
        matrix,
        client,
        args.output,
        repeats=repeats,
        mode=args.mode,
        fixture=args.fixture,
        expected_commit=args.expected_commit,
        expected_digest=args.expected_digest,
    )
    print(json.dumps({key: summary[key] for key in ("mode", "run_count", "turn_count", "passed")}, ensure_ascii=False))
    return 0 if args.mode == "baseline" or summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
