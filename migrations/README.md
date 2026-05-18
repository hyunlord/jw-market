# Migration Policy

This directory stores MariaDB schema migrations for the local mart pipeline.

## Rules

- Apply each migration once.
- Track applied migrations in `_migration_state`.
- Store a SHA-256 checksum for every applied SQL file.
- Skip an already-applied migration when the checksum matches.
- Stop with an error when an applied migration file has changed.

## Migration ID Format

Migration files use a numeric prefix:

```text
NNN_description.sql
```

Examples:

- `000_migration_state.sql`
- `001_layer1_raw_tables.sql`

## Commands

```bash
python scripts/run_migration.py status
python scripts/run_migration.py apply 000
python scripts/run_migration.py apply 001
python scripts/run_migration.py apply --all
```

The runner reads database credentials from `docker/.env`.

## Rollback Policy

Rollback is manual at this stage. Add a new forward migration for normal schema
changes; do not edit an already-applied migration.
