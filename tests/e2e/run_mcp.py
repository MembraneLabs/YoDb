"""The MCP server, end to end: a real MCP client talks to `yodb mcp` (a subprocess, over standard
input/output) against real PostgreSQL, and every answer is compared with an independent oracle.

Two worlds:

* ``shop``: the sample in examples/shop (customers, billing plans and tickets in three databases).
* ``real``: 13,083 real support messages (BANKING77) on one server, 2,000 customers on another
  (see "Real data" in README.md), which no SQL join can span.

The oracle reads the tables with plain SQL and filters, joins, orders and pages them in Python.
A query must return exactly the oracle's rows, in order, or be refused by a row guard; it must
never return different rows.

    examples/shop/setup.sh                      # and the two real-data servers, loaded (README.md)
    PYTHONPATH=src .venv/bin/python tests/e2e/run_mcp.py [-v] [--only shop,real,errors,hostile,protocol,startup,semantic] [--seed N] [--report FILE]
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from functools import cmp_to_key
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile
import time

import psycopg

HERE = Path(__file__).parent
REPO = HERE.parent.parent
sys.path[:0] = [str(HERE / "real")]

from mcp import Client, StdioServerParameters                                  # noqa: E402

SHOP_PORT = int(os.environ.get("YODB_SHOP_PORT", "55440"))
RECORDS = "host=localhost port=55432 dbname=records user=yodb_ro password=yodb_ro"
VECTORS = "host=localhost port=55433 dbname=vectors user=yodb_ro password=yodb_ro"
GUARDS = ("query_row_limit_exceeded", "query_coordinator_limit_exceeded")
PYTHON = sys.executable
CODE = re.compile(r"\[([a-z_]+)\](?: at (\S+?))?(?: \(source [^)]*\))?: ")


def shop_conn(db: str) -> str:
    return f"host=localhost port={SHOP_PORT} dbname={db} user=yodb_ro password=yodb_ro"


def base_env() -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "PYTHONPATH": str(REPO / "src"), "HOME": os.environ.get("HOME", "")}


def shop_env() -> dict[str, str]:
    return {**base_env(), **{f"YODB_CONN_{db.upper()}": shop_conn(db) for db in ("crm", "billing", "support")}}


def real_env() -> dict[str, str]:
    return {**base_env(), "YODB_CONN_E2E_RECORDS": RECORDS, "YODB_CONN_E2E_VECTORS": VECTORS}


def real_catalog() -> str:
    directory = Path(tempfile.mkdtemp(prefix="yodb-mcp-real-"))
    for name in ("datasets", "sources", "relations"):
        text = (HERE / "real" / "catalog" / f"{name}.yaml").read_text()
        (directory / f"{name}.yaml").write_text(text.replace("@MODEL@", "minishlab/potion-base-8M").replace("@DIMS@", "256"))
    return str(directory)


def server(catalog: str, env: dict[str, str], *options: str) -> StdioServerParameters:
    return StdioServerParameters(command=PYTHON, args=["-m", "yodb", "mcp", catalog, *options], env=env)


# --- the oracle: plain Python over rows read with plain SQL ----------------------------------


def fetch(conninfo: str, sql: str, columns: tuple[str, ...]) -> list[dict]:
    with psycopg.connect(conninfo, options="-c TimeZone=UTC") as connection:
        return [dict(zip(columns, row)) for row in connection.execute(sql).fetchall()]


def shop_world() -> dict:
    customers = fetch(shop_conn("crm"), "SELECT customer_id, name, country, signed_up FROM customers", ("id", "name", "country", "signed_up"))
    plans = dict(row.values() for row in fetch(shop_conn("billing"), "SELECT customer_id, plan FROM subscriptions", ("id", "plan")))
    for customer in customers:
        customer["plan"] = plans.get(customer["id"])
        customer["signed_up"] = customer["signed_up"].replace(tzinfo=UTC)
    tickets = fetch(shop_conn("support"), "SELECT ticket_id, customer_id, subject, status, priority FROM tickets", ("id", "customer_id", "subject", "status", "priority"))
    return {
        "datasets": {
            "customer": {"rows": customers, "types": {"id": "id", "name": "string", "country": "string", "plan": "string", "signed_up": "timestamp"}},
            "ticket": {"rows": tickets, "types": {"id": "id", "customer_id": "id", "subject": "string", "status": "string", "priority": "int"}},
        },
        "relationship": ("customer_has_ticket", "customer", "id", "ticket", "customer_id"),
    }


def real_world() -> dict:
    customers = fetch(VECTORS, "SELECT customer_id, name, region, tier FROM crm.customers", ("id", "name", "region", "tier"))
    messages = fetch(
        RECORDS,
        "SELECT m.message_id, m.body, m.split, m.word_count, m.customer_id, a.owner, a.priority, t.intent "
        "FROM support.messages m LEFT JOIN triage.assignments a USING (message_id) JOIN support.truth t USING (message_id)",
        ("id", "body", "split", "word_count", "customer_id", "owner", "priority", "intent"),
    )
    return {
        "datasets": {
            "customer": {"rows": customers, "types": {"id": "id", "name": "string", "region": "string", "tier": "string"}},
            "message": {"rows": messages, "types": {"id": "id", "body": "text", "split": "string", "word_count": "int", "owner": "string", "priority": "int", "customer_id": "id"}},
        },
        "relationship": ("customer_messages", "customer", "id", "message", "customer_id"),
    }


def holds(condition, row):
    """SQL three-valued logic: True, False, or None for unknown."""

    if "all" in condition:
        values = [holds(c, row) for c in condition["all"]]
        return False if False in values else (None if None in values else True)
    if "any" in condition:
        values = [holds(c, row) for c in condition["any"]]
        return True if True in values else (None if None in values else False)
    if "not" in condition:
        value = holds(condition["not"], row)
        return None if value is None else not value
    actual, op, wanted = row[condition["field"]], condition["op"], condition.get("value")
    if op == "is_null":
        return actual is None
    if op == "is_not_null":
        return actual is not None
    if actual is None:
        return None
    if isinstance(actual, datetime):
        parse = lambda text: datetime.fromisoformat(text.replace("Z", "+00:00"))      # noqa: E731
        wanted = [parse(w) for w in wanted] if isinstance(wanted, list) else parse(wanted)
    return {
        "eq": lambda: actual == wanted, "ne": lambda: actual != wanted,
        "in": lambda: actual in wanted, "not_in": lambda: actual not in wanted,
        "gt": lambda: actual > wanted, "gte": lambda: actual >= wanted, "lt": lambda: actual < wanted, "lte": lambda: actual <= wanted,
        "contains": lambda: wanted in actual, "starts_with": lambda: actual.startswith(wanted),
    }[op]()


def compare(a, b, descending):
    if a is None and b is None:
        return 0
    if a is None:
        return -1 if descending else 1          # NULLs last ascending, first descending
    if b is None:
        return 1 if descending else -1
    if a == b:
        return 0
    result = -1 if a < b else 1
    return -result if descending else result


def ordered(rows, terms):
    def cmp(a, b):
        for column, descending in terms:
            result = compare(a.get(column), b.get(column), descending)
            if result:
                return result
        return 0

    return sorted(rows, key=cmp_to_key(cmp))


def shown(value):
    return value.isoformat() if isinstance(value, datetime) else value


def page_in_force(query: dict) -> int:
    """The page, bounded by ``constraints.maximum_results``."""

    first = query.get("page", {}).get("first", 100)
    return min(first, query.get("constraints", {}).get("maximum_results", first))


def expected(world: dict, query: dict) -> list[dict]:
    """What the query must return."""

    dataset = world["datasets"][query["from"]["dataset"]]
    public = [f for f in dataset["types"]]
    rows = [r for r in dataset["rows"] if "where" not in query or holds(query["where"], r) is True]
    columns = ["id", *(f for f in query.get("select", public) if f != "id")]
    terms = [(o["field"], o.get("direction", "asc") == "desc") for o in query.get("order_by", [])]
    first = page_in_force(query)
    if "traverse" not in query:
        if not terms or terms[-1][0] != "id":
            terms.append(("id", False))
        return [{c: shown(r[c]) for c in columns} for r in ordered(rows, terms)[:first]]

    step = query["traverse"][0]
    name, start, start_key, end, end_key = world["relationship"]
    reverse = step.get("direction") == "reverse"
    other_name, root_key, other_key = (start, end_key, start_key) if reverse else (end, start_key, end_key)
    other = world["datasets"][other_name]
    alias = step.get("as", name)
    by_key: dict = {}
    for candidate in other["rows"]:
        if candidate[other_key] is not None and ("where" not in step or holds(step["where"], candidate) is True):
            by_key.setdefault(candidate[other_key], []).append(candidate)
    pairs = []
    for row in rows:
        matches = by_key.get(row[root_key], []) if row[root_key] is not None else []
        if not matches and step.get("optional"):
            pairs.append({**row, **{f"{alias}.{f}": None for f in other["types"]}})
        pairs.extend({**row, **{f"{alias}.{f}": m[f] for f in other["types"]}} for m in matches)
    other_columns = [f"{alias}.id", *(f"{alias}.{f}" for f in step.get("select", list(other["types"])) if f != "id")]
    pairs = ordered(pairs, [*terms, ("id", False), (f"{alias}.id", False)])
    return [{c: shown(p[c]) for c in [*columns, *other_columns]} for p in pairs[:first]]


# --- generating queries from the data -----------------------------------------------------------


def leaf(field, op, value=None):
    condition = {"field": field, "op": op}
    if op not in ("is_null", "is_not_null"):
        condition["value"] = value
    return condition


def query_value(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(value, datetime) else value


def samples(dataset: dict, field: str, rng: random.Random, count: int = 3) -> list:
    values = sorted({r[field] for r in dataset["rows"] if r[field] is not None})
    return [query_value(v) for v in rng.sample(values, min(count, len(values)))]


def leaves(dataset: dict, rng: random.Random, *, text_ops: bool = True) -> list[dict]:
    """Every operator on every field it applies to, with values taken from the data."""

    made = []
    for field, kind in dataset["types"].items():
        values = samples(dataset, field, rng)
        absent = {"int": -12345, "timestamp": "1999-01-01T00:00:00Z"}.get(kind, "no-such-value")
        for value in (values[0], absent):
            made += [leaf(field, "eq", value), leaf(field, "ne", value)]
        made += [leaf(field, "in", values), leaf(field, "not_in", values), leaf(field, "in", [absent]),
                 leaf(field, "is_null"), leaf(field, "is_not_null")]
        if kind in ("int", "timestamp"):
            made += [leaf(field, op, value) for op in ("gt", "gte", "lt", "lte") for value in values[:2]]
        if kind in ("string", "text") and text_ops:
            word = str(values[0])
            made += [leaf(field, "starts_with", word[: max(1, len(word) // 2)]), leaf(field, "contains", word[1:4] or word),
                     leaf(field, "starts_with", "zzz-none"), leaf(field, "contains", word.upper() + "#")]
    return made


def tree(pool: list[dict], rng: random.Random, depth: int) -> dict:
    if depth == 0 or rng.random() < 0.3:
        return rng.choice(pool)
    shape = rng.choice(("all", "any", "not"))
    if shape == "not":
        return {"not": tree(pool, rng, depth - 1)}
    return {shape: [tree(pool, rng, depth - 1) for _ in range(rng.randint(2, 3))]}


def cases_for(world: dict, rng: random.Random, *, trees: int, joins: int) -> list[tuple[str, dict]]:
    cases: list[tuple[str, dict]] = []
    pools = {}
    for name, dataset in world["datasets"].items():
        pool = pools[name] = leaves(dataset, rng)
        fields = list(dataset["types"])
        orderable = [f for f, kind in dataset["types"].items() if kind != "text"]
        base = {"from": {"dataset": name}}
        cases.append(("select", base))                                           # every public field, default page
        cases += [("select", {**base, "select": rng.sample(fields, k), "page": {"first": 20}}) for k in (1, 2, len(fields))]
        cases += [("page", {**base, "select": [fields[1]], "page": {"first": n}}) for n in (1, 2, 100, 500)]
        cases += [("filter", {**base, "where": condition, "page": {"first": 500}}) for condition in pool]
        cases += [("boolean", {**base, "select": rng.sample(fields, 2), "where": tree(pool, rng, 3), "page": {"first": 200}}) for _ in range(trees)]
        for field in orderable:
            for direction in ("asc", "desc"):
                for first in (1, 7, 100):
                    cases.append(("order", {**base, "where": tree(pool, rng, 1), "order_by": [{"field": field, "direction": direction}], "page": {"first": first}}))
        for _ in range(12):
            keys = rng.sample(orderable, 2)
            cases.append(("order2", {**base, "select": keys, "where": tree(pool, rng, 1),
                                     "order_by": [{"field": k, "direction": rng.choice(("asc", "desc"))} for k in keys], "page": {"first": rng.choice((5, 50))}}))

    name, start, _, end, _ = world["relationship"]
    for index in range(joins):
        reverse = rng.random() < 0.35
        root, other = (end, start) if reverse else (start, end)
        alias = name if index == 0 else rng.choice(("x", "joined", other))      # the prefix defaults to the relationship name
        step: dict = {"relationship": name} if index == 0 else {"relationship": name, "as": alias}
        if reverse:
            step["direction"] = "reverse"
        elif rng.random() < 0.3:
            step["direction"] = "forward"
        if rng.random() < 0.8:
            step["where"] = tree(pools[other], rng, rng.choice((0, 1, 2)))
        if rng.random() < 0.7:
            step["select"] = rng.sample(list(world["datasets"][other]["types"]), 2)
        if rng.random() < 0.3:
            step["optional"] = True
        query: dict = {"from": {"dataset": root}, "select": rng.sample(list(world["datasets"][root]["types"]), 2), "traverse": [step],
                       "page": {"first": rng.choice((1, 10, 100, 500))}}
        if rng.random() < 0.8:
            query["where"] = tree(pools[root], rng, rng.choice((0, 1, 2)))
        if rng.random() < 0.7:
            choices = [f for f, k in world["datasets"][root]["types"].items() if k != "text"] + \
                      [f"{alias}.{f}" for f, k in world["datasets"][other]["types"].items() if k != "text"]
            query["order_by"] = [{"field": f, "direction": rng.choice(("asc", "desc"))} for f in rng.sample(choices, rng.choice((1, 2)))]
        cases.append(("join", query))
    # a result cap on about one query in six, of every family: it must bound the page whatever the plan
    for index, (family, query) in enumerate(cases):
        if rng.random() < 0.16:
            cases[index] = (family, {**query, "constraints": {"maximum_results": rng.choice((1, 3, 40))}})
    return cases


# --- running -------------------------------------------------------------------------------------


class Report:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.results: list[tuple[str, str, bool, str]] = []
        self.lines: list[str] = []
        self.notes: dict[str, dict[str, int]] = {}

    def say(self, line: str = "") -> None:
        print(line, flush=True)
        self.lines.append(line)

    def check(self, section: str, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((section, name, bool(ok), detail))
        if self.verbose or not ok:
            self.say(f"  {'PASS' if ok else 'FAIL'} [{section}] {name}" + (f"  -- {detail}" if detail else ""))
        return bool(ok)

    def count(self, section: str, key: str) -> None:
        bucket = self.notes.setdefault(section, {})
        bucket[key] = bucket.get(key, 0) + 1


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content)


def code_of(result) -> tuple[str | None, str | None]:
    match = CODE.search(text_of(result))
    return (match.group(1), match.group(2)) if match else (None, None)


async def compare_cases(report: Report, section: str, client: Client, world: dict, cases: list[tuple[str, dict]]) -> None:
    started = time.perf_counter()
    for family, query in cases:
        label = f"{family}: {json.dumps(query)[:230]}"
        want = expected(world, query)
        result = await client.call_tool("query", {"query": query})
        explained = await client.call_tool("explain", {"query": query})
        if result.is_error:
            code, _ = code_of(result)
            if code in GUARDS:
                report.count(section, f"{family}: refused by a row guard ({code})")
                report.check(section, label, True, f"refused: {code}")
            else:
                report.check(section, label, False, f"error instead of {len(want)} rows: {text_of(result)[:300]}")
            continue
        got = result.structured_content["rows"]
        ok = got == want and result.structured_content["row_count"] == len(got)
        detail = f"{len(got)} rows" if ok else f"{len(got)} rows vs oracle {len(want)}; first difference: " + next(
            (f"#{i} got {g} want {w}" for i, (g, w) in enumerate(zip(got, want)) if g != w), "length only")
        report.check(section, label, ok, detail)
        report.count(section, f"{family}: rows identical to the oracle" if ok else f"{family}: WRONG ROWS")
        full = len(got) >= page_in_force(query)
        if ("note" in result.structured_content) != full:
            report.check(section, "page-full note " + label, False, f"note present={'note' in result.structured_content}, page full={full}")
        if explained.is_error or not explained.structured_content["steps"]:
            report.check(section, "explain " + label, False, text_of(explained)[:300])
        elif "traverse" in query and explained.structured_content["steps"][-1]["kind"] != "hash_join":
            report.check(section, "explain shows the join " + label, False, str(explained.structured_content["steps"][-1]))
        else:
            report.count(section, "explain: a plan for the same query")
    report.say(f"  {section}: {len(cases)} queries (each also explained) in {time.perf_counter() - started:.1f}s")


async def run_world(report: Report, section: str, params: StdioServerParameters, world: dict, rng: random.Random, *, trees: int, joins: int) -> None:
    async with Client(params) as client:
        described = (await client.call_tool("describe_catalog", {})).structured_content
        for name, dataset in world["datasets"].items():
            listed = {f: spec["type"] for f, spec in described["datasets"][name]["fields"].items()}
            report.check(section, f"describe_catalog lists {name} with its fields and types", listed == dataset["types"], str(listed))
        relationship = described["relationships"][world["relationship"][0]]
        report.check(section, "describe_catalog lists the relationship as traversable both ways",
                     relationship["traversable"] and relationship["reversible"], str(relationship))
        dumped = json.dumps(described)
        report.check(section, "describe_catalog shows no physical name or connection", not any(
            word in dumped for word in ("physical_name", "connection_ref", "password", "yodb_ro", "host=", "public.", "support.messages", "crm.customers")))
        await compare_cases(report, section, client, world, cases_for(world, rng, trees=trees, joins=joins))


SHOP_FIXED = [
    ("the README's UK customers", {"from": {"dataset": "customer"}, "select": ["name"], "where": leaf("country", "eq", "UK")},
     [{"id": "c01", "name": "Ada Lovelace"}, {"id": "c03", "name": "Alan Turing"}, {"id": "c10", "name": "Tim Berners-Lee"}]),
    ("US customers' open tickets by priority (a join over two databases)",
     {"from": {"dataset": "customer"}, "select": ["name"], "where": leaf("country", "eq", "US"),
      "traverse": [{"relationship": "customer_has_ticket", "as": "ticket", "select": ["priority"], "where": leaf("status", "eq", "open")}],
      "order_by": [{"field": "ticket.priority", "direction": "desc"}, {"field": "name", "direction": "asc"}], "page": {"first": 5}},
     [{"id": "c07", "name": "Barbara Liskov", "ticket.id": "t11", "ticket.priority": 5},
      {"id": "c02", "name": "Grace Hopper", "ticket.id": "t03", "ticket.priority": 5},
      {"id": "c05", "name": "Margaret Hamilton", "ticket.id": "t08", "ticket.priority": 5},
      {"id": "c06", "name": "Dennis Ritchie", "ticket.id": "t19", "ticket.priority": 4},
      {"id": "c08", "name": "Donald Knuth", "ticket.id": "t13", "ticket.priority": 4}]),
    ("customers with no plan, with their tickets or none (fields from three databases, a left join)",
     {"from": {"dataset": "customer"}, "select": ["name", "plan"], "where": leaf("plan", "is_null"),
      "traverse": [{"relationship": "customer_has_ticket", "as": "ticket", "select": ["subject"], "optional": True}]},
     None),
]


async def run_shop_fixed(report: Report, params: StdioServerParameters, world: dict) -> None:
    async with Client(params) as client:
        for name, query, rows in SHOP_FIXED:
            result = await client.call_tool("query", {"query": query})
            got = None if result.is_error else result.structured_content["rows"]
            report.check("shop", name, got == expected(world, query) and (rows is None or got == rows), text_of(result)[:200] if result.is_error else f"{len(got)} rows")
        timestamp = await client.call_tool("query", {"query": {"from": {"dataset": "customer"}, "select": ["signed_up"], "where": leaf("id", "eq", "c01")}})
        report.check("shop", "a timestamp comes back as RFC 3339 text", timestamp.structured_content["rows"] == [{"id": "c01", "signed_up": "2024-01-10T09:00:00+00:00"}],
                     str(timestamp.structured_content))
        as_text = await client.call_tool("query", {"query": json.dumps(SHOP_FIXED[0][1])})
        report.check("shop", "the query may be sent as JSON text", as_text.structured_content["rows"] == SHOP_FIXED[0][2])


# --- errors --------------------------------------------------------------------------------------

C, T = {"dataset": "customer"}, {"dataset": "ticket"}
STEP = {"relationship": "customer_has_ticket", "as": "ticket"}
ERRORS = [
    # name, query, code, a part of the location (or None)
    ("an unknown dataset", {"from": {"dataset": "orders"}}, "dataset_not_found", "from.dataset"),
    ("an unknown field in select", {"from": C, "select": ["name", "email"]}, "field_not_found", "select[1]"),
    ("an unknown field in where", {"from": C, "where": leaf("email", "eq", "x")}, "field_not_found", "where"),
    ("an unknown field nested in where", {"from": C, "where": {"all": [leaf("country", "eq", "US"), {"any": [leaf("email", "eq", "x")]}]}}, "field_not_found", "where.all[1].any[0]"),
    ("an unknown field in order_by", {"from": C, "order_by": [{"field": "email", "direction": "asc"}]}, "field_not_found", "order_by[0]"),
    ("an order term without a direction", {"from": C, "order_by": [{"field": "name"}]}, "query_shape_invalid", "order_by[0].direction"),
    ("a physical column name", {"from": C, "select": ["customer_id"]}, "field_not_found", "select[0]"),
    ("a physical table name", {"from": {"dataset": "customers"}}, "dataset_not_found", "from.dataset"),
    ("text for an int field", {"from": T, "where": leaf("priority", "eq", "3")}, "query_value_type_invalid", "where"),
    ("a number for a string field", {"from": C, "where": leaf("country", "eq", 3)}, "query_value_type_invalid", "where"),
    ("true for an int field", {"from": T, "where": leaf("priority", "eq", True)}, "query_value_type_invalid", "where"),
    ("a float for an int field", {"from": T, "where": leaf("priority", "gte", 2.5)}, "query_value_type_invalid", "where"),
    ("a timestamp without an offset", {"from": C, "where": leaf("signed_up", "gte", "2024-05-01 00:00")}, "query_value_type_invalid", "where"),
    ("a wrong value inside an in-list", {"from": T, "where": leaf("priority", "in", [1, "2"])}, "query_value_type_invalid", "where"),
    ("in without a list", {"from": T, "where": leaf("priority", "in", 3)}, "query_value_type_invalid", "where"),
    ("eq null", {"from": C, "where": leaf("plan", "eq", None)}, "query_value_type_invalid", "where"),
    ("a range operator on a string", {"from": C, "where": leaf("name", "gt", "M")}, "query_operator_not_supported", "where"),
    ("contains on an int", {"from": T, "where": leaf("priority", "contains", 3)}, "query_operator_not_supported", "where"),
    ("starts_with on an id", {"from": C, "where": leaf("id", "starts_with", "c0")}, "query_operator_not_supported", "where"),
    ("an unknown operator", {"from": C, "where": leaf("name", "like", "A%")}, "query_operator_not_supported", "where"),
    ("an empty all", {"from": C, "where": {"all": []}}, "query_expression_invalid", "where"),
    ("a condition with two shapes", {"from": C, "where": {"all": [leaf("country", "eq", "US")], "any": [leaf("country", "eq", "UK")]}}, "query_expression_invalid", "where"),
    ("a value for is_null", {"from": C, "where": {"field": "plan", "op": "is_null", "value": 1}}, "query_value_type_invalid", "where.value"),
    ("no from", {"select": ["name"]}, "query_shape_invalid", "from"),
    ("an unknown top-level key", {"from": C, "limit": 5}, "query_shape_invalid", None),
    ("select that is not a list", {"from": C, "select": "name"}, "query_shape_invalid", "select"),
    ("SQL instead of a query", "SELECT * FROM customers", "query_shape_invalid", None),
    ("broken JSON", '{"from": {"dataset": "customer"', "query_shape_invalid", None),
    ("a bad order direction", {"from": C, "order_by": [{"field": "name", "direction": "up"}]}, "query_shape_invalid", "order_by[0]"),
    ("a page of zero", {"from": C, "page": {"first": 0}}, "query_limit_invalid", "page.first"),
    ("a page over the maximum", {"from": C, "page": {"first": 501}}, "query_limit_invalid", "page.first"),
    ("a negative page", {"from": C, "page": {"first": -1}}, "query_limit_invalid", "page.first"),
    ("an in-list over 1,000 values", {"from": C, "where": leaf("id", "in", [f"c{n}" for n in range(1001)])}, "query_limit_invalid", "where"),
    ("a next page", {"from": C, "page": {"first": 2, "after": "cursor"}}, "query_shape_invalid", "page"),
    ("group_by", {"from": C, "group_by": ["country"]}, "query_shape_invalid", None),
    ("aggregate", {"from": C, "aggregate": [{"count": "id"}]}, "query_shape_invalid", None),
    ("distinct", {"from": C, "select": ["country"], "distinct": True}, "query_shape_invalid", None),
    ("two traverse steps", {"from": C, "traverse": [STEP, {**STEP, "as": "again"}]}, "query_feature_not_supported", "traverse"),
    ("a semantic condition, with no semantic filter configured", {"from": T, "where": {"semantic": {"field": "subject", "proposition": "about money"}}}, None, "where"),
    ("an unknown relationship", {"from": C, "traverse": [{"relationship": "customer_orders"}]}, "relationship_not_found", "traverse[0]"),
    ("a relationship that does not start here", {"from": T, "traverse": [STEP]}, "relationship_not_applicable", "traverse[0]"),
    ("an unknown field on the joined side", {"from": C, "traverse": [{**STEP, "select": ["email"]}]}, "field_not_found", "traverse[0]"),
    ("a root field named with the join prefix", {"from": C, "traverse": [STEP], "order_by": [{"field": "ticket.nope", "direction": "asc"}]}, "field_not_found", "order_by"),
    ("a wrong type on the joined side", {"from": C, "traverse": [{**STEP, "where": leaf("priority", "eq", "high")}]}, "query_value_type_invalid", "traverse[0]"),
    ("a join prefix with a dot", {"from": C, "traverse": [{**STEP, "as": "a.b"}]}, "query_shape_invalid", "traverse[0]"),
    ("an unknown key in a step", {"from": C, "traverse": [{**STEP, "on": "id = customer_id"}]}, "query_shape_invalid", "traverse[0]"),
]


async def run_errors(report: Report, params: StdioServerParameters, real: StdioServerParameters, world: dict) -> None:
    async with Client(params) as client:
        for name, query, code, location in ERRORS:
            for tool in ("query", "explain"):
                result = await client.call_tool(tool, {"query": query})
                got_code, got_location = code_of(result)
                ok = result.is_error and got_code is not None and (code is None or got_code == code) and (location is None or (got_location or "").startswith(location))
                report.check("errors", f"{tool}: {name}", ok, text_of(result).replace("\n", " ")[:220])
                leaked = [w for w in ("yodb_ro", "host=", "SELECT ", "public.", "psycopg", "Traceback") if w in text_of(result)]
                if leaked and not isinstance(query, str):
                    report.check("errors", f"{tool}: {name} leaks nothing", False, str(leaked))
        report.check("errors", "a call without its argument is refused by the protocol layer", (await client.call_tool("query", {})).is_error)
        try:
            unknown = await client.call_tool("drop_table", {"name": "customers"})
            report.check("errors", "an unknown tool is refused", unknown.is_error, text_of(unknown)[:120])
        except Exception as error:   # noqa: BLE001 - a protocol error is a refusal too
            report.check("errors", "an unknown tool is refused", True, type(error).__name__)
        report.check("errors", "the server still answers after every error",
                     (await client.call_tool("query", {"query": {"from": C, "page": {"first": 1}}})).structured_content["row_count"] == 1)
    async with Client(real) as client:
        reverse = await client.call_tool("query", {"query": {"from": {"dataset": "message"}, "where": leaf("id", "eq", "x"), "traverse": [{"relationship": "message_reference", "direction": "reverse"}]}})
        report.check("errors", "reverse over a one-way relationship", code_of(reverse)[0] == "relationship_not_applicable", text_of(reverse)[:200])
        for name, query in (
            ("every customer with every message (2,000 x 13,083, no filter), first page", {"from": {"dataset": "customer"}, "select": ["name"], "traverse": [{"relationship": "customer_messages", "as": "m", "select": ["word_count"]}]}),
            ("every message with its customer, from the messages' end, first page", {"from": {"dataset": "message"}, "select": ["split"], "traverse": [{"relationship": "customer_messages", "as": "c", "direction": "reverse", "select": ["name"]}]}),
        ):
            big = await client.call_tool("query", {"query": query})
            report.check("errors", f"an unfiltered join is answered exactly or refused by a guard: {name}",
                         code_of(big)[0] in GUARDS if big.is_error else big.structured_content["rows"] == expected(world, query), text_of(big)[:160].replace("\n", " "))
        wide = await client.call_tool("query", {"query": {"from": {"dataset": "message"}, "select": ["owner"], "where": leaf("body", "contains", "card")}})
        report.check("errors", "a text search over 13,083 rows answers or is refused by a guard, cleanly",
                     (not wide.is_error) or code_of(wide)[0] in GUARDS, text_of(wide)[:160])


# --- hostile input -------------------------------------------------------------------------------

INJECTIONS = [
    "'; DROP TABLE customers; --", "x' OR '1'='1", "US'; UPDATE customers SET name='pwned'; --", "\\'; SELECT pg_sleep(10); --",
    '"; DROP TABLE tickets; --', "$$; DELETE FROM customers; $$", "UK' UNION SELECT usename, passwd FROM pg_shadow --", "%", "_", "\\", "''", "Robert'); DROP TABLE students;--",
    "😀 ünïcödé", "a" * 5000,
]


async def run_hostile(report: Report, params: StdioServerParameters, world: dict) -> None:
    def counts() -> tuple:
        return tuple(sorted(
            (name, len(fetch(shop_conn(db), f"SELECT * FROM {table}", ("a",))))
            for db, table, name in (("crm", "customers", "customers"), ("billing", "subscriptions", "subscriptions"), ("support", "tickets", "tickets"))))

    before, snapshot = counts(), json.dumps(fetch(shop_conn("crm"), "SELECT customer_id, name FROM customers ORDER BY 1", ("id", "name")))
    async with Client(params) as client:
        for text in INJECTIONS:
            short = text[:40]
            for op in ("eq", "ne", "contains", "starts_with", "in", "not_in"):
                query = {"from": C, "select": ["name"], "where": leaf("name", op, [text] if op in ("in", "not_in") else text)}
                result = await client.call_tool("query", {"query": query})
                ok = (not result.is_error) and result.structured_content["rows"] == expected(world, query)
                report.check("hostile", f"an injection string as a {op} value is only a value: {short!r}", ok, text_of(result)[:160])
            for name, query in (
                ("dataset name", {"from": {"dataset": text}}),
                ("field name", {"from": C, "select": [text]}),
                ("filter field", {"from": C, "where": leaf(text, "eq", "x")}),
                ("order field", {"from": C, "order_by": [{"field": text, "direction": "asc"}]}),
                ("relationship name", {"from": C, "traverse": [{"relationship": text}]}),
                ("join prefix", {"from": C, "traverse": [{**STEP, "as": text}]}),
                ("operator", {"from": C, "where": leaf("name", text, "x")}),
            ):
                result = await client.call_tool("query", {"query": query})
                report.check("hostile", f"an injection string as a {name} is refused with a code: {short!r}", result.is_error and code_of(result)[0] is not None, text_of(result)[:160])
        deep: dict = leaf("country", "eq", "US")
        for _ in range(400):
            deep = {"not": deep}
        # as JSON text: the client library itself refuses to send an object nested this deep
        result = await client.call_tool("query", {"query": json.dumps({"from": C, "where": deep})})
        report.check("hostile", "a filter nested 400 deep is refused with a code", result.is_error and code_of(result)[0] is not None, text_of(result)[:160])
        huge = await client.call_tool("query", {"query": {"from": C, "where": leaf("name", "eq", "x" * 2_000_000)}})
        report.check("hostile", "a two-megabyte value answers or is refused, cleanly", (not huge.is_error) or code_of(huge)[0] is not None, text_of(huge)[:120])
        for name, argument in (("a number", 7), ("null", None), ("a list", [{"from": C}]), ("an empty object", {}), ("NaN text", "NaN")):
            try:
                result = await client.call_tool("query", {"query": argument})
                report.check("hostile", f"{name} as the query is refused", result.is_error, text_of(result)[:120])
            except Exception as error:   # noqa: BLE001
                report.check("hostile", f"{name} as the query is refused", True, type(error).__name__)
        alive = await client.call_tool("query", {"query": SHOP_FIXED[0][1]})
        report.check("hostile", "the server is still correct afterwards", alive.structured_content["rows"] == SHOP_FIXED[0][2])
    report.check("hostile", "no table lost or gained a row", counts() == before, str(counts()))
    report.check("hostile", "no customer was changed", json.dumps(fetch(shop_conn("crm"), "SELECT customer_id, name FROM customers ORDER BY 1", ("id", "name"))) == snapshot)


# --- the protocol and the process ----------------------------------------------------------------


async def run_protocol(report: Report, shop: StdioServerParameters, real: StdioServerParameters, shop_rows: dict, real_rows: dict) -> None:
    async with Client(shop) as client:
        tools = (await client.list_tools()).tools
        report.check("protocol", "the server lists describe_catalog, query and explain", [t.name for t in tools] == ["describe_catalog", "query", "explain"])
        report.check("protocol", "every tool is marked read-only and not destructive", all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools))
        report.check("protocol", "the server gives instructions", "describe_catalog first" in (client.instructions or ""))
        guide = next(t for t in tools if t.name == "query").description
        report.check("protocol", "the query tool's description teaches the query language",
                     all(word in guide for word in ("traverse", "order_by", "is_null", "starts_with", "at most 500", "Not available", "none is configured")))
        report.check("protocol", "the server names itself", client.server_info is not None and client.server_info.name == "yodb", str(client.server_info))
        first = await client.call_tool("describe_catalog", {})
        report.check("protocol", "an answer is structured content and the same JSON as text", json.loads(text_of(first)) == first.structured_content)
        queries = [{"from": C, "select": ["name"], "where": leaf("id", "eq", f"c{n:02d}")} for n in range(1, 13)] * 5
        started = time.perf_counter()
        results = await asyncio.gather(*(client.call_tool("query", {"query": q}) for q in queries))
        report.check("protocol", "60 calls at once in one session: every answer is its own",
                     all(r.structured_content["rows"] == expected(shop_rows, q) for r, q in zip(results, queries)), f"{time.perf_counter() - started:.2f}s")
        for index in range(200):
            query = {"from": T, "select": ["subject"], "where": leaf("priority", "eq", index % 5 + 1)}
            if (await client.call_tool("query", {"query": query})).structured_content["rows"] != expected(shop_rows, query):
                report.check("protocol", "200 calls one after another in one session", False, f"call {index}")
                break
        else:
            report.check("protocol", "200 calls one after another in one session", True)

    async def one_session(params, world, query):
        async with Client(params) as client:
            answers = await asyncio.gather(*(client.call_tool("query", {"query": query}) for _ in range(5)))
            return all(a.structured_content["rows"] == expected(world, query) for a in answers)

    both = await asyncio.gather(
        one_session(shop, shop_rows, {"from": C, "select": ["name"], "where": leaf("country", "eq", "US")}),
        one_session(real, real_rows, {"from": {"dataset": "customer"}, "select": ["name"], "where": leaf("region", "eq", "r3"), "page": {"first": 50}}),
        one_session(shop, shop_rows, {"from": T, "select": ["subject"], "where": leaf("status", "eq", "open")}),
    )
    report.check("protocol", "three servers at once (two catalogs), each correct", all(both))

    async with Client(server(shop.args[3], shop.env, "--timeout", "0.000001")) as client:
        slow = await client.call_tool("query", {"query": {"from": C, "select": ["name", "plan"]}})
        report.check("protocol", "--timeout bounds a query: query_timeout", code_of(slow)[0] == "query_timeout", text_of(slow)[:160])
        report.check("protocol", "and the server still describes its catalog", not (await client.call_tool("describe_catalog", {})).is_error)
    async with Client(server(shop.args[3], shop.env, "--no-statistics")) as client:
        plain = await client.call_tool("explain", {"query": SHOP_FIXED[1][1]})
        rows = await client.call_tool("query", {"query": SHOP_FIXED[1][1]})
        report.check("protocol", "--no-statistics plans by the fixed rules, with the same rows",
                     "strategy=rules" in plain.structured_content["optimizer"] and rows.structured_content["rows"] == SHOP_FIXED[1][2], str(plain.structured_content["optimizer"]))
    async with Client(real) as client:
        costed = await client.call_tool("explain", {"query": {
            "from": {"dataset": "customer"}, "select": ["name"], "traverse": [{"relationship": "customer_messages", "as": "m", "where": leaf("word_count", "gte", 70)}]}})
        report.check("protocol", "with statistics the join's plan is cost-based and names its driver",
                     any(o.startswith("strategy=cost_based") for o in costed.structured_content["optimizer"]) and any(o.startswith("driver=") for o in costed.structured_content["optimizer"]),
                     str(costed.structured_content["optimizer"]))


def start(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([PYTHON, "-m", "yodb", "mcp", *args], env=env, input=b"", capture_output=True, timeout=60)


def run_startup(report: Report, shop_catalog: str) -> None:
    def refused(name: str, done: subprocess.CompletedProcess, status: int, needle: str) -> None:
        err = done.stderr.decode()
        ok = done.returncode == status and done.stdout == b"" and needle in err and "Traceback" not in err and "password" not in err
        report.check("startup", name, ok, f"exit {done.returncode}; stdout {len(done.stdout)} bytes; stderr: {err.strip()[:200]}")

    refused("a missing catalog directory: exit 1, nothing on standard output", start(["/no/such/catalog"], shop_env()), 1, "catalog_load_failed")
    refused("a connection reference with no variable: exit 1", start([shop_catalog], base_env()), 1, "connection_reference_not_found")
    down = {**shop_env(), "YODB_CONN_CRM": "host=localhost port=1 dbname=crm user=yodb_ro password=yodb_ro connect_timeout=3"}
    refused("a database that is down: exit 1, named", start([shop_catalog], down), 1, "crm")
    wrong = {**shop_env(), "YODB_CONN_BILLING": shop_conn("billing").replace("password=yodb_ro", "password=nope")}
    refused("a wrong password: exit 1, and the password is not printed", start([shop_catalog], wrong), 1, "billing")
    mismatch = Path(tempfile.mkdtemp(prefix="yodb-mcp-bad-"))
    for name in ("datasets", "sources", "relations"):
        text = (Path(shop_catalog) / f"{name}.yaml").read_text()
        (mismatch / f"{name}.yaml").write_text(text.replace("physical_name: country", "physical_name: nation") if name == "sources" else text)
    refused("a catalog that does not match the database: exit 1, with what is wrong", start([str(mismatch)], shop_env()), 1, "source_validation_failed")
    refused("an unreadable connections file: exit 2", start([shop_catalog, "--connections", "/no/such/file"], shop_env()), 2, "connections file")
    connections = Path(tempfile.mkdtemp(prefix="yodb-mcp-conn-")) / "connections.yaml"
    connections.write_text("".join(f'{db}: "{shop_conn(db)}"\n' for db in ("crm", "billing", "support")))
    done = start([shop_catalog, "--connections", str(connections)], base_env())
    report.check("startup", "--connections FILE opens the catalog; the server ends cleanly when its input closes",
                 done.returncode == 0 and done.stdout == b"" and "serving catalog shop v1" in done.stderr.decode(), f"exit {done.returncode}: {done.stderr.decode().strip()[:200]}")
    module = subprocess.run([PYTHON, "-m", "yodb", "mcp", "--help"], env=base_env(), capture_output=True, timeout=30)
    report.check("startup", "yodb mcp --help", module.returncode == 0 and b"--timeout" in module.stdout and b"--connections" in module.stdout)


# --- the semantic filter, served from Python -----------------------------------------------------


async def run_semantic(report: Report, world: dict) -> None:
    from providers import PROPOSITIONS

    messages = world["datasets"]["message"]["rows"]
    script = str(HERE / "real" / "serve_mcp_semantic.py")
    flavours = {
        "split = test": leaf("split", "eq", "test"),
        "owner = ann and at least 12 words": {"all": [leaf("owner", "eq", "ann"), leaf("word_count", "gte", 12)]},
        "priority >= 4": leaf("priority", "gte", 4),
        "no other filter": None,
    }

    def truth(proposition, condition, first):
        rows = [m for m in messages if m["intent"] in PROPOSITIONS[proposition] and (condition is None or holds(condition, m) is True)]
        return [{"id": m["id"], "body": m["body"]} for m in ordered(rows, [("id", False)])[:first]], {m["id"] for m in rows}

    def ask(proposition, condition, first):
        semantic = {"semantic": {"field": "body", "proposition": proposition}}
        return {"from": {"dataset": "message"}, "select": ["body"], "where": semantic if condition is None else {"all": [condition, semantic]}, "page": {"first": first}}

    async with Client(StdioServerParameters(command=PYTHON, args=[script, "exact"], env=base_env())) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        report.check("semantic", "with a semantic filter the guide explains the condition", '"proposition"' in tools["query"].description and "none is configured" not in tools["query"].description)
        described = (await client.call_tool("describe_catalog", {})).structured_content
        report.check("semantic", "and the catalog marks the eligible field", described["datasets"]["message"]["fields"]["body"].get("semantic") is True
                     and "semantic" not in described["datasets"]["message"]["fields"]["split"])
        for proposition in PROPOSITIONS:
            for name, condition in flavours.items():
                for first in (10, 100):
                    want, _ = truth(proposition, condition, first)
                    result = await client.call_tool("query", {"query": ask(proposition, condition, first)})
                    label = f"exact: '{proposition}' with {name}, first {first}"
                    if result.is_error:
                        code = code_of(result)[0]
                        report.check("semantic", label, code in (*GUARDS, "query_semantic_budget_exceeded"), text_of(result)[:200])
                        report.count("semantic", f"refused by a guard or the budget ({code})")
                        continue
                    got = result.structured_content
                    ok = got["rows"] == want and got["semantic"]["exact"] is True and got["semantic"]["plan"] == "verify_all"
                    report.check("semantic", label, ok, f"{len(got['rows'])} rows vs truth {len(want)}; {got.get('semantic')}")
                    report.count("semantic", "exact: rows identical to the labelled truth" if ok else "exact: WRONG")
        proposition = "the customer is asking when their card will arrive"
        joined = {"from": {"dataset": "customer"}, "select": ["name"], "where": leaf("region", "eq", "r5"),
                  "traverse": [{"relationship": "customer_messages", "as": "message", "select": ["body"], "where": {"semantic": {"field": "body", "proposition": proposition}}}],
                  "page": {"first": 200}}
        result = await client.call_tool("query", {"query": joined})
        want = expected(world, {**joined, "traverse": [{**joined["traverse"][0], "where": leaf("intent", "in", sorted(PROPOSITIONS[proposition]))}]})
        report.check("semantic", "a semantic condition on the joined side, across two servers", (not result.is_error) and result.structured_content["rows"] == want,
                     text_of(result)[:200] if result.is_error else f"{len(want)} rows")
        capped = {**ask(proposition, flavours["split = test"], 500), "constraints": {"maximum_results": 5}}
        result = await client.call_tool("query", {"query": capped})
        report.check("semantic", "a result cap bounds a semantic query, and the note says the page is full",
                     (not result.is_error) and result.structured_content["rows"] == truth(proposition, flavours["split = test"], 5)[0] and "note" in result.structured_content,
                     text_of(result)[:160] if result.is_error else f"{result.structured_content['row_count']} rows")
        # a left join from all 2,000 customers probes the messages in two batches of keys: the report must add them up
        batched = {"from": {"dataset": "customer"}, "select": ["name"],
                   "traverse": [{"relationship": "customer_messages", "as": "message", "select": ["split"], "optional": True,
                                 "where": {"semantic": {"field": "body", "proposition": proposition}}}], "page": {"first": 500}}
        result = await client.call_tool("query", {"query": batched})
        matching = sum(m["intent"] in PROPOSITIONS[proposition] for m in messages)
        counts = None if result.is_error else result.structured_content["semantic"]
        report.check("semantic", "a join's semantic report counts every batch of keys, not only the last",
                     counts is not None and counts["records_that_qualified"] == matching and counts["records_judged"] == len(messages),
                     text_of(result)[:200] if result.is_error else f"{counts}; truth: {matching} of {len(messages)} messages")
        explained = await client.call_tool("explain", {"query": ask(proposition, flavours["split = test"], 10)})
        report.check("semantic", "explain shows the semantic step", (not explained.is_error) and "semantic" in json.dumps(explained.structured_content), json.dumps(explained.structured_content)[:300])
        for name, query, code in (
            ("a field that is not semantic", {"from": {"dataset": "message"}, "where": {"semantic": {"field": "split", "proposition": proposition}}}, None),
            ("a semantic condition under any", {"from": {"dataset": "message"}, "where": {"any": [leaf("split", "eq", "test"), {"semantic": {"field": "body", "proposition": proposition}}]}}, None),
            ("two semantic conditions", {"from": {"dataset": "message"}, "where": {"all": [{"semantic": {"field": "body", "proposition": proposition}}] * 2}}, None),
        ):
            result = await client.call_tool("query", {"query": query})
            report.check("semantic", f"refused with a code: {name}", result.is_error and code_of(result)[0] is not None, text_of(result)[:200])

    async with Client(StdioServerParameters(command=PYTHON, args=[script, "shortlist"], env=base_env())) as client:
        for proposition in PROPOSITIONS:
            for name in ("split = test", "priority >= 4"):
                _, all_true = truth(proposition, flavours[name], 10**9)
                result = await client.call_tool("query", {"query": ask(proposition, flavours[name], 100)})
                label = f"shortlist: '{proposition}' with {name}"
                if result.is_error:
                    report.check("semantic", label, code_of(result)[0] is not None and code_of(result)[0] != "query_execution_failed", text_of(result)[:200])
                    report.count("semantic", f"shortlist refused ({code_of(result)[0]})")
                    continue
                got = result.structured_content
                ids = [row["id"] for row in got["rows"]]
                ok = set(ids) <= all_true and got["semantic"]["exact"] is False and "shortlisted_by_vector_search" in got["semantic"] and ids == sorted(ids)
                report.check("semantic", label, ok, f"{len(ids)} rows, all true matches; truth has {len(all_true)}; {got['semantic']}")
                report.count("semantic", "shortlist: every row a true match, marked not exact" if ok else "shortlist: WRONG")


# --- main ----------------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    only = set(argv[argv.index("--only") + 1].split(",")) if "--only" in argv else None
    seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else 7
    report_path = argv[argv.index("--report") + 1] if "--report" in argv else None
    report = Report(verbose)
    wanted = lambda name: only is None or name in only      # noqa: E731

    shop_catalog = str(REPO / "examples" / "shop" / "catalog")
    shop, real = server(shop_catalog, shop_env()), server(real_catalog(), real_env())
    shop_rows, real_rows = shop_world(), real_world()
    report.say(f"seed {seed}; shop: {len(shop_rows['datasets']['customer']['rows'])} customers, {len(shop_rows['datasets']['ticket']['rows'])} tickets in three databases; "
               f"real: {len(real_rows['datasets']['message']['rows'])} messages and {len(real_rows['datasets']['customer']['rows'])} customers on two servers")
    started = time.perf_counter()
    if wanted("shop"):
        asyncio.run(run_shop_fixed(report, shop, shop_rows))
        asyncio.run(run_world(report, "shop", shop, shop_rows, random.Random(seed), trees=120, joins=150))
    if wanted("real"):
        asyncio.run(run_world(report, "real", real, real_rows, random.Random(seed + 1), trees=80, joins=120))
    if wanted("errors"):
        asyncio.run(run_errors(report, shop, real, real_rows))
    if wanted("hostile"):
        asyncio.run(run_hostile(report, shop, shop_rows))
    if wanted("protocol"):
        asyncio.run(run_protocol(report, shop, real, shop_rows, real_rows))
    if wanted("startup"):
        run_startup(report, shop_catalog)
    if wanted("semantic"):
        asyncio.run(run_semantic(report, real_rows))

    report.say()
    sections: dict[str, list[bool]] = {}
    for section, _, ok, _ in report.results:
        sections.setdefault(section, []).append(ok)
    for section, oks in sections.items():
        report.say(f"{section:<9} {sum(oks):>5} passed  {len(oks) - sum(oks):>3} failed")
        for key, count in sorted(report.notes.get(section, {}).items()):
            report.say(f"            {count:>5}  {key}")
    failed = [r for r in report.results if not r[2]]
    report.say(f"\n== {len(report.results) - len(failed)} passed, {len(failed)} failed in {time.perf_counter() - started:.0f}s")
    for section, name, _, detail in failed[:60]:
        report.say(f"   FAILED [{section}] {name}: {detail}")
    if report_path:
        Path(report_path).write_text("\n".join(report.lines) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
