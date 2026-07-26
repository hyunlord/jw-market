# F21 probe capture contract v1

The F21 probe harness records raw observations. It does not decide whether an
answer is correct.

## Question set

`eval/f21_probe_questions.v1.json` uses
`chat_f21_question_set_v1`. Stages own output directories. Scenarios own a
conversation and may contain one or more ordered turns. Each repetition creates
a separate conversation. `skip_reason` records a non-executed scenario without
inventing a replacement question.

Questions, turn order, repetitions, conversation boundaries, output names, and
SKIP conditions are data. Adding or removing a question does not require a
harness code change.

## Per-question output

Every attempted turn writes one `.sse` file and one `.json` file. The JSON schema
identifier is `chat_f21_question_answer_v1` and retains the F21 fields:

`stage`, `case_id`, `repetition`, `turn`, `question`, `conversation_id`,
`conversation_id_sha256`, `pod`, `trace_id`, `disposition`, `tools_called`,
`answer_full`, `answer_sha256`, `timing`, `total_elapsed_ms`,
`client_elapsed_s`, `http_status`, `error`, `qa_trace`, `trace`,
`router_diagnostics`, `conversation_event`, `event_names`, `sse_file`,
`sse_raw`, `started_utc`, and `finished_utc`.

The `.sse` file is the complete response body. `sse_raw` contains the same text
for direct comparison with the original F21 evidence.

## Run output

`progress.json` is atomically refreshed after each captured turn, so completed
turns survive a later interruption. `capture_summary.json` uses
`chat_f21_massive_live_probe_summary_v1`. `run_metadata.json` records the target
commit, generation, digest, endpoint, question-set hash, start and finish time,
and pacing settings. Authentication values are never recorded.

## Running the harness

Run from `chat/jw-chat-agent-poc`:

```bash
python3 -m scripts.f21_probe.cli \
  --base-url http://127.0.0.1:8080 \
  --stream-path /api/v1/market/socket-lab/stream \
  --output /tmp/f21-probe-run \
  --target-commit local \
  --target-generation local \
  --target-digest local
```

Change `--base-url` and `--stream-path` to target local, test2, or production
without changing the harness. Supply authentication through environment
variables, never command-line values:

```bash
python3 -m scripts.f21_probe.cli \
  --base-url https://example.invalid \
  --header-env 'Authorization=F21_AUTHORIZATION' \
  --output /tmp/f21-probe-run \
  --target-commit "$TARGET_COMMIT" \
  --target-generation "$TARGET_GENERATION" \
  --target-digest "$TARGET_DIGEST"
```

The default is one request at a time with a two-second interval. Concurrency is
explicitly bounded to 1-4. A non-empty output directory is rejected to prevent
mixing evidence from separate targets.
