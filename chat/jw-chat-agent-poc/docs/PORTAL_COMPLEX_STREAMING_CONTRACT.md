# Portal complex-answer streaming contract

## Purpose

The portal currently waits for the completed JSON response from
`POST /api/v1/rnd/chat/query` and then reveals `result.data.text` with a local
timer. That animation is not server streaming and cannot expose verified tool
results or answer paragraphs while the backend is still working.

This contract adds a streaming route without changing the existing JSON route.
The existing route remains the compatibility fallback until the portal, its
BFF, and wf301 all support the event stream.

## Portal-facing route

`POST /api/v1/rnd/chat/query/stream`

- Request body: identical to `POST /api/v1/rnd/chat/query`.
- Authentication and portal user headers: identical to the existing route.
- Response content type: `text/event-stream; charset=utf-8`.
- The BFF must flush each event immediately and must not buffer, gzip, merge,
  reorder, or replay content events by type.
- The browser must consume the response with `fetch()` and a stream reader.
  Native `EventSource` is not suitable because this route carries the existing
  POST body and abort signal.

## Event ordering

Events are processed strictly in arrival order.

1. `conversation`: newly allocated conversation id, when present.
2. `step`: actual backend work status. Multiple independent tools may be in
   progress at the same time; the UI must not serialize them artificially.
3. `sources`: source labels known at the time of the first verified content.
4. `file_sources`: uploaded-file provenance, when present.
5. `delta`: verified prose. Append exactly as received.
6. `markdown_block`: verified table or other markdown block. Append at its
   arrival position rather than collecting all blocks at the end.
7. `charts`: chart specifications.
8. `timing`, then `trace`.
9. `done`: terminal status.

`delta` and `markdown_block` may repeat and interleave. This ordering is what
keeps a heading adjacent to its table and prevents dates or URLs from being
split and reassembled incorrectly.

## First-display rule

- Show the first `delta` or `markdown_block` immediately.
- Do not run the existing `streamMessageText()` timer over streamed content.
- A verified-evidence lead may arrive after tools finish but before final LLM
  synthesis. It is part of the answer and must be rendered once.
- Progress labels are not answer text and must remain in the progress panel.
- Internal fields such as raw tool ids, `mode=parallel`, or stage counters must
  never be concatenated into visible labels.

## Completion and cancellation

- Keep `isGenerating=true` until `done` or an `error` event arrives.
- The existing AbortController cancels the fetch reader and then invokes the
  existing backend abort flow with the current trace id when available.
- On user cancellation, retain content already received; do not replace it
  with the completed JSON response.
- On stream failure before any content, retry the existing JSON route once.
- On stream failure after content began, show an interruption notice and do
  not append a second JSON answer, which would duplicate text.

## Session and history consistency

- Use the `conversation` event as the same chat id source currently read from
  `result.chatId` or `result.data.chatId`.
- Persist one final answer assembled from content events in arrival order.
- Sidebar/history projection must store the verified final answer, not only the
  transient progress labels.
- A follow-up turn must send the same conversation id and only the new user
  question. Prior progress labels or tool plans must not be copied into it.

## Acceptance checks

1. A complex clinical-plus-permission question renders verified content before
   final synthesis completes.
2. A simple market fast-path response remains byte-equivalent to the existing
   completed JSON text.
3. Headings and tables remain interleaved in arrival order.
4. Dates and URLs contain no inserted whitespace.
5. Parallel steps appear concurrently and expose no internal names.
6. Cancellation leaves no duplicate or late-appended answer.
7. Existing JSON-only clients continue to work unchanged.

## Ownership boundary

- Chat agent: emits ordered, safety-gated SSE events.
- wf301/BFF: relays events without buffering or reordering and preserves portal
  authentication/session headers.
- Portal: replaces fake text timing with a fetch-reader loop and renders events
  in arrival order.

The chat agent cannot make the production portal stream by itself. Portal and
BFF adoption of this additive contract is required for portal-visible TTFT.
