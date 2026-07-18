# Row Topic Share Definition

## Contract

`share_pct` for row-level topic assignment means:

```text
affected_rows(topic_id, brand, scope) / brand_total_rows(brand, scope) * 100
```

The unit of judgment is an independent row-topic yes/no decision. A single keyword activity row may be assigned to multiple topics when it substantially conveys multiple concepts. Therefore topic shares are not mutually exclusive and their sum is not expected to equal 100%.

`etc_pct` is a compatibility field computed after ranking and truncating the displayed axis topics:

```text
max(0, 100 - sum(share_pct for displayed top_n axis topics))
```

It is not an "other topic" share, an unassigned-row share, or the complement of a mutually exclusive distribution. Because the topic decisions overlap, the displayed topic sum may exceed 100%, in which case `etc_pct` is clipped to 0. It also changes when `top_n` changes because only the displayed axis topics are subtracted.

Frontend wording:

```text
share = 해당 토픽을 언급·전달한 활동 비율(중복 가능)
etc_pct = 표시된 상위 토픽 비율 합을 100에서 차감한 호환값(기타·미분류 비율 아님)
```

## Prompt Policy

Prompt version: `row_topic_v1`

The model receives a fixed rubric and keyword rows. It must not create new topics. For each row, it asks whether the row substantially conveys each listed topic. A row may carry several topics, or no topics. Empty topic arrays represent `none`; no row is inserted into `row_topic_assignment` for none.

This is intentionally different from a forced primary-topic classifier. The v2 mini check showed that primary-message competition suppresses co-occurring broad concepts and no longer measures the intended “related activity row ratio.”

## Relation To Existing Payload Shares

The previous payload was produced by batch-level summarization. Row-level assignment is a more explicit measurement layer:

- broad clinical topics can rise because every row-topic relation is counted independently;
- small or specific topics should remain close when the rubric is stable;
- totals above 100% are valid and expected.

Pilot evidence on G04C2/THRUPAS:

- 1,082 rows classified with 8 calls.
- 100% row coverage, no duplicate ids, no missing ids, no brand-specific misassignment.
- Six of the nine tracked topic ids were near the previous payload; broad T1/T2 rose under the independent row-topic definition.
- Filtered topic distributions were computed with zero LLM calls from the assignment table.

## Schema Policy

`row_topic_assignment` is versioned by `topic_set_version`, which is the source run id for the fixed market axis and brand-specific rubric.

When the topic set changes, assignment rows for that version are not comparable with earlier versions. A new `topic_set_version` requires a full re-assignment for affected rows. Current cost evidence suggests a full 65K-row pass is roughly $9-10 at batch size 150. Monthly increment mode should classify only new unassigned `(row_id, topic_set_version)` pairs.

Recommended operating policy:

- freeze the topic set quarterly unless PL explicitly approves a re-axis;
- run monthly incremental row assignment for new keyword rows;
- rebuild compatibility payloads and filterable shares through SQL aggregation over row assignments.

## Compatibility View

Legacy consumers can keep the current `topic_shares` item shape:

```json
{"topic_id":"T1","label":"배뇨개선","affected_row_count":310,"share_pct":28.7}
```

The denominator is always brand-total keyword rows for the selected scope and filter. Filter axes such as period, visit location, specialty, interest/usefulness, and prescription evolution can be applied before grouping, with no additional LLM calls.
