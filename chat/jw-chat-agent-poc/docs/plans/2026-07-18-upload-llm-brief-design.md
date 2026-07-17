# Upload LLM Brief Design

## Goal

Keep the deterministic upload card as the first response, then append one concise
brief for every uploaded file in the session. Its 3-5 sentence summary is rendered
from observed metadata, while one batched LLM call selects three file-appropriate
questions from server-owned safe templates.
The brief helps the user decide what to ask next; it is never answer evidence.

## Data Boundary

- `wf301-vdb-bridge` persists and exposes only observed upload-card metadata.
- Chat sends the complete session file list to one batched LLM call.
- The prompt contains file names, types, titles, page/slide counts, and worksheet
  bounds only. It does not contain inferred totals or document claims.
- The validator requires one result per input file, the exact file names, and
  exactly three distinct questions copied from the server-owned allowlist. It
  rejects modified questions and all extra fields.
- Summary sentences are deterministic and use only observed metadata, so the
  model cannot add unsupported content claims.
- Invalid, empty, or unavailable LLM output is discarded. The machine card
  remains usable without delay.

## Streaming

The existing SSE channel emits the machine card before the LLM call. Once the
validated batch returns, only the brief suffix is emitted. Replay and non-stream
responses contain the same combined text without duplicating the card.

## Evidence Rule

`file_brief_is_answer_evidence` remains `false`. Neither the machine card nor the
LLM brief is inserted into file search context, fact markdown, or source lists.
Detailed questions continue to use file-SQL or VDB evidence.
