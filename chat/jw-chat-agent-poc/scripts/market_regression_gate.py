"""Market no-regression gate for the chat answer surface.

Why this file exists
--------------------
Rounds up to R12.7c compared market answers against a normalized SHA
(``568a7e3a45d8…``) whose *recipe* lived only inside an audit archive. When that
archive was lost the expected value became unreproducible: ``grep -rl 568a7e3a``
returned nothing, and the gate could no longer be executed at all. The recipe
now lives in the repository, next to a unit test that pins it, so it cannot be
lost with an archive again.

The recipe
----------
1. Ask the deployed backend the canonical market question.
2. Take the ``text`` field of the answer.
3. Extract every numeric token in order of appearance:
   ``\\d[\\d,]*(?:\\.\\d+)?``
4. Join them with ``|`` and take the sha256 hexdigest.

Wording drift and LLM phrasing changes do not move the digest unless a *value*
changes, which is what "market 무회귀" is actually about.

Why the digest alone is not the verdict
---------------------------------------
Measured against an unchanged live build on 2026-08-15/16, five consecutive runs
of the same question produced three different answers:

    mart_records=32  80eea887…  2872 chars   x2
    mart_records=8   11f97bb5…  2620 chars   x1
    mart_records=8   cbc2a717…  2014 chars   x2

The mart lane returns a varying number of records run to run, so *no* whole-text
digest is stable on this system today. Treating a digest change as a regression
would therefore report a retrieval flake as a code regression, and — worse — a
digest match on a small cohort would hide a real one.

So this gate reports digests **grouped by mart record count** and only calls a
cohort stable when it repeats. The retrieval flake itself is tracked separately
(it predates R12.7c and reproduces on the rolled-back build); this script exists
to make it visible, not to hide it.

Flake handling
--------------
A run where the mart lane returned nothing contains no numeric tokens and hashes
to the sha256 of the empty string. Those are reported separately. Every sample's
record count is always printed — a shifting distribution is a finding.

Usage
-----
    python scripts/market_regression_gate.py --target http://127.0.0.1:8080 --runs 3
    python scripts/market_regression_gate.py --target ... --expect <sha256>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request

CANONICAL_QUESTION = "리바로 매출 알려줘"
NUMERIC_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def normalize(answer_text: str) -> str:
    """Ordered numeric tokens joined by '|'. This is the whole recipe."""
    return "|".join(NUMERIC_TOKEN_RE.findall(answer_text))


def digest(answer_text: str) -> str:
    return hashlib.sha256(normalize(answer_text).encode("utf-8")).hexdigest()


def mart_records(trace: object) -> int:
    """records_received for the mart lane, 0 when the lane returned nothing."""
    if isinstance(trace, dict):
        spine = trace.get("lossless_spine")
        if isinstance(spine, dict):
            for evidence in spine.get("evidence_sets") or ():
                if isinstance(evidence, dict) and evidence.get("source") == "mart":
                    coverage = evidence.get("coverage") or {}
                    return int(coverage.get("records_received") or 0)
    return 0


def ask(target: str, question: str, timeout: float) -> dict:
    body = json.dumps({"question": question, "external_mode": "on"}).encode("utf-8")
    request = urllib.request.Request(
        f"{target.rstrip('/')}/chat/answer",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    payload["_elapsed_s"] = round(time.monotonic() - started, 1)
    payload["_status"] = 200
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="backend base URL")
    parser.add_argument("--question", default=CANONICAL_QUESTION)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--expect", default=None, help="expected sha256; omit to establish one")
    args = parser.parse_args()

    samples: list[dict] = []
    for index in range(1, args.runs + 1):
        try:
            payload = ask(args.target, args.question, args.timeout)
        except urllib.error.HTTPError as exc:
            print(f"run {index}: HTTP {exc.code} — {exc.read()[:200]!r}")
            samples.append({"run": index, "status": exc.code, "sha256": None})
            continue
        text = payload.get("text", "")
        sha = digest(text)
        records = mart_records(payload.get("trace"))
        empty = sha == EMPTY_SHA256 or records == 0
        samples.append(
            {
                "run": index,
                "status": 200,
                "elapsed_s": payload["_elapsed_s"],
                "sha256": sha,
                "mart_records": records,
                "text_chars": len(text),
                "flake_empty": empty,
            }
        )
        print(
            f"run {index}: sha256={sha} mart_records={records} "
            f"chars={len(text)} elapsed={payload['_elapsed_s']}s"
            + ("  [FLAKE: no evidence]" if empty else "")
        )

    usable = [s for s in samples if s.get("sha256") and not s.get("flake_empty")]
    flakes = [s for s in samples if s.get("flake_empty")]
    print(f"\nusable samples: {len(usable)}/{len(samples)}   flake (no evidence): {len(flakes)}")

    if not usable:
        print("VERDICT: UNKNOWN — every sample hit the empty-evidence flake")
        return 2

    cohorts: dict[int, list[dict]] = {}
    for sample in usable:
        cohorts.setdefault(sample["mart_records"], []).append(sample)
    print("\ncohorts by mart record count:")
    for records in sorted(cohorts, reverse=True):
        group = cohorts[records]
        for sha in sorted({s["sha256"] for s in group}):
            matching = [s for s in group if s["sha256"] == sha]
            print(
                f"  mart_records={records:<4} {sha}  x{len(matching)}"
                f"  chars={sorted({s['text_chars'] for s in matching})}"
            )

    record_counts = sorted(cohorts)
    if len(record_counts) > 1:
        print(
            f"NOTE: retrieval returned {record_counts} records across runs — "
            "the mart lane is not deterministic; see the flake track."
        )

    if args.expect is None:
        best = max(cohorts)
        digests = sorted({s["sha256"] for s in cohorts[best]})
        if len(digests) == 1 and len(cohorts[best]) > 1:
            print(f"VERDICT: BASELINE {digests[0]} (mart_records={best}, n={len(cohorts[best])})")
            return 0
        print(
            "VERDICT: NO STABLE BASELINE — the richest cohort "
            f"(mart_records={best}) is not reproducible across runs"
        )
        return 1

    matched = [s for s in usable if s["sha256"] == args.expect]
    if matched:
        print(
            f"VERDICT: PASS — {len(matched)}/{len(usable)} usable samples match {args.expect} "
            f"(mart_records={sorted({s['mart_records'] for s in matched})})"
        )
        return 0
    print(f"VERDICT: FAIL — expected {args.expect}, observed {sorted({s['sha256'] for s in usable})}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
