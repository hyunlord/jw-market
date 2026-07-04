# WF196 Rubric Changelog

This log records the rubric changes that led from the rev4084 baseline to the
rev5347 rubric v3 deployment for workflow 196.

## Revision Summary

| Version | Status | Main Change | Evidence |
| --- | --- | --- | --- |
| rev4084 baseline | Previous live workflow | Existing rubric before the precision tightening work | `wf196_rubric_rev4084_baseline.md` |
| v1 | Evaluation draft | First explicit rubric replacement and compact evaluation run | `wf196_prompt_rubric_eval_20260703_184321.zip` |
| v2 | Replay draft | Added stricter relatedness handling and score caps to reduce cutoff-band overmatching | `wf196_rubric_v2_replay_20260703_190024.zip` |
| v2.1 | Replay draft | Rebalanced low-incidental behavior after v2 over-penalized some concrete indirect cases | `wf196_rubric_v2_replay_20260703_190024.zip` |
| v2.2 | Replay draft | Refined cap buckets and review bands, with population forecast of score>=50 reduced from 37.29% to about 16.2% | `wf196_rubric_v22_replay_20260703_222631.zip` |
| v3 | Deployed | Restored a clearer importance axis while preserving the relatedness gate and stricter caps | `wf196_rubric_v3_replay_20260704_031635.zip`, deployed as rev5347 |

## Why The Rubric Changed

The rev4084 prompt admitted too many weakly related articles into the score>=50
set. The observed operating population had 12,223 of 32,782 rows at score>=50
(37.29%). Replay work showed diffusion in the old 40-point band: the
`diffusion_40s` sample averaged 42.7 under the old rubric and 8.0 under the
tighter v1 draft.

The desired behavior was not simply "lower all scores." High-confidence direct
matches needed to stay high, while incidental mentions, broad company context,
and competitor-only references needed explicit ceilings.

## v1

v1 introduced a more explicit rubric replacement and evaluated it against the
same sampled rows as the rev4084 prompt. It proved that the prompt could sharply
reduce diffusion while preserving high-regression cases, but it still needed
better boundaries for indirect-but-concrete market events.

Key evidence:

- `wf196_prompt_rubric_eval_20260703_184321.zip`
- `evidence/rubric_v1_replacement.txt`
- `eval_comparison_summary.csv`

## v2

v2 tightened relatedness and cap logic. The goal was to stop articles with only
thin company, category, or competitor proximity from crossing the operational
threshold.

Replay evidence:

- `cutoff_50_55`: score>=50 fell from 20/20 old rows to 6/20 under v2.
- `diffusion_40s`: average score moved from 42.7 old to 0.0 under v2.
- `high_regression`: 10/10 stayed at score>=50, with average near 87.5.

The rejected shape was an overly permissive threshold where any mention of a
therapeutic area could be treated as operationally actionable.

## v2.1

v2.1 adjusted the low-incidental area after v2 proved too blunt for some
concrete indirect cases. The change kept pure incidental references below the
threshold while allowing documented market-adjacent events to remain visible
for review.

Replay evidence:

- `v21_low10.low_incidental`: score>=50 stayed 0/10.
- Average low-incidental score settled near 15.9 in the focused check.

## v2.2

v2.2 refined the cap taxonomy and review bands. It separated pure incidental
mentions from concrete boundary cases and added more precise cap behavior for
market context, competitor references, and low-evidence claims.

Replay evidence from 400 sampled rows:

- Old score>=50: 164/400 (41.0% sample).
- v2.2 score>=50: 74/400 (18.5% sample).
- Population forecast score>=50: about 5,310.2 of 32,782 (16.2%).
- Tag violations: 0.

This became the precision baseline, but review showed that some important,
clearly related articles were being held too low because importance and
relatedness were not separated clearly enough.

## v3

v3 preserved the relatedness gate and stricter cap behavior, then restored a
clearer importance axis. The intent was to keep weak articles out while letting
high-importance, strongly related events recover score.

Replay evidence:

- Old score>=50: 164/400 (41.0% sample).
- v2.2 score>=50: 74/400 (18.5% sample).
- v3 score>=50: 76/400 (19.0% sample).
- Population forecast score>=50: about 5,560.3 of 32,782 (16.96%).
- Tag violations: 0 in eval and replay.
- Important-related uplift examples were validated in `two_axis_validation.csv`.

Deployment:

- Previous live workflow: rev4084.
- Deployed v3 workflow: rev5347.
- Deploy evidence zip: `/tmp/wf196_rubric_v3_deploy_20260704_043635.zip`.
- Deployed live prompt hash:
  `ae42a12bd4a278304226e8b123e24fa8fa0b9cca4dd925106c481f4a94a61e1f`.

## Forward Rules

- Do not rename or reinterpret the score>=50 threshold without checking Agent2
  and downstream bundle filters.
- Keep direct product/news matches high; fix overmatching with relatedness gates
  and caps, not by globally depressing the scoring scale.
- Any future rubric change should include: baseline prompt, replacement text,
  replay metrics, tag violation check, deployment revision, and a hash of the
  live prompt after deployment.
