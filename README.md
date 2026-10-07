# YoDb

YoDb is a **read-only, typed, explainable query layer over several databases**.
You describe your data once, in three YAML files; you then ask questions about
*logical* datasets, and YoDb works out which databases to read, in what order,
and how to put the answer together. It never copies or writes your data.

```bash
yodb query catalog/ '{
  "from": {"dataset": "order"},
  "select": ["status", "amount", "method", "carrier"],
  "where": {"all": [
    {"field": "status", "op": "in", "value": ["paid", "shipped"]},
    {"field": "method", "op": "eq", "value": "wire"}
  ]},
  "order_by": [{"field": "amount", "direction": "desc"}],
  "page": {"first": 5}
}'
```

`status` lives in a sales database, `method` in a payments database and
`carrier` in a logistics one. YoDb reads each, restricted by the IDs it has
already found, assembles the records, and `yodb explain` shows exactly how.

Datasets can also be **joined** over a relationship you declare, across databases:

```bash
yodb query catalog/ '{
  "from": {"dataset": "customer"}, "select": ["name"], "where": {"field": "country", "op": "eq", "value": "US"},
  "traverse": [{"relationship": "customer_has_ticket", "as": "ticket", "select": ["subject"],
                "where": {"field": "status", "op": "eq", "value": "open"}}],
  "page": {"first": 10}
}'
```

## What V0.1 does

- **A catalog you can trust.** Three YAML files map business datasets and fields
  to approved tables and columns. YoDb inspects the real databases and refuses to
  open a catalog that does not match them.
- **Typed queries across sources.** Filters (`eq`, `in`, ranges, text, `is_null`,
  `all`/`any`/`not`), ordering and paging over one dataset whose fields are spread
  over PostgreSQL databases, assembled by a logical ID.
- **Joins between datasets.** `traverse` follows a declared relationship between two
  datasets in different sources (inner or left, either direction for a bidirectional
  relationship): one row per matching pair, filters and ordering on either side, the
  smaller side driving and the other read in batches of keys.
- **A planner that explains itself.** Pushdown to each database, key transfer
  between sources, and a cost-based search over read orders using the databases'
  own statistics, with a stated fallback to fixed rules.
- **A semantic filter.** `{"semantic": {"field": "body", "proposition": "..."}}`:
  a natural-language condition judged by a verifier you supply, optionally
  shortlisted by vector search with the vectors in the same database or a separate
  one.
- **A server for AI agents.** `yodb mcp catalog/` serves the catalog over the Model
  Context Protocol: an agent can list the datasets, run typed queries and read plans,
  and nothing else. No SQL, no physical names, no writes.
- **Safe by construction.** Read-only sessions, parameterised values, every name
  checked against the catalog, bounded filters, one time budget per query, and
  errors that never contain credentials, SQL or values.

## Install

```bash
pip install .                 # the library and the `yodb` command (Python 3.11+)
pip install ".[semantic]"     # optional: a local embedding model for the semantic filter
pip install ".[mcp]"          # optional: the MCP server for AI agents (`yodb mcp`)
```

## Use

```bash
yodb catalog  catalog/                  # what can be queried (no database needed)
yodb validate catalog/                  # check the catalog against the real databases
yodb explain  catalog/ query.json       # the plan, without running it
yodb query    catalog/ @query.json      # run it
yodb mcp      catalog/                  # serve it to an AI agent (see docs/reference/mcp.mdx)
```

```python
import yodb

with yodb.connect("catalog/") as db:
    result = db.query(query, timeout_seconds=5)
    for row in result.rows:
        print(row)
```

Connection strings stay out of the catalog: each `connection_ref` is read from
the environment variable `YODB_CONN_<REF>`. Start with the
[quickstart](docs/getting-started/quickstart.mdx).

## Know the limits

V0.1 is a correct, tested core with deliberate limits: no cursor paging, no aggregation,
one join step per query, joins and assembly in memory under row guards, PostgreSQL only. They are listed in [docs/reference/limits.mdx](docs/reference/limits.mdx).
Read them before you rely on a query shape.

## Documentation

The documentation lives in [docs/](docs/) and builds to a static site with
[docs-site/](docs-site/) (`python3 docs-site/build.py`). Design notes and delivery
plans are in [plans/](plans/), starting with
[the V0.1 design](plans/v0.1-federated-semantic-data-layer.md),
[operators](plans/v0.1-operators.md) and
[the cost-based planner](plans/v0.1-cost-based-planning.md).

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests          # unit tests, no database needed
```

The end-to-end suites run against a throwaway PostgreSQL in Docker: 77
hand-written cases, about 8,800 generated queries compared with a native SQL
oracle, robustness checks (hostile input, timeouts, concurrency, a million rows),
the semantic filter on real data with the vectors on a second server, and the MCP
server driven by a real MCP client. See
[tests/e2e/README.md](tests/e2e/README.md).
