"""Trigger service: webhook receiver + ledger status API (D-1 option (a)).

Deliberately NOT part of jw-market-backend-api — ingest load/failure must never
share a process, pod, or endpoint with serving (STOP ①). Runs from the same
pipeline-orchestrator image (fastapi/uvicorn/PyMySQL already included):

    uvicorn --factory pipeline.scripts.ingest_hook.app:build --port 8080

Endpoints (the site's whole contract surface):
  POST /ingest/webhook   {"manifest_path": "<path under INGEST_INPUT_ROOT>"}
  GET  /ingest/status    ?epoch=&category=&manifest_sha=
  POST /ingest/reconcile  (promote queued submissions; sweep/ops helper)
  GET  /healthz
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from pipeline.scripts.ingest_hook import config, job_launcher
from pipeline.scripts.ingest_hook.contract import ContractError, load_manifest
from pipeline.scripts.ingest_hook.ledger import Ledger


class WebhookPayload(BaseModel):
    manifest_path: str


class IngestService:
    def __init__(self, ledger: Ledger, input_root: Path, transport=None):
        self.ledger = ledger
        self.input_root = input_root
        self.transport = transport

    # -- promotion: one running Job per category, FIFO within a category ----
    def promote(self, category: str) -> str | None:
        if self.ledger.running_in_category(category) > 0:
            return None
        entry = self.ledger.next_queued(category)
        if entry is None:
            return None
        name = job_launcher.submit_job(
            category=category,
            manifest_sha=entry.manifest_sha,
            manifest_path=entry.manifest_path,
            transport=self.transport,
        )
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        self.ledger.mark_running(entry.epoch, category, entry.manifest_sha, job_name=name, run_id=run_id)
        return name

    def receive_webhook(self, manifest_path: str) -> dict:
        path = (self.input_root / manifest_path).resolve() if not Path(manifest_path).is_absolute() else Path(manifest_path).resolve()
        root = self.input_root.resolve()
        if root not in path.parents and path != root:
            raise HTTPException(status_code=400, detail="manifest_path escapes the input root")
        try:
            manifest = load_manifest(path)
        except ContractError as exc:
            raise HTTPException(status_code=422, detail=f"contract violation: {exc}") from exc
        if not manifest.complete:
            raise HTTPException(status_code=409, detail="manifest is not marked complete; webhook is submit-confirm only")

        decision = self.ledger.receive(
            manifest.epoch, manifest.category, manifest.manifest_sha, manifest_path=str(path)
        )
        launched = self.promote(manifest.category) if decision.action == "queued" else None
        return {
            "epoch": manifest.epoch,
            "category": manifest.category,
            "manifest_sha": manifest.manifest_sha,
            "decision": decision.action,
            "status": decision.status,
            "reason": decision.reason,
            "job_name": launched,
        }


def create_app(service: IngestService) -> FastAPI:
    app = FastAPI(title="jw-ingest-hook", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/ingest/webhook")
    def webhook(payload: WebhookPayload) -> dict:
        return service.receive_webhook(payload.manifest_path)

    @app.get("/ingest/status")
    def status(epoch: str, category: str, manifest_sha: str) -> dict:
        entry = service.ledger.status(epoch, category, manifest_sha)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown submission identity")
        return {
            "epoch": entry.epoch,
            "category": entry.category,
            "manifest_sha": entry.manifest_sha,
            "status": entry.status,
            "reason": entry.reason,
            "job_name": entry.job_name,
            "received_at": entry.received_at,
            "finished_at": entry.finished_at,
        }

    @app.post("/ingest/reconcile")
    def reconcile() -> dict:
        launched = {
            category: name
            for category in service.ledger.queued_categories()
            if (name := service.promote(category)) is not None
        }
        return {"launched": launched}

    return app


def build() -> FastAPI:
    """uvicorn --factory entrypoint (production wiring from env)."""
    # sqlite ledgers self-create; the mysql ingest_ledger DDL is applied
    # manually at activation (PL gate) — never implicitly from service boot.
    ledger = config.open_configured_ledger()
    return create_app(IngestService(ledger, config.input_root()))
