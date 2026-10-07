# YoDb sample: a shop across three databases

Customers live in `crm`, their billing plan in `billing`, their support tickets in `support`.
YoDb presents them as two datasets (`customer`, `ticket`) and can join them, although no SQL join
spans three databases.  (Here the three databases share one throwaway Postgres server; they could
as well be on three servers.)

Needs Docker and Python 3.11+.

## 1. Install (in a fresh virtual environment)

    python3 -m venv .venv && source .venv/bin/activate
    pip install "yodb @ https://github.com/MembraneLabs/YoDb/releases/download/v0.1.0/yodb-0.1.0-py3-none-any.whl"

## 2. Start the databases and export the connections

    cd examples/shop
    ./setup.sh            # prints three `export YODB_CONN_...` lines; paste them into your shell

## 3. Look around

    yodb catalog catalog/        # no database needed: the datasets, fields and where each comes from
    yodb validate catalog/       # checks the catalog against the real databases: all three "valid"

## 4. Query

A single dataset whose fields come from two databases (`plan` is in billing):

    yodb query catalog/ '{"from":{"dataset":"customer"},"select":["name","country","plan"],
      "where":{"field":"country","op":"eq","value":"UK"},"order_by":[{"field":"name","direction":"asc"}]}'

    c01  Ada Lovelace     UK  pro
    c03  Alan Turing      UK  free
    c10  Tim Berners-Lee  UK  pro

A join between datasets in different databases: US customers with their open tickets, most urgent first:

    yodb query catalog/ '{"from":{"dataset":"customer"},"select":["name","country"],
      "where":{"field":"country","op":"eq","value":"US"},
      "traverse":[{"relationship":"customer_has_ticket","as":"ticket","select":["subject","status","priority"],
                   "where":{"field":"status","op":"eq","value":"open"}}],
      "order_by":[{"field":"ticket.priority","direction":"desc"},{"field":"name","direction":"asc"}],
      "page":{"first":5}}'

    c07  Barbara Liskov     US  t11  Two-factor not working   open  5
    c02  Grace Hopper       US  t03  App crashes on export    open  5
    c05  Margaret Hamilton  US  t08  Data missing after sync  open  5
    c06  Dennis Ritchie     US  t19  Login loop on mobile     open  4
    c08  Donald Knuth       US  t13  Webhook fails            open  4

A left join (`"optional": true`) keeps customers with no match; here, customers with no plan at all:

    yodb query catalog/ '{"from":{"dataset":"customer"},"select":["name","plan"],
      "where":{"field":"plan","op":"is_null"},
      "traverse":[{"relationship":"customer_has_ticket","as":"ticket","select":["subject"],"optional":true}]}'

    c11  Hedy Lamarr         NULL  NULL  NULL
    c12  Katherine Johnson   NULL  t20   Welcome and thanks

See how a query will run, without running it (which side drives, what is pushed to each database):

    yodb explain catalog/ '{"from":{"dataset":"customer"},"select":["name"],
      "traverse":[{"relationship":"customer_has_ticket","as":"ticket","select":["subject"],
                   "where":{"field":"priority","op":"gte","value":5}}]}'

Add `--format json` (or `jsonl`) for machine-readable output.

Things to try: a typo in a field name (the error says what is valid), `"direction":"reverse"` to start
from `ticket` and traverse to its customer, changing `"first"` to a page of your own.

## 5. Give it to an AI agent

`yodb mcp` serves the same catalog over the Model Context Protocol (install the extra first:
`pip install "yodb[mcp]"`, or `pip install ".[mcp]"` from a checkout). In Claude Code, from this directory
and with the three variables exported:

    claude mcp add yodb-shop \
      -e YODB_CONN_CRM="$YODB_CONN_CRM" -e YODB_CONN_BILLING="$YODB_CONN_BILLING" -e YODB_CONN_SUPPORT="$YODB_CONN_SUPPORT" \
      -- yodb mcp "$PWD/catalog"

Then ask: "Which US customers have open tickets with priority 4 or more?" The agent calls
`describe_catalog`, writes the query, and can call `explain` to show how it ran. It cannot write, run SQL,
or see a table name.

## 6. Clean up

    ./teardown.sh
