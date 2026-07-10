# wf196 rev5674 cutoff branch handoff

The public news exposure decision is owned by the serving/develop surface and
is not implemented in this crawl branch. New crawl rows provide the immutable
version marker below so serving can apply the confirmed branch without
rewriting historical rows.

## Marker contract

- Legacy or missing marker: retain the existing cutoff policy.
- New wf196 GA rows: `event_brand_scores.source_processor = workflow_196_rev5674`.
- The marker is written only for newly appended Tier1 rows.
- Existing `news_raw`, `events`, and `event_brand_scores` rows must not be
  updated or deleted during cutover.

## Exposure policy

Exposure uses `score >= cutoff` for the row's tag and processor version.

| Tag | Legacy cutoff | `workflow_196_rev5674` cutoff |
|---|---:|---:|
| 자본/경영 | 43 | 53 |
| 외부/트렌드 | 49 | 53 |
| 공급/생산 | 51 | 53 |
| 신약/R&D | 54 | 73 |
| 정책/규제 | 55 | 69 |
| 기타 | excluded | excluded |

Unknown or missing processors must follow the legacy branch so historical rows
do not change behavior. `기타` is always excluded before any numeric cutoff is
evaluated, including Agent2 evidence selection.

## Serving acceptance checks

1. The same tag and score produce legacy and rev5674 results according to the
   two columns above.
2. A score exactly equal to its cutoff is exposed.
3. `기타` is never exposed at any score or processor version.
4. A sample of 20 historical rows has identical exposure output before and
   after deployment.
5. Newly appended rev5674 rows retain `workflow_196_rev5674` through the serving
   query and response path.

The replay crossing asymmetry is accepted by PL as an expected consequence of
tag migration plus integer score steps; it is not a reason to alter the
confirmed cutoff values.
