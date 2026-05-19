# JW Market Test

`jw-market-test` is the prototype track for the JW market mart pipeline. The
current repository separates source data, pipeline code, and generated outputs so
it can be carried to GCP without changing the local development contract.

## Current state

- Catalog: 16 `ml_market` + 19 `cd_market` definitions in `catalog/`.
- Layer 1: UBIST parquet hive partitions in `output/ubist/` and IQVIA raw tables in MariaDB.
- Layer 2: enriched facts in `output/enriched/`.
- Local DB: MariaDB Galera 12.0.2 under `pipeline/docker/`.
- System viewer: `viewer/current_state.html`.

## Repository structure

| path | role |
|---|---|
| `catalog/` | YAML source-of-truth for market metadata and matching dictionaries |
| `data/` | raw input files, gitignored |
| `pipeline/` | Docker, migrations, ETL scripts, scheduling, monitoring specs |
| `output/catalog/` | versioned dimension parquet outputs |
| `output/ubist/` | generated UBIST parquet partitions, gitignored except manifest |
| `output/enriched/` | generated Layer 2 parquet partitions, gitignored |
| `viewer/` | HTML verification dashboards |
| `docs/` | architecture, deployment, and operations documentation |
| `audits/` | phase audit artifacts |

## Local quick start

```bash
cd /Users/rexxa/github/jw-market-test
cd pipeline/docker
cp .env.example .env
# edit .env if needed
# docker compose up -d mariadb
# bash verify.sh
cd ../..
python pipeline/scripts/run_migration.py status
```

The R-3 phase does not build or run the ETL container. When ready, build with:

```bash
make -f pipeline/Makefile build
```

## Pipeline commands

```bash
make -f pipeline/Makefile init
make -f pipeline/Makefile load-ubist
make -f pipeline/Makefile load-iqvia
make -f pipeline/Makefile enrich
make -f pipeline/Makefile verify
```

## Environment variables

| variable | default | purpose |
|---|---|---|
| `PROJECT_ROOT` | auto-detected | repo or container root |
| `LOG_LEVEL` | `INFO` | script log verbosity |
| `LOG_FORMAT` | plain text | set `json` for structured logs |
| `STRUCTURED_LOGS` | unset | set `true` for JSON logs |
| `ETL_RETRY_ATTEMPTS` | `3` | transient DB retry attempts |
| `ETL_RETRY_BASE_SECONDS` | `1.0` | retry exponential backoff base |

## Troubleshooting

- DB connection fails: check `pipeline/docker/.env`, container health, and host port.
- UBIST partition count changes: inspect `output/ubist/_manifest.json` and monthly source files.
- Match rate drops: open `viewer/current_state.html`, then inspect `output/enriched/` and Layer 2 audit CSVs.
- GCP deployment is not yet applied in this repo; see `docs/deployment.md` for the handoff spec.
