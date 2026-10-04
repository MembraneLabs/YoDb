# Local PostgreSQL end-to-end run

Spins up a throwaway Postgres in Docker, seeds three "sources" (schemas `crm`,
`billing`, `support`), activates the real YAML catalog through the live
inspector/validator, and runs ~50 logical queries. Each query is printed with
its physical plan, the exact SQL each source received (with parameters and row
counts), the rows, and a PASS/FAIL against an independent native SQL join.

Nothing is persisted: data lives on `tmpfs`, so removing the container removes
all of it (no Docker volume is created).

## Run

```bash
# 1. start (port 55432, loopback only) and wait until ready
docker run -d --name yodb-e2e-pg --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=yodb_e2e \
  -p 127.0.0.1:55432:5432 pgvector/pgvector:pg17
until docker exec yodb-e2e-pg pg_isready -U postgres -d yodb_e2e; do sleep 1; done; sleep 2

# 2. seed tables + the read-only role `yodb_ro` (the only role YoDb uses)
docker exec -i yodb-e2e-pg psql -U postgres -d yodb_e2e -v ON_ERROR_STOP=1 -q < tests/e2e/seed.sql

# 3. run everything (or pass name substrings: `... run_e2e.py M05 S12`; `-q` hides rows)
PYTHONPATH=src .venv/bin/python tests/e2e/run_e2e.py

# 4. tear down (removes data too)
docker rm -f yodb-e2e-pg
```

Override the server with `YODB_E2E_CONNINFO="host=... dbname=... user=yodb_ro password=..."`.
Exit code is non-zero when any case fails.

## Data (see `seed.sql`)

| Table | Rows | Purpose |
| --- | --- | --- |
| `crm.accounts` | 12 | identity source; NULL name/seats/signup/country, duplicate name (c01/c12), mixed case |
| `billing.customers` | 10 | contributor; no row for c04/c09/c12, orphan c20, NULL plan (c06), NULL mrr (c03) |
| `support.tickets_summary` | 6 | second contributor; orphan c30, NULL tier (c08) |
| `billing.dup_accounts` | 3 | duplicate identity -> catalog must be **rejected** (`catalog_dup/`) |
| `crm.events` | 10,500 | exceeds the 10,000-row scan guard |

## Semantic cases (`V`)

`helpdesk.tickets` (20 tickets, 5-dim keyword-count embeddings built in SQL by
`helpdesk.toy_embed`; the runner's `ToyEmbedder` computes the same vector) and
`helpdesk.owners` (second source). Every V case runs under Plan A and Plan B,
compares with a native SQL keyword oracle, and prints the plan, SQL, and the
semantic stats (considered / shortlisted / verified / qualified / cost). Needs the
`pgvector/pgvector` image. On a 20-row table Plan B verifies about as many
records as Plan A because the shortlist covers the table; savings need a large one.

## Case families

`S` single-source pushdown, `C` coordinator-only operators, `M` multi-source
assembly, `B` big-table guard, `E` rejections. The oracle is a hand-written
`LEFT JOIN` across the three schemas (harness only; YoDb never sees it).

## Known caveats of this setup

- Timestamps: query values must be timezone-aware; naive source timestamps are
  read as UTC and sessions are pinned to `TimeZone=UTC`.
- Text ordering relies on the server's collation agreeing with Python's string
  comparison. The alpine image sorts by code point; a glibc `en_US.UTF-8`
  database may order mixed-case text differently between pushed SQL ordering
  (single-source) and coordinator ordering (multi-source). Not covered here.
