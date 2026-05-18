# Phase 16-C-1 Layer 1 DDL Audit Summary

Generated at: 2026-05-18T17:52:29

## Pre-push

- Current branch: main
- Pre-push result: skipped because this local repository has no configured `origin` remote.
- Action needed: add/restore remote before pushing `main` and tags.

## Migration

- `000_migration_state.sql`: applied
- `001_layer1_raw_tables.sql`: applied
- `_migration_state`: 2 rows

## Tables

- `ubist_monthly_sales_raw`: created
- `iqvia_nsa_quarterly_raw`: created
- `iqvia_csd_monthly_raw`: created
- `iqvia_chso_monthly_raw`: created

## Verification

- Python compile: PASS
- Docker container: healthy before migration
- Virtual generated column test: PASS
- Test row cleanup: remaining rows = 0

## MariaDB Compatibility Note

MariaDB Galera 12.0.2 rejects an indexed generated column when the indexed column directly uses `CAST(SUBSTRING(...))`. The DDL keeps the visible `period_yyyy` / `period_mm` contract by using invisible helper generated columns, then indexing `period_yyyy`.

## Commit / Tag

- Commit: pending at zip creation time
- Tag: pending at zip creation time
- Push: blocked until `origin` remote is configured
