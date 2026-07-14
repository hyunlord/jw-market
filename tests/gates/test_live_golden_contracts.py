from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread
from typing import Iterator


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "pipeline" / "scripts" / "gates" / "release_acceptance.py"
TRACKED_CONTRACTS = ROOT / "tests" / "gates" / "chat_backend_live_goldens.json"


def _canonical_sha(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _contract(identifier: str, path: str, payload: object) -> dict[str, object]:
    return {
        "id": identifier,
        "gate_enabled": True,
        "canonical_sha256": _canonical_sha(payload),
        "request": {"method": "GET", "path": path, "headers": {}},
        "truth_basis_status": "confirmed",
        "truth_basis": "fixture owner approval plus independent fixture invariant",
        "measured_at": "2026-07-14T19:00:00+09:00",
        "database": "fixture",
        "build_sha": "fixture-build",
        "runtime_digest": "sha256:fixture",
    }


def _write_tracked_contracts(tmp_path: Path, contracts: list[dict[str, object]]) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    path = repo / "contracts.json"
    path.write_text(
        json.dumps(
            {
                "owner": "fixture",
                "canonicalization": "canonical-json-v1",
                "contracts": contracts,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "contracts.json"], check=True)
    return repo, path


def _run_live(repo: Path, contracts: Path, base_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "live-goldens",
            "--repo-root",
            str(repo),
            "--contracts",
            str(contracts),
            "--base-url",
            base_url,
            "--environment",
            "failure-injection",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


@contextmanager
def _server(payloads: dict[str, object]) -> Iterator[tuple[str, list[str]]]:
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            calls.append(self.path)
            payload = payloads.get(self.path)
            if payload is None:
                self.send_error(404)
                return
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_live_gate_calls_every_enabled_request_and_reports_http_evidence(tmp_path: Path) -> None:
    payloads = {
        "/api/a": {"value": 1},
        "/api/b?scope=full": {"value": 2},
        "/api/unconfirmed": {"value": 3},
    }
    contracts = [
        _contract("a", "/api/a", payloads["/api/a"]),
        _contract("b", "/api/b?scope=full", payloads["/api/b?scope=full"]),
        {
            **_contract("unconfirmed", "/api/unconfirmed", {"value": 3}),
            "gate_enabled": False,
            "canonical_sha256": None,
            "truth_basis_status": "unconfirmed",
            "truth_basis": "No market-owner truth basis has been approved.",
            "last_observed_sha256": _canonical_sha({"value": 3}),
        },
    ]
    repo, registry = _write_tracked_contracts(tmp_path, contracts)

    with _server(payloads) as (base_url, calls):
        result = _run_live(repo, registry, base_url)

    assert result.returncode == 0
    assert calls == ["/api/a", "/api/b?scope=full", "/api/unconfirmed"]
    assert "golden_http=a status=200 bytes=" in result.stdout
    assert "golden_http=b status=200 bytes=" in result.stdout
    assert "golden_observation=unconfirmed status=200 bytes=" in result.stdout
    assert f"actual={_canonical_sha({'value': 3})} expected=unconfirmed" in result.stdout
    assert "gate=live_api_goldens" in result.stdout
    assert "classification=census" in result.stdout
    assert "checked=2" in result.stdout
    assert "population=2" in result.stdout
    assert "tolerance=exact canonical sha256" in result.stdout
    assert "failures=0" in result.stdout
    assert "exit_code=0" in result.stdout


def test_live_gate_wrong_expected_fails_after_calling_endpoint(tmp_path: Path) -> None:
    payload = {"value": 1}
    contract = _contract("wrong", "/api/wrong", payload)
    contract["canonical_sha256"] = "0" * 64
    repo, registry = _write_tracked_contracts(tmp_path, [contract])

    with _server({"/api/wrong": payload}) as (base_url, calls):
        result = _run_live(repo, registry, base_url)

    assert calls == ["/api/wrong"]
    assert result.returncode == 1
    assert "golden_status=wrong matched=false" in result.stdout
    assert f"actual={_canonical_sha(payload)} expected={'0' * 64}" in result.stdout
    assert "failures=1" in result.stdout
    assert "exit_code=1" in result.stdout


def test_live_gate_rejects_zero_enabled_population(tmp_path: Path) -> None:
    contract = {
        **_contract("unconfirmed", "/api/unconfirmed", {"value": 1}),
        "gate_enabled": False,
        "canonical_sha256": None,
        "truth_basis_status": "unconfirmed",
    }
    repo, registry = _write_tracked_contracts(tmp_path, [contract])

    with _server({"/api/unconfirmed": {"value": 1}}) as (base_url, calls):
        result = _run_live(repo, registry, base_url)

    assert calls == ["/api/unconfirmed"]
    assert result.returncode == 1
    assert "empty enabled golden population is a failure" in result.stdout
    assert "checked=0" in result.stdout
    assert "population=0" in result.stdout
    assert "failures=1" in result.stdout


def test_live_gate_rejects_contract_registry_outside_git_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    registry = tmp_path / "untracked-contracts.json"
    registry.write_text('{"contracts": []}', encoding="utf-8")

    result = _run_live(repo, registry, "http://127.0.0.1:1")

    assert result.returncode == 1
    assert "contract registry must be inside the repository" in result.stdout


def test_repository_live_registry_tracks_five_cases_but_gates_only_confirmed_three() -> None:
    document = json.loads(TRACKED_CONTRACTS.read_text(encoding="utf-8"))
    contracts = document["contracts"]

    assert {contract["id"] for contract in contracts} == {
        "brands",
        "market_status",
        "cause_livalo",
        "cause_aktemra",
        "cause_guardlet",
    }
    enabled = [contract for contract in contracts if contract["gate_enabled"]]
    assert [contract["id"] for contract in enabled] == [
        "brands",
        "market_status",
        "cause_livalo",
    ]
    for contract in enabled:
        assert contract["truth_basis_status"] == "confirmed"
        assert len(contract["canonical_sha256"]) == 64
        assert contract["truth_basis"]
        assert contract["measured_at"]
        assert contract["database"]
        assert contract["build_sha"]
        assert contract["runtime_digest"]
    for contract in contracts:
        if not contract["gate_enabled"]:
            assert contract["truth_basis_status"] == "unconfirmed"
            assert contract["canonical_sha256"] is None
            assert len(contract["last_observed_sha256"]) == 64


def test_gate_sources_do_not_accept_tmp_contract_registries() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "pipeline" / "scripts" / "gates").rglob("*")
        if path.is_file() and path.suffix in {".py", ".sh"}
    )

    assert "/tmp" not in sources
