from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from .backfill import (
    BackfillManifest,
    build_backfill_manifest,
    compare_manifest_state,
)
from .backfill_runner import run_backfill_sequentially
from .contract import HiraWorkflowInput
from .http_client import (
    LIST_SLOW_RESPONSE_SECONDS,
    HiraHttpClient,
    HiraRequestPolicy,
)
from .pagination import fetch_notice_index
from .stage_cli import monitored_user_agent

DEFAULT_TASK_QUEUE = "jw-market-hira-benefit-v1"


def _policy_from_args(args: argparse.Namespace) -> HiraRequestPolicy:
    return HiraRequestPolicy(
        delay_after_response_seconds=args.delay_seconds,
        request_jitter_seconds=args.jitter_seconds,
        maximum_attempts=args.maximum_attempts,
        circuit_failure_limit=args.circuit_failure_limit,
        circuit_pause_seconds=args.circuit_pause_seconds,
    )


def _prepare(args: argparse.Namespace) -> int:
    client = HiraHttpClient(
        policy=replace(
            _policy_from_args(args),
            slow_response_seconds=LIST_SLOW_RESPONSE_SECONDS,
        ),
        user_agent=monitored_user_agent(os.environ.get("HIRA_USER_AGENT")),
    )
    index = fetch_notice_index(
        index_url=args.index_url,
        base_url=args.base_url,
        fetch_form=client.post_form_text,
    )
    manifest = build_backfill_manifest(index.items, chunk_size=args.chunk_size)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(manifest.to_json(), encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "hira_backfill_manifest_ready",
                "manifest_path": str(args.manifest),
                "manifest_sha256": manifest.manifest_sha256,
                "total_count": manifest.total_count,
                "chunk_count": manifest.chunk_count,
                "chunk_sizes": [len(chunk.items) for chunk in manifest.chunks],
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    from temporalio.client import Client
    from temporalio.common import WorkflowIDReusePolicy
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.worker import Worker

    from .temporal_workflow import (
        WORKFLOW_NAME,
        HiraBenefitDailyWorkflow,
        run_hira_benefit_stage,
    )

    manifest = BackfillManifest.from_json(args.manifest.read_text(encoding="utf-8"))
    if manifest.total_count != args.expected_total:
        raise RuntimeError(
            f"manifest total changed: {manifest.total_count}!={args.expected_total}"
        )
    base_config = HiraWorkflowInput(
        run_id="replaced-by-backfill-runner",
        state_root=str(args.state_root),
        repo_root=str(args.repo_root),
        first_run_mode="backfill_all",
        manifest_path=str(args.manifest),
        manifest_sha256=manifest.manifest_sha256,
        chunk_index=0,
        chunk_size=manifest.chunk_size,
        request_policy=_policy_from_args(args),
        workflow_timeout_seconds=args.workflow_timeout_seconds,
    )
    client = await Client.connect(args.temporal_address, namespace=args.namespace)

    async def execute(
        config: HiraWorkflowInput,
        workflow_id: str,
    ) -> dict[str, object]:
        print(
            json.dumps(
                {
                    "event": "hira_backfill_chunk_start",
                    "chunk_index": config.chunk_index,
                    "workflow_id": workflow_id,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        try:
            result = await client.execute_workflow(
                WORKFLOW_NAME,
                config,
                id=workflow_id,
                task_queue=args.task_queue,
                execution_timeout=timedelta(
                    seconds=config.workflow_timeout_seconds
                ),
                # Completed chunks attach to their durable result; failed chunks may
                # restart under the same manifest-derived identity.
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except WorkflowAlreadyStartedError:
            result = await client.get_workflow_handle(workflow_id).result()
        if not isinstance(result, dict):
            raise RuntimeError(
                f"workflow {workflow_id} returned {type(result).__name__}"
            )
        print(
            json.dumps(
                {
                    "event": "hira_backfill_chunk_complete",
                    "chunk_index": config.chunk_index,
                    "workflow_id": workflow_id,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return result

    worker = Worker(
        client,
        task_queue=args.task_queue,
        workflows=[HiraBenefitDailyWorkflow],
        activities=[run_hira_benefit_stage],
    )
    async with worker:
        progress = await run_backfill_sequentially(
            manifest=manifest,
            base_config=base_config,
            progress_path=args.progress,
            execute=execute,
        )
    from .repository import connect_from_env, load_notice_state

    connection = connect_from_env()
    try:
        stored = load_notice_state(connection)
    finally:
        connection.rollback()
        connection.close()
    final_gate = compare_manifest_state(manifest, stored)
    final_gate_payload = {
        **asdict(final_gate),
        "passed": final_gate.passed,
        "manifest_sha256": manifest.manifest_sha256,
    }
    args.final_gate.parent.mkdir(parents=True, exist_ok=True)
    args.final_gate.write_text(
        json.dumps(
            final_gate_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if not final_gate.passed:
        raise RuntimeError(
            "final manifest identity gate failed: "
            f"matched={final_gate.matched_count}/{final_gate.expected_count} "
            f"missing={len(final_gate.missing_ids)} "
            f"hash_mismatch={len(final_gate.hash_mismatch_ids)}"
        )
    print(
        json.dumps(
            {
                "event": "hira_backfill_complete",
                **asdict(progress),
                "parsed_count": progress.parsed_count,
                "partial_count": progress.partial_count,
                "failed_count": progress.failed_count,
                "final_gate": final_gate_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def _add_policy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    parser.add_argument("--jitter-seconds", type=float, default=0.5)
    parser.add_argument("--maximum-attempts", type=int, default=3)
    parser.add_argument("--circuit-failure-limit", type=int, default=3)
    parser.add_argument("--circuit-pause-seconds", type=int, default=1800)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--chunk-size", type=int, default=500)
    prepare.add_argument(
        "--index-url",
        default=(
            "https://www.hira.or.kr/rc/insu/insuadtcrtr/"
            "InsuAdtCrtrList.do"
        ),
    )
    prepare.add_argument("--base-url", default="https://www.hira.or.kr")
    _add_policy_args(prepare)

    run = commands.add_parser("run")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--progress", type=Path, required=True)
    run.add_argument("--final-gate", type=Path, required=True)
    run.add_argument("--state-root", type=Path, required=True)
    run.add_argument("--repo-root", type=Path, required=True)
    run.add_argument("--expected-total", type=int, default=4577)
    run.add_argument("--workflow-timeout-seconds", type=int, default=3600)
    run.add_argument(
        "--temporal-address",
        default=os.environ.get(
            "TEMPORAL_ADDRESS",
            "temporal-frontend.temporal.svc:7233",
        ),
    )
    run.add_argument(
        "--namespace",
        default=os.environ.get("TEMPORAL_NAMESPACE", "default"),
    )
    run.add_argument(
        "--task-queue",
        default=os.environ.get("HIRA_TEMPORAL_TASK_QUEUE", DEFAULT_TASK_QUEUE),
    )
    _add_policy_args(run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        return _prepare(args)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
