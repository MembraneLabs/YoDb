# YoDb

YoDb lets an app or an AI agent ask one question about data that lives in
several PostgreSQL databases. You do not write SQL, and your data is not copied
anywhere.

## The problem

A company's data is rarely in one place. Customers are in one database, their
billing plan in a second, their support tickets in a third. A question like
"which UK customers are on the pro plan?" needs two of them, and no single SQL
query can reach both.

Today you either copy everything into one warehouse first, or you write code
that queries each database and stitches the answers together. If an AI agent is
asking, it has to guess the table names and the join keys, and a wrong guess
gives a confident wrong answer.

## What YoDb does

You describe your data once, in three small YAML files. After that, YoDb treats
the three databases as one set of named datasets:

```text
$ yodb catalog catalog/

customer  - A customer of the shop.
  id         id          from crm
  name       string      from crm
  country    string      from crm
  plan       string      from billing
  signed_up  timestamp   from crm

ticket  - A support ticket.
  id           id        from support
  customer_id  id        from support
  subject      string    from support
  status       string    from support
  priority     int       from support
```

`customer` looks like one table, but `name` and `country` come from the `crm`
database and `plan` comes from `billing`. Now ask a question:

```bash
yodb query catalog/ '{
  "from": {"dataset": "customer"},
  "select": ["name", "country", "plan"],
  "where": {"field": "country", "op": "eq", "value": "UK"},
  "order_by": [{"field": "name", "direction": "asc"}]
}'
```

```text
id   name             country  plan
---  ---------------  -------  ----
c01  Ada Lovelace     UK       pro
c03  Alan Turing      UK       free
c10  Tim Berners-Lee  UK       pro
(3 rows)
```

YoDb asked `crm` for the UK customers, asked `billing` for the plans of those
three customers only, and put the rows together.

You can also follow a relationship between two datasets. This finds US
customers with open support tickets, most urgent first. The customers are in one
database and the tickets in another:

```bash
yodb query catalog/ '{
  "from": {"dataset": "customer"},
  "select": ["name"],
  "where": {"field": "country", "op": "eq", "value": "US"},
  "traverse": [{
    "relationship": "customer_has_ticket", "as": "ticket",
    "select": ["subject", "priority"],
    "where": {"field": "status", "op": "eq", "value": "open"}
  }],
  "order_by": [{"field": "ticket.priority", "direction": "desc"}, {"field": "name", "direction": "asc"}],
  "page": {"first": 3}
}'
```

```text
id   name               ticket.id  ticket.subject           ticket.priority
---  -----------------  ---------  -----------------------  ---------------
c07  Barbara Liskov     t11        Two-factor not working   5
c02  Grace Hopper       t03        App crashes on export    5
c05  Margaret Hamilton  t08        Data missing after sync  5
(3 rows)
```

## How it works

1. **You describe the data.** Three YAML files say which datasets exist, which
   table and column each field comes from, and how datasets relate. This is the
   *catalog*. YoDb checks it against the real databases and refuses to start if
   they do not match.
2. **You ask in terms of the catalog.** A query is a small piece of JSON that
   names datasets and fields. It never names a table, and it is never SQL.
3. **YoDb works out the rest.** It decides which databases to read and in what
   order, sends each one a filtered query, and joins the answers. `yodb explain`
   shows the plan without running it.

## Try it in five minutes

The sample in [examples/shop](examples/shop) is the three-database shop used
above. You need Docker and Python 3.11 or newer.

```bash
pip install .                # from a checkout of this repository
cd examples/shop
./setup.sh                   # starts PostgreSQL and prints three export lines; paste them
yodb validate catalog/       # checks the catalog against the databases
yodb query catalog/ '{"from": {"dataset": "customer"}, "select": ["name", "plan"]}'
```

[examples/shop/README.md](examples/shop/README.md) has more queries to try.

## Use it from Python

```python
import yodb

with yodb.connect("catalog/") as db:
    result = db.query({
        "from": {"dataset": "customer"},
        "select": ["name", "plan"],
        "where": {"field": "country", "op": "eq", "value": "UK"},
    })
    for row in result.rows:
        print(row["name"], row["plan"])
```

Connection strings stay out of the catalog. Each database is named by a
`connection_ref`, and YoDb reads its connection string from the environment
variable `YODB_CONN_<REF>`.

## Give it to an AI agent

```bash
pip install ".[mcp]"
yodb mcp catalog/
```

This serves the catalog over the Model Context Protocol (MCP), the standard way
to give an AI agent tools. The agent gets three tools: list the datasets, run a
query, and explain a query. It sees dataset and field names only. It cannot send
SQL, see table names, or write anything. See
[docs/reference/mcp.mdx](docs/reference/mcp.mdx).

## Why not let the agent write SQL?

Because a mistake in SQL is silent. A wrong join key or a wrong table still
returns rows. With YoDb:

- **Joins are declared by you, once.** The agent picks a relationship by name.
  It cannot invent one.
- **A mistake is refused, with a reason.** A misspelt field gives
  `error [field_not_found] at select[0]: Unknown field 'nam' on dataset
  'customer'.` SQL text is refused outright.
- **It cannot write.** Every database session is read-only.
- **It cannot run away.** Each query has a time limit and row limits. A query
  that would read too much is stopped and refused.
- **Nothing leaks.** Errors never contain passwords, SQL or data values.

## Filtering by meaning

A filter can also be a sentence. This keeps the tickets whose text is about a
late delivery, whatever words the customer used:

```json
{"semantic": {"field": "body", "proposition": "the customer is asking about a late delivery"}}
```

You supply the model that judges each row. YoDb can first narrow the candidates
with a vector search, and it enforces a cost and time budget. See
[docs/query/semantic-filter.mdx](docs/query/semantic-filter.mdx).

## What it cannot do yet

YoDb is at version 0.1. It is tested, but deliberately small:

- **PostgreSQL only.** No other database, and no SaaS APIs.
- **No counting or totals.** There is no `count`, `sum` or `group by`.
- **One relationship per query.** You can follow one `traverse` step.
- **One page of results.** Up to 500 rows per query, with no "next page".
- **Read-only.** There is no way to write data.
- **Bounded size.** Rows from different databases are joined in memory. A query
  that would need more than the row limits is refused.

The full list is in [docs/reference/limits.mdx](docs/reference/limits.mdx). Read
it before you rely on a query shape.

## When to use something else

- **Your data is in one database.** Use SQL.
- **You need reports, counts and totals over large data.** Use a data warehouse,
  or a federated SQL engine such as Trino.
- **Your data is in other systems** (MySQL, MongoDB, Salesforce). YoDb cannot
  read them yet.

## What you can rely on

[docs/reference/compatibility.mdx](docs/reference/compatibility.mdx) lists which
parts are stable (the query format, the catalog files, the error codes, the
`yodb` command) and which may still change.

## Documentation

- [Quickstart](docs/getting-started/quickstart.mdx): the sample shop, step by step
- [Concepts](docs/getting-started/overview.mdx): the handful of terms used everywhere
- [Writing a catalog](docs/catalog/overview.mdx)
- [The query format](docs/query/query-model.mdx) and [joins](docs/query/joins.mdx)
- [The command line](docs/reference/cli.mdx) and the [Python API](docs/reference/python.mdx)
- [Errors](docs/reference/errors.mdx) and [limits](docs/reference/limits.mdx)
- [Running YoDb](docs/operations/operating.mdx): database roles, secrets and time limits

The docs build to a static site with `python3 docs-site/build.py`. Design notes
are in [plans/](plans/).

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests     # unit tests, no database needed
```

The end-to-end suites run against a throwaway PostgreSQL in Docker and compare
every answer with plain SQL. See [tests/e2e/README.md](tests/e2e/README.md).
