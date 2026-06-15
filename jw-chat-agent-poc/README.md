# JW Market Chat Agent P1 POC

Local-only proof of concept for the JW Market chat agent described in
`JW_Chat_Agent_Architecture_v3.md`.

## Scope

- BQ decomposition/routing via the provided BQ map only.
- Brand resolver fixture for 리바로 and 리바로젯, including combo split.
- Cache-backed metric tools using local fixtures.
- External API wrappers for MFDS, ClinicalTrials v2, openFDA, domestic patent,
  and FDA Orange Book patent. Tests run fixture mode; live mode is GET-only and
  reads `DATA_GO_KR_KEY` from the environment when needed.
- Local document RAG over text fixtures, with structured-upload rejection.
- Deterministic orchestrator that returns decomposition, tool calls, answer, and
  source tags.

## Explicit exclusions

HIRA, bundle tools, Paragraph IV, embedding fallback, and operational temp VDB
are out of P1 scope.

## Run

```bash
cd /Users/rexxa/github/jw-market-test
PYTHONPATH=jw-chat-agent-poc pytest -q jw-chat-agent-poc/tests
PYTHONPATH=jw-chat-agent-poc python jw-chat-agent-poc/scripts/run_scenarios.py --out /tmp/chat_poc_verify
```

## BQ map status

The map is copied from the PL-provided screen-read table in the task request.
It must be reviewed by PL before expanding P1 behavior.

