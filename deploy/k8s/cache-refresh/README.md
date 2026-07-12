# Deep-analysis cache refresh

`jw-cache-refresh-daily-cronjob.yaml` is the repository copy of the live
`jw-cache-refresh-daily` CronJob. The scripts mounted from the
`cache-refresh-validator` ConfigMap are canonicalized at:

- `pipeline/scripts/etl/cache_refresh/cache_deep_analysis_events_update.py`
- `pipeline/scripts/etl/cache_refresh/cache_deep_analysis_refresh_validate.py`

Regenerate the ConfigMap manifest from those files before applying the CronJob:

```bash
kubectl -n llmops create configmap cache-refresh-validator \
  --from-file=cache_deep_analysis_events_update.py=pipeline/scripts/etl/cache_refresh/cache_deep_analysis_events_update.py \
  --from-file=cache_deep_analysis_refresh_validate.py=pipeline/scripts/etl/cache_refresh/cache_deep_analysis_refresh_validate.py \
  --dry-run=client -o yaml > /tmp/cache-refresh-validator.yaml

kubectl apply -f /tmp/cache-refresh-validator.yaml
kubectl apply -f deploy/k8s/cache-refresh/jw-cache-refresh-daily-cronjob.yaml
```

Review the generated ConfigMap diff and script SHA-256 values before applying.
The checked-in scripts intentionally match the live ConfigMap byte-for-byte;
behavior changes belong in a separate reviewed change.
