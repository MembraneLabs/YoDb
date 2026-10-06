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

# 3b. the generated matrix (about 3 minutes; ~8,800 executions; see below)
PYTHONPATH=src .venv/bin/python tests/e2e/run_matrix.py [-v] [--only filter,semantic] [--seed N] [--report FILE]

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

## Optimizer cases (`O`)

`bulk.items` and `bulk.tags` (200,000 rows each; `kind` has 1,000 distinct values,
`tag` has 2). Each case runs once with the fixed rules and once with statistics read from
real `pg_stats`. O1/O2 fail under the rules (the huge filter exceeds the 10,000-row
guard) and succeed under the cost-based plan; O3 shows the optimizer declining and the
query failing safely either way; O4 shows no change when the rules are already right.

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

## The generated matrix (`run_matrix.py`)

Queries are generated from the real data (values are sampled from the tables), run
through the full path, and compared row for row, in order, with a native SQL oracle.
Data: the small NULL-heavy `customer` dataset plus `order` (6,000 skewed orders across
`sales` / `payments` / `logistics`, ~80% / ~60% coverage, NULLs everywhere, orphans).

Every relational case runs under four planner configurations: `rules`, `stats`
(real `pg_stats` plus learned scan sizes), and both again with key transfer off.

| Category | What it covers |
| --- | --- |
| `filter` | every operator x every type x sampled values, on every field of both datasets |
| `boolean_tree` | random all / any / not trees, depth up to 3, over one to three sources |
| `cross_source` | every pair of source homes x all/any x negation |
| `order_page` | every field x asc/desc x page size x (no filter, anchor, contributor, both), plus two-key orders |
| `select`, `page`, `limits` | field subsets, ids-only, page sizes, an in-list at the 1,000 limit |
| `invalid_operator`, `invalid_query`, `unimplemented_operator` | every refused operator/type pair, bad queries, group_by/aggregate/distinct/join/union/traverse |
| `semantic`, `semantic_rules` | 540 combinations of proposition x filter x order x page x quality bar under verify-all, default and small shortlists; placement, limit and eligibility errors |
| `optimizer` | skewed bulk tables: rules vs statistics (statistics must never do worse) |

Known, deliberate gaps: ordering text with mixed case across several sources (the
coordinator and the database may collate differently) is not asserted.

## Robustness (`run_robustness.py`)

Through the public front door (`yodb.connect`), against the same database plus a
million-row table (`seed_scale.sql`, loaded after `seed.sql`):

```bash
docker exec -i yodb-e2e-pg psql -U postgres -d yodb_e2e -v ON_ERROR_STOP=1 -q < tests/e2e/seed_scale.sql
PYTHONPATH=src .venv/bin/python tests/e2e/run_robustness.py [--only hostile,timeouts,concurrent,scale] [-v]
```

| Section | What it checks |
| --- | --- |
| `hostile` | injection strings as values for every operator, injection through every name in a query, deep nesting, huge lists and strings, NaN, wrong types, non-JSON text, and that sessions are read-only even for a role that could write; each must give the right rows or a clean YoDb error, quickly, leaving the database unchanged |
| `timeouts` | a limit really stops a slow statement; a whole multi-source query shares one budget; an exhausted pool fails cleanly and recovers |
| `concurrent` | 800 queries from 32 threads over a pool of 4: every answer correct, no leaked or lingering connections |
| `scale` | pushdown, key transfer and the guards over 1,000,000 + 500,000 rows, with a peak-memory check |

## Joins (`run_joins.py`)

`buyer` (50,000 rows, its score in a second table) joined to `purchase` (300,000 rows, its carrier in a second
table) over the declared `buyer_purchases` relationship: four sources, four connection references. Load
`seed_scale.sql` first.

```bash
PYTHONPATH=src .venv/bin/python tests/e2e/run_joins.py [-v] [--seed N]
```

150 generated joins (random filters on both sides, inner and left, orderings, page sizes), each compared with the
equivalent native SQL join, with and without statistics: the rows must be identical or YoDb must refuse with a
guard error (never different rows). The report also counts joins a SQL join could answer that YoDb refused (an OR across sources, or a
side whose every read exceeds the row guard, are inherent), checks key batching, the reverse direction, the refusal of unfiltered joins
and the time limit.

## Real data, with the vectors in a separate database (`real/`)

13,083 real customer-support messages (BANKING77, 77 labelled intents) live on one Postgres
server; their embeddings, made by a real open model run locally, live on **another server**.
Queries ask natural-language propositions; the judge is the dataset's own intent label (a
perfect verifier), so what is measured is *retrieval*: does the shortlist from the vector store
find the true matches, how much verification does it save, and does the planner choose well.

```bash
pip install fastembed          # a small ONNX runtime; no PyTorch
docker run -d --name yodb-records-pg --tmpfs /var/lib/postgresql/data -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=records -p 127.0.0.1:55432:5432 pgvector/pgvector:pg17
docker run -d --name yodb-vectors-pg --tmpfs /var/lib/postgresql/data -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=vectors -p 127.0.0.1:55433:5432 pgvector/pgvector:pg17
# wait until both answer `pg_isready`, then download, embed and load (minishlab/potion-base-8M: 256 dims)
PYTHONPATH=src .venv/bin/python tests/e2e/real/load.py \
  --records "host=localhost port=55432 dbname=records user=postgres password=postgres" \
  --vectors "host=localhost port=55433 dbname=vectors user=postgres password=postgres" [--model NAME]
PYTHONPATH=src .venv/bin/python tests/e2e/real/run_realdata.py [-v] [--model NAME] [--report FILE]
docker rm -f yodb-records-pg yodb-vectors-pg
```

Engines compared on the same 96 queries (8 propositions x 6 filters x 2 page sizes): `A_exact` (verify
everything, the reference), `A_cap1000` (default limits), `B_rules` (fixed rules), `B_keys5k` (may send 5,000
IDs to a source), `B_wide` (also a larger shortlist), `B_stats` (cost-based, real `pg_stats`) and `B_fit`
(cost-based with a recall curve fitted to this model). The report ends with the model's measured recall
curve against the planner's assumed one.

Joins across **two servers** (`real/run_realjoins.py`, after loading as above): 2,000 customers on the vectors server,
their 13,083 support messages on the records server, so no SQL join can span them. The oracle reads both tables into
memory and joins them in Python; YoDb's join must match it exactly (inner, left, the messages side driving, the reverse
direction, key batches, and a semantic condition on the messages side).

```bash
PYTHONPATH=src .venv/bin/python tests/e2e/real/run_realjoins.py [-v]
```

