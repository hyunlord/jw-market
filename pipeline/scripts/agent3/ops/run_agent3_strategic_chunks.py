from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import sys

from pipeline.scripts.agent3.db import DbConfig, connect


WORKLIST = Path("/work/worklist_7706.tsv")
CHUNK_DIR = Path("/tmp/agent3_market_chunks")
LEDGER = Path("/tmp/agent3_market_checkpoint.jsonl")
CHUNK_SIZE = 250
MAX_CALLS = 5137


def market_count() -> int:
    with connect(DbConfig.from_env()) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS rows_n FROM agent3_brand_strength_market")
            return int(cursor.fetchone()["rows_n"])


with WORKLIST.open(encoding="utf-8", newline="") as stream:
    reader = csv.DictReader(stream, delimiter="\t")
    fieldnames = list(reader.fieldnames or [])
    rows = list(reader)

if len(rows) != 7706:
    raise SystemExit(f"worklist count mismatch: {len(rows)}")

CHUNK_DIR.mkdir(parents=True, exist_ok=True)
LEDGER.write_text("", encoding="utf-8")
cumulative_calls = 0
cumulative_affected = 0
cumulative_errors = 0

print(
    "[strategic-full-harness] "
    f"commit=3893303804373b36f866d7bf7ed80bd6d782c1e2 units={len(rows)} "
    f"chunk_size={CHUNK_SIZE} max_calls={MAX_CALLS} initial_rows={market_count()}",
    flush=True,
)

for offset in range(0, len(rows), CHUNK_SIZE):
    chunk_no = offset // CHUNK_SIZE + 1
    chunk_rows = rows[offset : offset + CHUNK_SIZE]
    chunk_path = CHUNK_DIR / f"chunk_{chunk_no:03d}.tsv"
    result_path = CHUNK_DIR / f"chunk_{chunk_no:03d}.json"
    with chunk_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(chunk_rows)
    print(
        f"[strategic-full-chunk-start] chunk={chunk_no} offset={offset} units={len(chunk_rows)} "
        f"rows_before={market_count()} cumulative_calls={cumulative_calls}",
        flush=True,
    )
    command = [
        sys.executable,
        "-m",
        "pipeline.scripts.agent3.run_market_source",
        "--worklist",
        str(chunk_path),
        "--mode",
        "full",
        "--expected-workflow-rev",
        "5692",
        "--output",
        str(result_path),
    ]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        print(f"[strategic-full-stop] chunk={chunk_no} reason=runner_rc rc={completed.returncode}", flush=True)
        raise SystemExit(completed.returncode)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    calls = int(result["workflow_calls"])
    errors = int(result["workflow_errors"])
    cumulative_calls += calls
    cumulative_errors += errors
    cumulative_affected += int(result["affected"])
    checkpoint = {
        "chunk": chunk_no,
        "offset": offset,
        "units": len(chunk_rows),
        "source_units": int(result["source_units"]),
        "affected": int(result["affected"]),
        "candidate_units": int(result["candidate_units"]),
        "market_position": int(result["market_position"]),
        "workflow_calls": calls,
        "workflow_errors": errors,
        "skipped_same_hash": int(result["skipped_same_hash"]),
        "skipped_same_content": int(result["skipped_same_content"]),
        "rows_after": market_count(),
        "cumulative_calls": cumulative_calls,
        "cumulative_cost_krw": round(cumulative_calls * 3.39, 2),
        "cumulative_affected": cumulative_affected,
    }
    with LEDGER.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"[strategic-full-checkpoint] {json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)}", flush=True)
    result_path.unlink()
    if errors:
        print(f"[strategic-full-stop] chunk={chunk_no} reason=workflow_errors errors={errors}", flush=True)
        raise SystemExit(20)
    if cumulative_calls > MAX_CALLS:
        print(
            f"[strategic-full-stop] chunk={chunk_no} reason=budget calls={cumulative_calls} "
            f"cost={cumulative_calls * 3.39:.2f}",
            flush=True,
        )
        raise SystemExit(21)

summary = {
    "requested": len(rows),
    "chunks": (len(rows) + CHUNK_SIZE - 1) // CHUNK_SIZE,
    "final_rows": market_count(),
    "workflow_calls": cumulative_calls,
    "workflow_errors": cumulative_errors,
    "cost_krw": round(cumulative_calls * 3.39, 2),
    "affected": cumulative_affected,
}
print(f"[strategic-full-complete] {json.dumps(summary, ensure_ascii=False, sort_keys=True)}", flush=True)
if summary["final_rows"] != len(rows):
    raise SystemExit(f"completion mismatch: {summary}")
