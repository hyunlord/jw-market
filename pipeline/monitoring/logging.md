# Structured Logging

All R-3 pipeline scripts use the shared `pipeline/scripts/ops_utils.py` logging setup.

## Plain text default

```text
2026-05-19 15:10:00 [INFO] scripts.etl.iqvia_loader: reading NSA ...
```

## JSON mode

Set either variable:

```bash
LOG_FORMAT=json
# or
STRUCTURED_LOGS=true
```

JSON logs include:

- `timestamp`
- `level`
- `logger`
- `message`
- `exception` when present

## Runtime controls

- `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR`
- `ETL_RETRY_ATTEMPTS=3`
- `ETL_RETRY_BASE_SECONDS=1.0`
- `PROJECT_ROOT=/app` in containers
