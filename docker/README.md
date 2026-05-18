# Phase 16-B Local MariaDB Galera

Local MariaDB Galera environment for the mart pipeline track in `jw-market-test`.

This uses the same MariaDB Galera image tag confirmed for the GCP environment, via Bitnami's free legacy registry:

```text
bitnamilegacy/mariadb-galera:12.0.2-debian-12-r0
```

Bitnami's 2025-08 free image registry split moved free `bitnami/*` images to `bitnamilegacy/*`, so the local compose file uses `bitnamilegacy/` while keeping the exact MariaDB Galera version tag.

The local setup is a single-node standalone bootstrap. It is for schema migration and ETL verification only, not for production cluster testing.

This is the base environment for Phase 16-C and later:

- Layer 1 raw staging
- Layer 2 enriched facts
- Layer 3/4 mart tables with JSON columns and generated-column indexes

## Requirements

- Docker Desktop
- About 1GB of free disk space

## Initial Setup

```bash
cd /Users/rexxa/github/jw-market-test/docker
cp .env.example .env
```

Edit `.env` and replace the `changeme-*` placeholders with local passwords.

## Start

```bash
docker compose up -d mariadb
docker compose ps
docker compose logs -f mariadb
```

Wait until the container becomes healthy before running ETL or migrations.

## Connection Test

Root user:

```bash
docker exec -it jw-mariadb mariadb -u root -p
```

Application user:

```bash
docker exec -it jw-mariadb mariadb -u jwapp -p jw_mart
```

## Automated Verification

After the container is healthy:

```bash
bash verify.sh
```

The script checks container health, MariaDB version, database/user access, JSON column behavior, virtual generated-column indexing, timezone, and character set.

## JSON Column Verification

Manual SQL smoke test:

```sql
INSERT INTO _jsoncheck VALUES (1, '{"name": "Atorvastatin", "amount": 100}');
SELECT id, payload, name FROM _jsoncheck;
SELECT JSON_EXTRACT(payload, '$.amount') FROM _jsoncheck WHERE id = 1;
```

## Stop / Delete

Stop while keeping the database volume:

```bash
docker compose down
```

Stop and delete all local database data:

```bash
docker compose down -v
rm -rf data/
```

## Troubleshooting

Port conflict:

- If host port `3307` is already in use, edit `HOST_PORT` in `.env`.
- During the Codex verification run, local port `3307` was already allocated, so `.env` was adjusted to `HOST_PORT=3308`. The compose file still defaults to `3307` for new setups.

Healthcheck failure:

```bash
docker compose ps
docker compose logs --tail=200 mariadb
```

If Galera refuses to bootstrap after an unclean local shutdown, reset the local dev volume. This deletes only local Docker data under `docker/data/`.

Reset local data when the container was initialized with the wrong credentials:

```bash
docker compose down -v
rm -rf data/
docker compose up -d mariadb
```
