# Upload Progress Contract

This contract describes how a portal can expose immediate file readiness without
waiting for the complete preprocessing and indexing job. It is additive: clients
that only use the synchronous upload result continue to work unchanged.

## Flow

1. Upload files through the existing upload endpoint with one session identifier.
2. When the response has `state: "accepted"`, render `file_cards` immediately and
   retain `upload_id`.
3. Poll `GET /upload/status` with the same `workflow_id`, the same session
   identifier, and `upload_id`.
4. Enable questions for a file as soon as its `query_ready` value is `true`.
5. Stop polling when the top-level state is `ready`, `blocked`, `failed`,
   `interrupted`, or `expired`.

The status endpoint is session-owned. A client must not reuse an `upload_id` with
another `chat_id` or `app_session_id`.

## Immediate Response

The accepted response contains these public fields:

```json
{
  "mode": "upload",
  "temp_documents": [],
  "file_cards": [],
  "upload_id": "opaque upload id",
  "state": "accepted",
  "ready": false,
  "message": "파일 확인 완료. 질문 준비를 진행하고 있습니다.",
  "status_url": "/upload/status"
}
```

`file_cards` are deterministic metadata derived from the uploaded file. They are
safe to display immediately, but they are navigation aids rather than evidence
for numeric or factual answers.

## Status Request

```http
GET /upload/status?workflow_id={workflow_id}&chat_id={chat_id}&upload_id={upload_id}
```

`app_session_id` may be used instead of `chat_id`. One of them is required.

## Status Response

```json
{
  "upload_id": "opaque upload id",
  "state": "preprocessing",
  "ready": false,
  "files": [
    {
      "file_name": "report.pdf",
      "state": "preprocessing",
      "route": "vdb",
      "message": "앞 20/185페이지는 질문할 수 있습니다.",
      "query_ready": true,
      "indexed_pages": 20,
      "total_pages": 185,
      "card": {}
    }
  ],
  "message": "파일을 처리하고 있습니다.",
  "updated_at": "2026-07-18T06:00:00+09:00",
  "expires_at": "2026-07-19T06:00:00+09:00"
}
```

Top-level and per-file states are:

- Active: `accepted`, `preprocessing`, `committing`
- Successful terminal: `ready`
- Unsuccessful terminal: `blocked`, `failed`, `interrupted`, `expired`

`ready` means every file reached the successful terminal state. `query_ready`
is deliberately per-file and can become true earlier.

## Presentation Rules

The first UI version can render `message` and `file_cards` in the existing chat
message channel. A later dedicated upload card may use the same fields for a
progress bar and file-level status.

- If `indexed_pages` and `total_pages` are present, progress is
  `indexed_pages / total_pages`.
- If they are absent, show the textual state and do not invent a percentage.
- A file with `query_ready: true` may be queried while the full job continues.
- Do not label the whole upload complete until the top-level `ready` is true.
- Preserve one card and one status row per uploaded file; do not collapse a
  multi-file upload into the first file.

## Partial-Index Honesty

When a PDF is only partially indexed, answers must retain the backend scope
caption, for example `앞 20/185페이지 기준`. The UI must not remove or weaken
that caption.

- A fact found in the indexed range may be stated normally with its page source.
- Absence is only absence from the indexed range, not from the entire document.
- After the full upload reaches `ready`, the same question uses the complete
  document and no partial-range caption is needed.

## Evidence Boundary

Machine cards and generated briefs help the user understand and navigate the
file. They are not answer evidence. Numeric aggregation uses file-SQL, and
document claims use retrieved VDB chunks with file and location provenance.
Clients must not promote card or brief text into a source citation.

