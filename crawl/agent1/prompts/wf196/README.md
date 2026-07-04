# WF196 Rubric Prompt Archive

This directory keeps byte-preserved WF196 scoring prompt snapshots and the
change history for the rubric that is deployed through GenOS workflow 196.

## Files

- `wf196_rubric_v3.md`: deployed WF196 system prompt from GenOS workflow rev
  5347, containing rubric v3. This file is intentionally stored without a
  repo-added header so its SHA256 can be compared directly with deployment
  evidence.
- `wf196_rubric_rev4084_baseline.md`: live WF196 system prompt captured before
  the v3 deployment. Use this as the rollback and comparison baseline.
- `wf196_rubric_CHANGELOG.md`: rationale and evaluation history from v1 through
  v3.

## Evidence

The byte source for the two prompt snapshots is:

- `/tmp/wf196_rubric_v3_deploy_20260704_043635.zip`
  - `evidence/wf196_system_prompt_rev5347_live.txt`
  - `evidence/wf196_system_prompt_rev4084_live_backup.txt`
- Replay and evaluation evidence:
  - `/tmp/wf196_prompt_rubric_eval_20260703_184321.zip`
  - `/tmp/wf196_rubric_v2_replay_20260703_190024.zip`
  - `/tmp/wf196_rubric_v22_replay_20260703_222631.zip`
  - `/tmp/wf196_rubric_v3_replay_20260704_031635.zip`

Known hashes:

```text
ae42a12bd4a278304226e8b123e24fa8fa0b9cca4dd925106c481f4a94a61e1f  wf196_rubric_v3.md
9b0c9518b88bd587597f7bca7429ad0049f594a399df0efc23a494822b89983d  wf196_rubric_rev4084_baseline.md
372020f87755664e4377b575e3956f9a57fe9522a295546b59c43a9c30678843  rubric_v3_replacement.txt in replay evidence
```

Verify the repo copies with:

```bash
shasum -a 256 crawl/agent1/prompts/wf196/wf196_rubric_v3.md \
  crawl/agent1/prompts/wf196/wf196_rubric_rev4084_baseline.md
```

## Deployment And Rollback

WF196 prompt changes are applied in GenOS by creating a new workflow revision
from the edited system prompt, then deploying that revision to the serving
endpoint used by `score_v2.py`.

For rollback, restore the workflow prompt to the rev4084 baseline or redeploy
the previously captured rev4084 workflow revision. Do not edit this archive as
part of an emergency rollback; commit a new prompt snapshot only after the
runtime action has been verified and the evidence zip exists.

## Maintenance Rules

- Keep deployed prompt snapshots byte-preserved when they are meant to be
  hash-comparable evidence.
- Store rationale, hashes, replay metrics, and deployment notes in README or
  CHANGELOG files rather than adding metadata headers to raw prompt snapshots.
- If the scoring filter or Agent2 consumption contract changes, update this
  directory together with the workflow revision evidence.
