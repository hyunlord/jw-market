# Agent3 Prompt Changelog

## 2026-07-05 — full-run preparation

- Require narrative sentences to copy rounded strings from candidate
  `display_numbers`; raw numeric values remain available only in `numbers`.
- Add low-base guidance: candidates flagged `low_base=true` must include a
  volatility caveat instead of overstating percentage growth.
- Keep the fixed JSON output contract from the wf316 skeleton prompt.
