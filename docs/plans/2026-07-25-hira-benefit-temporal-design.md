# HIRA Benefit Criteria Temporal Batch Design

## Decision

Implement a separate HIRA workflow package under
`pipeline/scripts/crawler/hira_benefit`. Reuse the validated news-crawler
orchestration concepts, but do not import or modify news crawler modules.
Do not create a Temporal schedule or deployment manifest in this change.

Deployment is blocked until the 2026-07-26 03:10 production news crawl proves
the timeout, descendant cleanup, and orphan-writer protections introduced by
commit `97bcf76c`. That commit is not an ancestor of the current `develop`
base, so its canonical integration is also a deployment prerequisite.

## Workflow

The workflow runs four activities in order:

1. `discover_changes`
2. `collect_details`
3. `persist_results`
4. `verify_run`

Each activity runs a child command in a new process group. Thirty seconds
without stdout emits a heartbeat. Cancellation or timeout sends `SIGTERM` to
the process group, waits ten seconds, then sends `SIGKILL`.

Subprocess failures without a completed gate receipt remain retryable for the
activities whose Temporal policy allows retries. A written receipt with an
explicit four-condition gate failure is non-retryable. `persist_results`
revalidates the collect receipt before opening its write transaction, so a
manual stage invocation cannot advance state from failed or partial collection
output. Repeating a successful persist with the same `run_id` is idempotent.

The HIRA four-condition gate is:

- `exit_code == 0`
- `failures == 0`
- `identity_gap == 0`
- `pending_gap == 0`

`PARTIAL` and `FAILED` parses are not silently discarded. They are persisted
with raw text. A configurable rolling FAILED-rate alert is separate from the
data gate.

## Timeout Budget

F16 observed list median 0.55 seconds and detail median 0.16 seconds. The
client adds a 0.50-second politeness delay. For a bounded first run of 500
details, the expected network time is approximately:

`0.55 + 500 * (0.16 + 0.50) = 330.55 seconds`

The implementation budgets 350 seconds, requires at least 3x workflow margin,
sets detail collection to 30 minutes, and sets the workflow to 60 minutes.
This is HIRA-specific and does not copy the news 8-hour/18-hour values.

## Change Detection

`brdBltNo` is the source identity, not a trusted watermark. Correctness uses a
SHA-256 fingerprint of:

- `brdBltNo`
- normalized title
- notice date
- canonical detail URL

An unseen ID is new. A known ID with a changed fingerprint is changed. An
identical fingerprint is skipped. This detects edits and lower-numbered late
registrations without assuming monotonic IDs.

The successful state is stored in `hira_benefit_crawl_state`; per-run metrics
are stored in `hira_benefit_crawl_run`; run artifacts remain in durable
Temporal receipt storage. State advances in the same transaction as notice
and brand-link rows.

No implicit first-run behavior exists. Deployment must explicitly select:

- `backfill_all`, or
- `recent_n` with a positive limit.

The proposed shadow default is `recent_n=500`. A full backfill remains a
separate PL decision.

## Parsing

The parser first extracts structured sections by semantic headings:

- target: 투여대상, 급여대상, 대상환자, 인정기준
- exclusion: 제외기준, 투여제외, 급여제외
- dose: 투여용량, 용량제한, 용법 and 용량, 투여기간

Status is:

- `OK`: all three fields found
- `PARTIAL`: one or two fields found
- `FAILED`: none found

Every status preserves normalized raw text, raw HTML SHA-256, and
`failed_fields`. Missing values remain `NULL`.

## Alerting

No external receiver exists in the current news crawler; it exposes durable
failure receipts and status only. The HIRA implementation therefore provides:

- recent N successful-run aggregate `FAILED / parsed`
- configurable N
- configurable threshold with no default threshold
- durable `ALERT` log and receipt
- optional `HIRA_ALERT_WEBHOOK_URL` best-effort delivery

The threshold and final receiver are intentionally deferred to the validation
round with jw chat.

## Storage

The HIRA namespace is separate from UBIST and IQVIA:

- `hira_benefit_notice`
- `hira_benefit_notice_brand`
- `hira_benefit_crawl_run`
- `hira_benefit_crawl_state`

A notice-to-brand mapping table avoids duplicating one notice when several
products are named. Initial matching uses normalized explicit brand names
loaded from `cache_brands.default` where `is_jw` is true. No brand list is
hardcoded. Product-code enrichment can be added after the first-scope list is
provided and its namespace is confirmed.

## Runtime Evidence and Unknowns

- Detail page `brdBltNo=53026` exposed title, category, notice reference,
  posting date, attachment names, and body.
- A single bare GET to the supplied list entry point returned HTTP 200 with an
  833-byte "page information does not exist" body. It did not reproduce the
  F16 168,552-byte response.
- Therefore list parameters/session behavior and global ID monotonicity,
  reuse, and gaps are not confirmed in this round.
- The first-scope competitor brand list has not been supplied.

The zero-row index gate prevents the observed error page from advancing state.
