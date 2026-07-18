# Row-topic monthly wrapper source of truth

The live `brand-activity-row-topic-monthly` CronJob historically mounted
`row_topic_monthly_wrapper.py` from the
`brand-activity-row-topic-monthly-wrapper` ConfigMap. The canonical source is
now `row_topic_monthly_wrapper.py` in this directory.

The initial canonicalization deliberately does not mutate the running CronJob.
Before a later deployment, compare the ConfigMap key byte-for-byte with this
file, update the ConfigMap through the reviewed deployment path, and confirm
the SHA-256 value recorded by
`tests/deploy/test_pipeline_canonical_artifacts.py`. A future image-based
transition must preserve the same arguments and environment contract before
the ConfigMap mount is removed.

