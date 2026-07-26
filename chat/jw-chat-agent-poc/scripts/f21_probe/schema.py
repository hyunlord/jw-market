from __future__ import annotations


QUESTION_ANSWER_SCHEMA = "chat_f21_question_answer_v1"
SUMMARY_SCHEMA = "chat_f21_massive_live_probe_summary_v1"
RUN_METADATA_SCHEMA = "chat_f21_probe_run_metadata_v1"

QUESTION_ANSWER_FIELDS = {
    "answer_full",
    "answer_sha256",
    "case_id",
    "client_elapsed_s",
    "conversation_event",
    "conversation_id",
    "conversation_id_sha256",
    "disposition",
    "error",
    "event_names",
    "finished_utc",
    "http_status",
    "pod",
    "qa_trace",
    "question",
    "repetition",
    "router_diagnostics",
    "schema",
    "sse_file",
    "sse_raw",
    "stage",
    "started_utc",
    "timing",
    "tools_called",
    "total_elapsed_ms",
    "trace",
    "trace_id",
    "turn",
}
