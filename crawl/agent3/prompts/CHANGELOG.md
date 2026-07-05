# Agent3 Prompt Changelog

## 2026-07-05 — wf316 live rev 5359

- Registered and deployed repo prompt to wf316 as revision 5359 / deploy 1321.
- Live prompt SHA256 matches `jw_agent3_brand_strength_prompt.md`.
- This revision supersedes skeleton rev 5356, which did not enforce
  `display_numbers` narrative copy.

## 2026-07-05 — full-run preparation

- Require narrative sentences to copy rounded strings from candidate
  `display_numbers`; raw numeric values remain available only in `numbers`.
- Add low-base guidance: candidates flagged `low_base=true` must include a
  volatility caveat instead of overstating percentage growth.
- Keep the fixed JSON output contract from the wf316 skeleton prompt.

## 2026-07-05 — wf316 server-side numbers contract

- Removed model-owned `numbers` echo from the wf316 prompt contract.
- Added `candidate_index` as the model-to-candidate join key; server code injects exact raw numbers from candidates after workflow output.
- Preserved display-number-only narrative rule and low-base caveat rule.


## 2026-07-05 — wf316 live rev 5365

- Registered workflow rev 5365 and deployment 1324 after removing model-owned `numbers` output.
- Synchronized Flowise backing row `82c6dfa1-da8f-42a2-8837-15586c1e6829` with the same prompt text; previous backing row still held the rev 5356 skeleton prompt.
- Runtime code now injects exact `numbers` from matched candidates after wf316 returns `candidate_index`.
