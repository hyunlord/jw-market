# Scheduling Spec

This folder contains deployment-ready scheduling references for the pipeline container.
They are intentionally not applied by Phase R-3.

## Monthly order

1. `load-ubist --all --truncate`
2. `load-iqvia --all`
3. `enrich --all`

The jobs are separated in Cloud Scheduler so operations can rerun only the failed stage.
The GKE CronJob example runs `all` as one conservative monthly batch where a single
Kubernetes job owns the whole sequence.

## Operational rules

- Use `concurrencyPolicy: Forbid` or equivalent so monthly loads do not overlap.
- Mount `/data` read-only and `/output` read-write.
- Use `LOG_FORMAT=json` in managed runtimes.
- Promote image tags explicitly; do not run `latest` in GCP.
