"""Generated end-to-end matrix against a throwaway Postgres.

Every query is generated from the real data (values are sampled from the tables),
sent through the full YoDb path, and compared with a native SQL oracle.  Each
relational case runs under four planner configurations:

  rules        fixed rules, key transfer on
  stats        cost-based (real pg_stats + learned scan sizes), key transfer on
  rules_nokeys fixed rules, key transfer off (every read is a plain scan)
  stats_nokeys cost-based, key transfer off

Axes covered: every filter operator x every type x sampled values, boolean
trees (all / any / not, up to depth 3), order x direction x page size (and
two-key orders), select permutations, error cases (invalid operator/type pairs,
limits, unimplemented operators), the semantic filter (verify-all, shortlist,
small shortlist, budgets, quality bar, placement errors) and the optimizer
(rules vs statistics on skewed bulk tables).

    PYTHONPATH=src .venv/bin/python tests/e2e/run_matrix.py [-v] [--only cat,cat] [--seed N] [--report FILE]

Setup is the same as run_e2e.py (see README.md).  Exit code is non-zero on any failure.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
import itertools
import random
import re
import sys
import time

import psycopg

import run_e2e as base
from yodb.catalog import SourceKind
from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.connections import (
    MappingPostgresConnectionResolver,
    PostgresConnectionAdapter,
    PostgresConnectionSettings,
)
from yodb.errors import YoDbError
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine
from yodb.inspection import (
    InspectionAdapterBinding,
    PostgresCatalogValidator,
    PostgresSourceInspector,
    SourceInspectionRegistry,
)
from yodb.planning import (
    FederatedPhysicalPlanner,
    ObservationStore,
    PlannerPolicy,
    PostgresPlanningAdapter,
    PostgresStatisticsProvider,
    SourcePlanningRegistry,
    StatisticsService,
)
from yodb.runtime import InMemoryCatalogRuntime
from yodb.semantic import SemanticExtension, SemanticPlanPreference, SemanticPolicy, SemanticRuntime

# --------------------------------------------------------------------------------------
# data model of the generator
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class F:
    name: str      # logical field
    col: str       # oracle column (qualified)
    kind: str      # str | int | float | ts | bool
    home: str      # which source owns it: A (anchor), B, C


@dataclass(frozen=True)
class DS:
    name: str
    from_sql: str
    id_col: str
    fields: tuple[F, ...]

    def field(self, name: str) -> F:
        return next(f for f in self.fields if f.name == name)


CUSTOMER = DS(
    "customer",
    base.FROM,
    "a.account_id",
    (
        F("name", "a.company_name", "str", "A"), F("status", "a.account_status", "str", "A"),
        F("seats", "a.seats", "int", "A"), F("signup_at", "a.signup_at", "ts", "A"),
        F("is_vip", "a.is_vip", "bool", "A"), F("country", "a.country", "str", "A"),
        F("plan", "b.plan", "str", "B"), F("mrr", "b.mrr", "float", "B"), F("auto_renew", "b.auto_renew", "bool", "B"),
        F("tier", "s.tier", "str", "C"), F("open_tickets", "s.open_tickets", "int", "C"),
    ),
)
ORDER = DS(
    "order",
    "sales.orders o LEFT JOIN payments.payments p ON p.order_id = o.order_id "
    "LEFT JOIN logistics.shipments s ON s.order_id = o.order_id",
    "o.order_id",
    (
        F("customer_ref", "o.customer_ref", "str", "A"), F("status", "o.status", "str", "A"),
        F("amount", "o.amount", "float", "A"), F("items", "o.items", "int", "A"),
        F("placed_at", "o.placed_at", "ts", "A"), F("rush", "o.rush", "bool", "A"), F("channel", "o.channel", "str", "A"),
        F("method", "p.method", "str", "B"), F("paid_amount", "p.paid_amount", "float", "B"), F("settled", "p.settled", "bool", "B"),
        F("carrier", "s.carrier", "str", "C"), F("days", "s.days", "int", "C"), F("express", "s.express", "bool", "C"),
    ),
)
DATASETS = {"customer": CUSTOMER, "order": ORDER}

OPS = {
    "str": ["eq", "ne", "in", "not_in", "contains", "starts_with", "is_null", "is_not_null"],
    "int": ["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "is_null", "is_not_null"],
    "float": ["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "is_null", "is_not_null"],
    "ts": ["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "is_null", "is_not_null"],
    "bool": ["eq", "ne", "in", "not_in", "is_null", "is_not_null"],
}
ALL_OPS = ["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "contains", "starts_with", "is_null", "is_not_null"]
MISSING = {"str": "zzz-missing", "int": 987_654, "float": -1.5, "ts": "1999-01-01T00:00:00Z", "bool": None}


def lit(kind: str, value) -> str:
    if kind == "str":
        return "'" + str(value).replace("'", "''") + "'"
    if kind == "bool":
        return "TRUE" if value else "FALSE"
    if kind == "ts":
        return "'" + value.replace("Z", "").replace("T", " ") + "'::timestamp"
    return repr(value)


def wire(kind: str, value):
    """A sampled DB value in the form the query language takes."""

    if kind == "ts" and isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value


# --------------------------------------------------------------------------------------
# expression trees -> (YoDb filter, oracle SQL)
# --------------------------------------------------------------------------------------

Tree = tuple


def render(ds: DS, tree: Tree) -> tuple[dict, str]:
    tag = tree[0]
    if tag == "p":
        _, name, op, value = tree
        f = ds.field(name)
        predicate = {"field": name, "op": op}
        if op not in ("is_null", "is_not_null"):
            predicate["value"] = value
        col = f.col
        if op == "eq":
            sql = f"{col} = {lit(f.kind, value)}"
        elif op == "ne":
            sql = f"{col} <> {lit(f.kind, value)}"
        elif op in ("in", "not_in"):
            items = ", ".join(lit(f.kind, v) for v in value)
            sql = f"{col} {'IN' if op == 'in' else 'NOT IN'} ({items})"
        elif op in ("gt", "gte", "lt", "lte"):
            sql = f"{col} {dict(gt='>', gte='>=', lt='<', lte='<=')[op]} {lit(f.kind, value)}"
        elif op == "contains":
            sql = f"position({lit('str', value)} in {col}) > 0"
        elif op == "starts_with":
            sql = f"left({col}, {len(value)}) = {lit('str', value)}"
        elif op == "is_null":
            sql = f"{col} IS NULL"
        else:
            sql = f"{col} IS NOT NULL"
        return predicate, sql
    if tag in ("all", "any"):
        parts = [render(ds, child) for child in tree[1]]
        glue = " AND " if tag == "all" else " OR "
        return {tag: [p for p, _ in parts]}, "(" + glue.join(s for _, s in parts) + ")"
    inner = render(ds, tree[1])
    return {"not": inner[0]}, f"NOT ({inner[1]})"


def leaves(tree: Tree):
    if tree[0] == "p":
        yield tree
    else:
        for child in tree[1] if tree[0] in ("all", "any") else (tree[1],):
            yield from leaves(child)


# --------------------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------------------


@dataclass
class Case:
    category: str
    name: str
    query: dict
    sql: str | None = None                # oracle SQL (None for error cases)
    fields: tuple[str, ...] = ()          # result columns in oracle order
    kinds: tuple[str, ...] = ()           # their kinds, to normalize values
    expect_error: str | None = None       # substring of the expected error code ("" = any error)
    engines: tuple[str, ...] = ("rules", "stats", "rules_nokeys", "stats_nokeys")
    shape: dict = field(default_factory=dict)   # metadata for the coverage report


def select_fields(ds: DS, select: list[str]) -> list[str]:
    return ["id", *[s for s in select if s != "id"]]


def relational_case(category, name, ds: DS, select, tree=None, order=(), first=100, **shape) -> Case:
    select = list(dict.fromkeys(select))
    query = {"from": {"dataset": ds.name}, "select": select, "page": {"first": first}}
    where_sql = "TRUE"
    if tree is not None:
        query["where"], where_sql = render(ds, tree)
    if order:
        query["order_by"] = [{"field": n, "direction": d} for n, d in order]
    names = select_fields(ds, select)
    cols = [ds.id_col if n == "id" else ds.field(n).col for n in names]
    kinds = tuple("str" if n == "id" else ds.field(n).kind for n in names)
    order_sql = ", ".join(f"{ds.field(n).col} {d.upper()}" for n, d in order)
    order_sql = (order_sql + ", " if order_sql else "") + f"{ds.id_col} ASC"
    sql = f"SELECT {', '.join(cols)} FROM {ds.from_sql} WHERE {where_sql} ORDER BY {order_sql} LIMIT {first}"
    homes = {ds.field(n).home for n in names if n != "id"}
    if tree is not None:
        homes |= {ds.field(leaf[1]).home for leaf in leaves(tree)}
    return Case(category, name, query, sql, tuple(names), kinds, shape={"ds": ds.name, "sources": "".join(sorted(homes)), **shape})


def error_case(category, name, query, expect="", engines=("rules",)) -> Case:
    return Case(category, name, query, expect_error=expect, engines=engines)


# --------------------------------------------------------------------------------------
# sampling real values
# --------------------------------------------------------------------------------------


class Sampler:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.cache: dict[tuple[str, str], list] = {}

    def values(self, ds: DS, f: F) -> list:
        key = (ds.name, f.name)
        if key in self.cache:
            return self.cache[key]
        with self.connection.cursor() as cursor:
            if f.kind == "bool":
                values = [True, False]
            elif f.kind == "str":
                cursor.execute(f"SELECT {f.col}, count(*) c FROM {ds.from_sql} WHERE {f.col} IS NOT NULL GROUP BY 1 ORDER BY c DESC, 1")
                rows = [r[0] for r in cursor.fetchall()]
                values = rows[:5] if len(rows) <= 8 else [rows[0], rows[len(rows) // 2], rows[-1]]
            else:
                cursor.execute(
                    f"SELECT percentile_disc(ARRAY[0.1, 0.5, 0.9]) WITHIN GROUP (ORDER BY {f.col}) FROM {ds.from_sql} WHERE {f.col} IS NOT NULL"
                )
                values = [wire(f.kind, v) for v in cursor.fetchone()[0]]
        self.cache[key] = values
        return values


def single_predicates(ds: DS, sampler: Sampler):
    for f in ds.fields:
        values = sampler.values(ds, f)
        for op in OPS[f.kind]:
            if op in ("is_null", "is_not_null"):
                yield f, op, [None]
            elif op in ("in", "not_in"):
                picks = [[values[0]], values[:2], [values[0], MISSING[f.kind]]] if f.kind != "bool" else [[True], [False], [True, False]]
                yield f, op, picks
            elif op in ("contains", "starts_with"):
                text = str(values[0])
                yield f, op, [text[:3], text[1:4] if len(text) > 4 else text, MISSING["str"]]
            elif f.kind == "bool":
                yield f, op, [True, False]
            else:
                yield f, op, [*values, *([MISSING[f.kind]] if op in ("eq", "ne") else [])]


def random_leaf(ds: DS, sampler: Sampler, rng: random.Random, fields=None) -> Tree:
    f = rng.choice(fields or ds.fields)
    op = rng.choice(OPS[f.kind])
    values = sampler.values(ds, f)
    if op in ("is_null", "is_not_null"):
        return ("p", f.name, op, None)
    if op in ("in", "not_in"):
        pool = [*values, MISSING[f.kind]] if f.kind != "bool" else [True, False]
        return ("p", f.name, op, rng.sample(pool, k=min(len(pool), rng.randint(1, 3))))
    if op in ("contains", "starts_with"):
        text = str(rng.choice(values))
        start = 0 if op == "starts_with" else rng.randint(0, max(0, len(text) - 2))
        return ("p", f.name, op, text[start:start + rng.randint(1, 3)] or "a")
    pool = [*values, MISSING[f.kind]] if f.kind != "bool" else [True, False]
    return ("p", f.name, op, rng.choice(pool))


def random_tree(ds: DS, sampler: Sampler, rng: random.Random, depth: int) -> Tree:
    if depth == 0 or rng.random() < 0.35:
        return random_leaf(ds, sampler, rng)
    kind = rng.choice(["all", "all", "any", "any", "not"])
    if kind == "not":
        return ("not", random_tree(ds, sampler, rng, depth - 1))
    return (kind, [random_tree(ds, sampler, rng, depth - 1) for _ in range(rng.randint(2, 3))])


def build_relational_cases(connection, rng: random.Random) -> list[Case]:
    sampler = Sampler(connection)
    cases: list[Case] = []
    # L1: every operator x every type x sampled values, on every field of both datasets
    for ds in DATASETS.values():
        for f, op, value_sets in single_predicates(ds, sampler):
            for index, value in enumerate(value_sets):
                tree = ("p", f.name, op, None if op in ("is_null", "is_not_null") else value)
                cases.append(relational_case("filter", f"{ds.name}.{f.name} {op} #{index}", ds, [f.name], tree, op=op, kind=f.kind, home=f.home))
    # L2: boolean trees (random, seeded), including cross-source mixes
    for ds, count in ((ORDER, 400), (CUSTOMER, 200)):
        for i in range(count):
            tree = random_tree(ds, sampler, rng, depth=3)
            select = rng.sample([f.name for f in ds.fields], k=rng.randint(1, 4))
            cases.append(relational_case("boolean_tree", f"{ds.name} tree #{i}", ds, select, tree, first=rng.choice([5, 50, 500]), leaves=len(list(leaves(tree)))))
    # L2b: structured combinations across sources: all pairs of home sources x combinator x negation
    for ds in (ORDER, CUSTOMER):
        by_home = {h: [f for f in ds.fields if f.home == h] for h in "ABC"}
        for (h1, h2), comb, neg in itertools.product(itertools.combinations_with_replacement("ABC", 2), ("all", "any"), (False, True)):
            for rep in range(3):
                a = random_leaf(ds, sampler, rng, by_home[h1])
                b = random_leaf(ds, sampler, rng, by_home[h2])
                tree = (comb, [a, ("not", b) if neg else b])
                cases.append(relational_case("cross_source", f"{ds.name} {h1}{'&' if comb == 'all' else '|'}{'!' if neg else ''}{h2} #{rep}", ds,
                                             [by_home[h1][0].name, by_home[h2][-1].name], tree, comb=comb, neg=neg))
    # L3: order x direction x page size x filter flavor (single key), plus two-key orders
    orderable = [f for f in ORDER.fields if f.name != "customer_ref"] + [ORDER.field("customer_ref")]
    anchor_filter = ("p", "status", "ne", "cancelled")
    contributor_filter = ("p", "method", "in", ["wire", "gift"])
    both_filter = ("all", [anchor_filter, ("p", "carrier", "eq", "dhl")])
    for f, direction, first, (flavor, tree) in itertools.product(
        orderable, ("asc", "desc"), (1, 7, 50, 500), (("none", None), ("anchor", anchor_filter), ("contributor", contributor_filter), ("both", both_filter))
    ):
        if first in (1, 500) and flavor in ("anchor", "contributor") and f.kind not in ("str", "float"):
            continue   # keep the matrix broad but not redundant
        cases.append(relational_case("order_page", f"order by {f.name} {direction} first={first} filter={flavor}", ORDER, [f.name, "status"], tree,
                                     order=[(f.name, direction)], first=first, home=f.home, flavor=flavor))
    for (a, b), (da, db), first in itertools.product(
        [("status", "amount"), ("channel", "placed_at"), ("method", "items"), ("carrier", "days"), ("rush", "status"), ("settled", "carrier")],
        [("asc", "asc"), ("asc", "desc"), ("desc", "asc"), ("desc", "desc")], (5, 100),
    ):
        cases.append(relational_case("order_page", f"order by {a} {da}, {b} {db} first={first}", ORDER, [a, b], None,
                                     order=[(a, da), (b, db)], first=first, home="multi"))
    for f, direction in itertools.product([f for f in CUSTOMER.fields if f.name != "name"], ("asc", "desc")):
        cases.append(relational_case("order_page", f"customer order by {f.name} {direction}", CUSTOMER, [f.name], None, order=[(f.name, direction)], first=6, home=f.home))
    # name has mixed case ("Acme", "acme"): single-source order is the database's own collation
    cases.append(relational_case("order_page", "customer order by name asc (single source, pushed)", CUSTOMER, ["name"], None, order=[("name", "asc")], first=12, home="A"))
    # L4: select permutations (single fields, per-source, random subsets, everything)
    for ds in DATASETS.values():
        names = [f.name for f in ds.fields]
        subsets = [[n] for n in names] + [[f.name for f in ds.fields if f.home == h] for h in "ABC"] + [names] + [rng.sample(names, k=3) for _ in range(8)]
        for subset in subsets:
            cases.append(relational_case("select", f"{ds.name} select {','.join(subset)}", ds, subset, None, first=40))
        cases.append(relational_case("select", f"{ds.name} select id only", ds, ["id"], None, first=25))
    cases.append(relational_case("select", "empty select returns ids only", ORDER, [], None, first=10))
    # L5: page sizes and the default page
    for first in (1, 2, 99, 100, 101, 500):
        cases.append(relational_case("page", f"order first={first}", ORDER, ["status"], None, first=first))
    return cases


def build_error_cases(connection) -> list[Case]:
    cases: list[Case] = []
    sample = {"str": "x", "int": 1, "float": 1.5, "ts": "2024-01-01T00:00:00Z", "bool": True}
    # every operator on every type: valid pairs are exercised above, the rest must be refused cleanly
    for kind, field_name in (("str", "status"), ("int", "items"), ("float", "amount"), ("ts", "placed_at"), ("bool", "rush")):
        for op in ALL_OPS:
            if op in OPS[kind]:
                continue
            predicate = {"field": field_name, "op": op, "value": sample["str" if op in ("contains", "starts_with") else kind]}
            cases.append(error_case("invalid_operator", f"{op} on {kind}", {"from": {"dataset": "order"}, "select": ["id"], "where": predicate}, "operator_not_supported"))
    valid = {"from": {"dataset": "order"}, "select": ["status"], "page": {"first": 5}}
    cases += [
        error_case("invalid_query", "unknown dataset", {**valid, "from": {"dataset": "nope"}}),
        error_case("invalid_query", "unknown select field", {**valid, "select": ["nope"]}),
        error_case("invalid_query", "unknown where field", {**valid, "where": {"field": "nope", "op": "eq", "value": 1}}),
        error_case("invalid_query", "unknown order field", {**valid, "order_by": [{"field": "nope", "direction": "asc"}]}),
        error_case("invalid_query", "bad direction", {**valid, "order_by": [{"field": "status", "direction": "sideways"}]}),
        error_case("invalid_query", "unknown operator", {**valid, "where": {"field": "status", "op": "like", "value": "%a%"}}),
        error_case("invalid_query", "raw sql key", {**valid, "sql": "SELECT 1"}),
        error_case("invalid_query", "type mismatch int vs string", {**valid, "where": {"field": "items", "op": "eq", "value": "many"}}),
        error_case("invalid_query", "type mismatch bool vs string", {**valid, "where": {"field": "rush", "op": "eq", "value": "yes"}}),
        error_case("invalid_query", "naive timestamp rejected", {**valid, "where": {"field": "placed_at", "op": "gt", "value": "2024-01-01T00:00:00"}}, "type_invalid"),
        error_case("invalid_query", "eq with null value", {**valid, "where": {"field": "status", "op": "eq", "value": None}}),
        error_case("invalid_query", "is_null must not take a value", {**valid, "where": {"field": "status", "op": "is_null", "value": 1}}),
        error_case("invalid_query", "in with an empty list", {**valid, "where": {"field": "status", "op": "in", "value": []}}),
        error_case("invalid_query", "in with a scalar", {**valid, "where": {"field": "status", "op": "in", "value": "paid"}}),
        error_case("invalid_query", "empty all", {**valid, "where": {"all": []}}),
        error_case("invalid_query", "empty any", {**valid, "where": {"any": []}}),
        error_case("limits", "page.first = 0", {**valid, "page": {"first": 0}}),
        error_case("limits", "page.first above the maximum (501)", {**valid, "page": {"first": 501}}),
        error_case("limits", "in-list above the maximum (1001 values)", {**valid, "where": {"field": "customer_ref", "op": "in", "value": [f"u{i}" for i in range(1001)]}}),
        error_case("limits", "a cursor is not part of a page", {**valid, "page": {"first": 5, "after": "abc"}}, "shape_invalid"),
        error_case("unimplemented_operator", "group_by", {**valid, "group_by": ["status"]}),
        error_case("unimplemented_operator", "aggregate", {**valid, "aggregate": {"count": "*"}}),
        error_case("unimplemented_operator", "distinct", {**valid, "distinct": True}),
        error_case("unimplemented_operator", "join", {**valid, "join": {"dataset": "customer"}}),
        error_case("unimplemented_operator", "union", {**valid, "union": [valid]}),
        error_case("unimplemented_operator", "traverse", {**valid, "traverse": {"relation": "x"}}),
        error_case("semantic_rules", "semantic without any extension registered", {
            "from": {"dataset": "ticket"}, "select": ["subject"], "where": {"semantic": {"field": "body", "proposition": "mentions price"}}}, "not_supported",
            engines=("rules",)),
    ]
    # in-list at exactly the limit succeeds (an oracle case, not an error)
    return cases


def build_limit_boundary_cases(connection) -> list[Case]:
    values = [f"u{i}" for i in range(1, 1001)]
    tree = ("p", "customer_ref", "in", values)
    return [relational_case("limits", "in-list at the maximum (1000 values)", ORDER, ["customer_ref"], tree, first=500)]


# --------------------------------------------------------------------------------------
# semantic matrix
# --------------------------------------------------------------------------------------

PROPOSITIONS = ("mentions price", "mentions cancel", "mentions price and cancel", "mentions refund and bug")
SEM_WHERE = {
    "none": (None, "TRUE"),
    "priority>=3": ({"field": "priority", "op": "gte", "value": 3}, "t.priority >= 3"),
    "owner=ann": ({"field": "owner", "op": "eq", "value": "ann"}, "o.owner = 'ann'"),
    "priority+owner": ({"all": [{"field": "priority", "op": "gte", "value": 3}, {"field": "owner", "op": "ne", "value": "bob"}]}, "(t.priority >= 3 AND o.owner <> 'bob')"),
    "subject contains (coordinator-only)": ({"field": "subject", "op": "contains", "value": "1"}, "position('1' in t.subject) > 0"),
}
SEM_ORDER = {"none": ([], "t.ticket_id"), "subject desc": ([("subject", "desc")], "t.subject DESC, t.ticket_id"),
             "priority desc, subject asc": ([("priority", "desc"), ("subject", "asc")], "t.priority DESC, t.subject ASC, t.ticket_id")}


@dataclass
class SemanticCase:
    name: str
    query: dict
    sql: str
    fields: tuple[str, ...]
    all_matches_sql: str
    quality_blocks: bool


def build_semantic_cases() -> list[SemanticCase]:
    cases = []
    for prop, (wname, (wtree, wsql)), (oname, (order, osql)), first, quality in itertools.product(
        PROPOSITIONS, SEM_WHERE.items(), SEM_ORDER.items(), (1, 3, 20), (None, 0.5, 0.99)
    ):
        terms = [*([wtree] if wtree else []), {"semantic": {"field": "body", "proposition": prop}}]
        query = {"from": {"dataset": "ticket"}, "select": ["subject", "owner"], "page": {"first": first},
                 "where": terms[0] if len(terms) == 1 else {"all": terms}}
        if order:
            query["order_by"] = [{"field": n, "direction": d} for n, d in order]
        if quality is not None:
            query["constraints"] = {"minimum_quality": quality}
        fields = ("id", "subject", "owner")
        cols = ", ".join(base.TICKET_COLUMN[f] for f in fields)
        core = f"SELECT {cols} FROM {base.TICKET_FROM} WHERE ({wsql}) AND ({base.keywords_sql(prop)}) ORDER BY {osql}"
        cases.append(SemanticCase(f"'{prop}' where={wname} order={oname} first={first} quality={quality}", query, f"{core} LIMIT {first}", fields, core, quality is not None and quality > 0.95))
    return cases


def semantic_error_cases() -> list[Case]:
    ticket = {"from": {"dataset": "ticket"}, "select": ["subject"], "page": {"first": 5}}
    sem = {"semantic": {"field": "body", "proposition": "mentions price"}}
    plain = {"field": "priority", "op": "gte", "value": 3}
    engines = ("semantic",)
    return [
        error_case("semantic_rules", "semantic under any", {**ticket, "where": {"any": [sem, plain]}}, "expression_invalid", engines),
        error_case("semantic_rules", "semantic under not", {**ticket, "where": {"not": sem}}, "expression_invalid", engines),
        error_case("semantic_rules", "two semantic terms", {**ticket, "where": {"all": [sem, {"semantic": {"field": "body", "proposition": "mentions cancel"}}]}}, "limit_invalid", engines),
        error_case("semantic_rules", "semantic on a non-eligible field", {**ticket, "where": {"semantic": {"field": "subject", "proposition": "mentions price"}}}, "operator_not_supported", engines),
        error_case("semantic_rules", "semantic on a non-text field", {**ticket, "where": {"semantic": {"field": "priority", "proposition": "mentions price"}}}, "operator_not_supported", engines),
        error_case("semantic_rules", "blank proposition", {**ticket, "where": {"semantic": {"field": "body", "proposition": "   "}}}, "", engines),
        error_case("semantic_rules", "unknown semantic key", {**ticket, "where": {"semantic": {"field": "body", "proposition": "x", "extra": 1}}}, "", engines),
        error_case("semantic_rules", "minimum_quality out of range", {**ticket, "constraints": {"minimum_quality": 1.5}, "where": sem}, "", engines),
        error_case("semantic_rules", "minimum_quality without a semantic term", {**ticket, "constraints": {"minimum_quality": 0.5}, "where": plain}, "", engines),
        error_case("semantic_rules", "semantic with a cursor", {**ticket, "page": {"first": 5, "after": "x"}, "where": sem}, "shape_invalid", engines),
    ]


# --------------------------------------------------------------------------------------
# optimizer cases (bulk tables)
# --------------------------------------------------------------------------------------


def build_optimizer_cases(rng: random.Random) -> list[Case]:
    cases = []
    kinds = [f"k{i}" for i in range(1000)]
    for count in (1, 2, 5, 20, 100, 400):
        for tag in ("hot", "cold"):
            values = kinds[:count]
            where = {"all": [{"field": "kind", "op": "in", "value": values}, {"field": "tag", "op": "eq", "value": tag}]}
            col = lambda v: "'" + v + "'"
            sql = (f"SELECT i.item_id, i.kind, t.tag FROM bulk.items i LEFT JOIN bulk.tags t USING (item_id) "
                   f"WHERE i.kind IN ({', '.join(col(v) for v in values)}) AND t.tag = '{tag}' ORDER BY i.item_id LIMIT 500")
            cases.append(Case("optimizer", f"kind in {count} values AND tag={tag}",
                              {"from": {"dataset": "item"}, "select": ["kind", "tag"], "where": where, "page": {"first": 500}},
                              sql, ("id", "kind", "tag"), ("str", "str", "str"), engines=("rules", "stats"), shape={"ds": "item"}))
    for tag in ("hot", "cold"):
        cases.append(Case("optimizer", f"tag={tag} only (nothing narrows)",
                          {"from": {"dataset": "item"}, "select": ["tag"], "where": {"field": "tag", "op": "eq", "value": tag}, "page": {"first": 500}},
                          f"SELECT i.item_id, t.tag FROM bulk.items i LEFT JOIN bulk.tags t USING (item_id) WHERE t.tag = '{tag}' ORDER BY i.item_id LIMIT 500",
                          ("id", "tag"), ("str", "str"), engines=("rules", "stats"), shape={"ds": "item"}))
    return cases


# --------------------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------------------

KEY_TRANSFER = re.compile(r'"(account_id|customer_id|order_id|ticket_id|item_id)" IN \(')


def normalize(value, kind):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@dataclass
class Outcome:
    case: Case
    engine: str
    ok: bool
    detail: str = ""
    error_code: str = ""
    rows: int = 0
    key_transfer: bool = False
    fingerprint: str = ""
    kinds: tuple[str, ...] = ()
    strategy: str = ""
    ms: float = 0.0


class Matrix:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        resolver = MappingPostgresConnectionResolver({ref: PostgresConnectionSettings(conninfo=base.CONNINFO) for ref in base.REFS})
        self.connections = PostgresConnectionAdapter(resolver, max_size=4, acquire_timeout_seconds=10)
        inspector = PostgresSourceInspector(self.connections)
        registry = SourceInspectionRegistry([InspectionAdapterBinding(
            source_kind=inspector.source_kind, inspector=inspector, validator=PostgresCatalogValidator())])
        self.runtime = InMemoryCatalogRuntime(base.HERE / "catalog", registry)
        refresh = self.runtime.refresh()
        if self.runtime.active is None:
            raise SystemExit(f"catalog not activated: {refresh.status.value}")
        self.executor = base.RecordingExecutor(self.connections)
        self.compilers = QueryCompilerRegistry([PostgresQueryCompiler()])
        self.executors = QueryExecutionAdapterRegistry([self.executor])
        self.oracle = psycopg.connect(base.CONNINFO)
        self.statistics = StatisticsService({SourceKind.POSTGRES: PostgresStatisticsProvider(self.connections)}, observations=ObservationStore())
        self.engines = {
            "rules": self.engine(),
            "stats": self.engine(statistics=self.statistics),
            "rules_nokeys": self.engine(policy=PlannerPolicy(maximum_transfer_keys=0)),
            "stats_nokeys": self.engine(policy=PlannerPolicy(maximum_transfer_keys=0), statistics=self.statistics),
        }
        toy = SemanticRuntime(base.ToyVerifier(), base.ToyEmbedder(), verification_batch_size=5)
        info, dims = base.ToyEmbedder.info, base.ToyEmbedder.dimensions
        def semantic(**policy):
            return self.engine(semantic=SemanticExtension(toy, policy=SemanticPolicy(embedder=info, embedder_dimensions=dims, **policy)))
        self.semantic_engines = {
            "A": semantic(preference=SemanticPlanPreference.VERIFY_ALL),
            "B": semantic(),
            "Bsmall": semantic(minimum_shortlist=3, shortlist_oversample=1),
        }
        self.engines["semantic"] = self.semantic_engines["B"]
        self.outcomes: list[Outcome] = []

    def engine(self, *, policy=PlannerPolicy(), statistics=None, semantic=None):
        extensions = () if semantic is None else (semantic,)
        planner = FederatedPhysicalPlanner(
            SourcePlanningRegistry([PostgresPlanningAdapter()]), policy=policy, statistics=statistics, extensions=extensions)
        return QueryExecutionEngine(self.runtime, self.compilers, self.executors, planner=planner, statistics=statistics, extensions=extensions)

    def expected(self, sql: str, kinds):
        with self.oracle.cursor() as cursor:
            cursor.execute(sql)
            return [tuple(normalize(v, k) for v, k in zip(row, kinds)) for row in cursor.fetchall()]

    def run_case(self, case: Case) -> None:
        expected = None if case.sql is None else self.expected(case.sql, case.kinds or ("str",) * 99)
        for label in case.engines:
            engine = self.engines[label]
            self.executor.log.clear()
            outcome = Outcome(case, label, False)
            started = time.perf_counter()
            try:
                try:
                    explanation = engine.explain(case.query)
                    outcome.fingerprint = explanation.plan_fingerprint
                    outcome.kinds = tuple(node.kind for node in explanation.nodes)
                    outcome.strategy = (explanation.optimizer[0] if explanation.optimizer else "none").replace("strategy=", "")
                except YoDbError:
                    pass
                result = engine.execute(case.query, timeout_seconds=30)
                got = [tuple(row.get(f) for f in case.fields) for row in result.rows]
                outcome.rows = len(got)
                outcome.key_transfer = any(KEY_TRANSFER.search(entry["sql"]) for entry in self.executor.log)
                if case.expect_error is not None:
                    outcome.detail = f"expected an error containing '{case.expect_error}' but got {len(got)} rows"
                elif got == expected:
                    outcome.ok = True
                else:
                    outcome.detail = diff(expected, got)
            except YoDbError as error:
                outcome.error_code = error.code.value
                if case.expect_error is not None:
                    outcome.ok = case.expect_error in error.code.value
                    if not outcome.ok:
                        outcome.detail = f"expected error '{case.expect_error}', got {error.code.value}: {error.detail.message}"
                else:
                    outcome.detail = f"unexpected error {error.code.value}: {error.detail.message}"
            except Exception as error:   # a crash is always a failure
                outcome.detail = f"CRASH {type(error).__name__}: {error}"
            outcome.ms = (time.perf_counter() - started) * 1000
            if not outcome.ok:
                outcome.detail += "\n      sql: " + " | ".join(f"[{e['source']}] {e['sql'][:150]}" for e in self.executor.log)
            self.outcomes.append(outcome)
            if self.verbose or not outcome.ok:
                print(f"{'PASS' if outcome.ok else 'FAIL'} [{case.category}] [{label}] {case.name}" + ("" if outcome.ok else f"\n      {outcome.detail}"))

    def run_semantic(self, case: SemanticCase) -> None:
        wrapper = Case("semantic", case.name, case.query, case.sql, case.fields, ("str",) * len(case.fields), engines=("A", "B", "Bsmall"))
        with self.oracle.cursor() as cursor:
            cursor.execute(case.sql)
            expected = [tuple(row) for row in cursor.fetchall()]
            cursor.execute(case.all_matches_sql)
            everything = {tuple(row) for row in cursor.fetchall()}
        if case.quality_blocks:
            expected, everything = [], set()   # the toy verifier reports 0.95
        for label in ("A", "B", "Bsmall"):
            engine = self.semantic_engines[label]
            self.executor.log.clear()
            outcome = Outcome(wrapper, label, False)
            started = time.perf_counter()
            try:
                explanation = engine.explain(case.query)
                outcome.kinds = tuple(node.kind for node in explanation.nodes)
                outcome.fingerprint = explanation.plan_fingerprint
                result = engine.execute(case.query, timeout_seconds=30)
                got = [tuple(row.get(f) for f in case.fields) for row in result.rows]
                report = result.reports["semantic"]
                outcome.strategy = report.stats.plan.value
                outcome.rows = len(got)
                outcome.key_transfer = any(KEY_TRANSFER.search(e["sql"]) for e in self.executor.log)
                if label == "Bsmall":
                    outcome.ok = set(got) <= everything and len(got) <= case.query["page"]["first"]
                    outcome.detail = "" if outcome.ok else f"returned a non-match or too many rows: {got}"
                else:
                    outcome.ok = got == expected
                    outcome.detail = "" if outcome.ok else diff(expected, got)
            except YoDbError as error:
                outcome.error_code = error.code.value
                outcome.detail = f"unexpected error {error.code.value}: {error.detail.message}"
            except Exception as error:
                outcome.detail = f"CRASH {type(error).__name__}: {error}"
            outcome.ms = (time.perf_counter() - started) * 1000
            self.outcomes.append(outcome)
            if self.verbose or not outcome.ok:
                print(f"{'PASS' if outcome.ok else 'FAIL'} [semantic] [{label}] {case.name}" + ("" if outcome.ok else f"\n      {outcome.detail}"))

    def run_optimizer(self, case: Case) -> None:
        """Results must match the oracle whenever a plan succeeds; statistics must never do worse than the rules."""

        expected = self.expected(case.sql, case.kinds)
        results = {}
        for label in case.engines:
            self.executor.log.clear()
            outcome = Outcome(case, label, False)
            started = time.perf_counter()
            try:
                explanation = self.engines[label].explain(case.query)
                outcome.fingerprint = explanation.plan_fingerprint
                outcome.kinds = tuple(node.kind for node in explanation.nodes)
                outcome.strategy = (explanation.optimizer[0] if explanation.optimizer else "none").replace("strategy=", "")
                result = self.engines[label].execute(case.query, timeout_seconds=60)
                got = [tuple(row.get(f) for f in case.fields) for row in result.rows]
                outcome.rows = len(got)
                outcome.ok = got == expected
                outcome.detail = "" if outcome.ok else diff(expected, got)
                outcome.key_transfer = any(KEY_TRANSFER.search(e["sql"]) for e in self.executor.log)
                results[label] = "ok" if outcome.ok else "wrong"
            except YoDbError as error:
                outcome.error_code = error.code.value
                outcome.ok = "row_limit" in error.code.value      # failing safely on the scan guard is acceptable
                outcome.detail = "" if outcome.ok else f"unexpected error {error.code.value}"
                results[label] = "guard" if outcome.ok else "error"
            outcome.ms = (time.perf_counter() - started) * 1000
            self.outcomes.append(outcome)
            if self.verbose or not outcome.ok:
                print(f"{'PASS' if outcome.ok else 'FAIL'} [optimizer] [{label}] {case.name} -> {results.get(label)}")
        if results.get("rules") == "ok" and results.get("stats") != "ok":
            self.outcomes.append(Outcome(case, "stats_not_worse", False, "statistics failed where the rules succeeded"))
            print(f"FAIL [optimizer] stats worse than rules: {case.name}")
        else:
            self.outcomes.append(Outcome(case, "stats_not_worse", True))


def diff(expected, got) -> str:
    if len(expected) != len(got):
        head = f"expected {len(expected)} rows, got {len(got)}"
    else:
        head = f"{len(expected)} rows, order or values differ"
    for index, (a, b) in enumerate(itertools.zip_longest(expected, got)):
        if a != b:
            return f"{head}; first difference at row {index}: expected {a}, got {b}"
    return head


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------


def report(matrix: Matrix, elapsed: float) -> tuple[str, bool]:
    outcomes = matrix.outcomes
    lines = []
    out = lines.append
    by_category: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        by_category[o.case.category].append(o)
    total_fail = sum(not o.ok for o in outcomes)
    out("")
    out("=" * 96)
    out(f"MATRIX RESULT: {len(outcomes) - total_fail} passed, {total_fail} failed, {len(outcomes)} executions, "
        f"{len({o.case.name + o.case.category for o in outcomes})} distinct cases, {elapsed:.0f} s")
    out("=" * 96)
    out(f"{'category':<24}{'cases':>7}{'runs':>7}{'pass':>7}{'fail':>6}   engines")
    for category, items in by_category.items():
        cases = len({o.case.name for o in items})
        engines = ",".join(sorted({o.engine for o in items}))
        out(f"{category:<24}{cases:>7}{len(items):>7}{sum(o.ok for o in items):>7}{sum(not o.ok for o in items):>6}   {engines}")

    relational = [o for o in outcomes if o.case.category in ("filter", "boolean_tree", "cross_source", "order_page", "select", "page", "limits") and o.case.sql]
    out("\nFILTER OPERATOR x TYPE (single-predicate cases; pass/runs)")
    grid: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for o in outcomes:
        if o.case.category == "filter":
            grid[(o.case.shape["op"], o.case.shape["kind"])].append(o.ok)
    kinds = ["str", "int", "float", "ts", "bool"]
    out(f"{'op':<14}" + "".join(f"{k:>10}" for k in kinds))
    for op in ALL_OPS:
        cells = []
        for k in kinds:
            v = grid.get((op, k))
            cells.append(f"{sum(v)}/{len(v)}" if v else "-")
        out(f"{op:<14}" + "".join(f"{c:>10}" for c in cells))

    out("\nPLAN SHAPES EXERCISED (relational executions that ran)")
    node_counter = Counter()
    for o in outcomes:
        for k in set(o.kinds):
            node_counter[k] += 1
    for kind, count in sorted(node_counter.items()):
        out(f"  {kind:<24}{count:>6} plans")
    ran = [o for o in relational if o.ok]
    out(f"  key transfer used (id IN (...))  {sum(o.key_transfer for o in ran):>6} of {len(ran)} executions")
    for engine in ("rules", "stats", "rules_nokeys", "stats_nokeys"):
        mine = [o for o in ran if o.engine == engine]
        out(f"    {engine:<14} key transfer in {sum(o.key_transfer for o in mine):>4} of {len(mine)}")
    by_sources = Counter(o.case.shape.get("sources") for o in relational if o.engine == "rules")
    out("  sources touched (A=anchor B/C=contributors): " + ", ".join(f"{k}:{v}" for k, v in sorted(by_sources.items(), key=lambda kv: str(kv[0]))))

    out("\nOPTIMIZER: did statistics change the plan? (same query, rules vs stats)")
    pairs = defaultdict(dict)
    for o in outcomes:
        if o.engine in ("rules", "stats") and o.fingerprint:
            pairs[(o.case.category, o.case.name)][o.engine] = o
    differs = sum(1 for p in pairs.values() if len(p) == 2 and p["rules"].fingerprint != p["stats"].fingerprint)
    both = sum(1 for p in pairs.values() if len(p) == 2)
    out(f"  plans differ in {differs} of {both} cases; strategy chosen by stats engine: "
        + ", ".join(f"{k}={v}" for k, v in Counter(p['stats'].strategy for p in pairs.values() if 'stats' in p).items()))
    opt = [o for o in outcomes if o.case.category == "optimizer" and o.engine in ("rules", "stats")]
    if opt:
        out(f"  {'case':<44}{'rules':>14}{'stats':>14}")
        by_name = defaultdict(dict)
        for o in opt:
            by_name[o.case.name][o.engine] = "ok" if o.error_code == "" and o.ok else (o.error_code or "WRONG")
        for name, row in by_name.items():
            out(f"  {name:<44}{row.get('rules', '-'):>14}{row.get('stats', '-'):>14}")

    sem = [o for o in outcomes if o.case.category == "semantic"]
    if sem:
        out("\nSEMANTIC: plan actually used and verifier work")
        for label in ("A", "B", "Bsmall"):
            mine = [o for o in sem if o.engine == label]
            plans = Counter(o.strategy for o in mine)
            out(f"  engine {label:<7} runs={len(mine):>4} pass={sum(o.ok for o in mine):>4}  plans used: " + ", ".join(f"{k}={v}" for k, v in plans.items()))
        errors = [o for o in outcomes if o.case.category == "semantic_rules"]
        out(f"  placement/limit/budget rules: {sum(o.ok for o in errors)}/{len(errors)} refused with the right code")

    out("\nERROR CODES SEEN (cases that expected an error)")
    codes = Counter(o.error_code for o in outcomes if o.case.expect_error is not None and o.error_code)
    for code, count in codes.most_common():
        out(f"  {code:<44}{count:>5}")

    slow = sorted(outcomes, key=lambda o: -o.ms)[:5]
    out("\nSLOWEST EXECUTIONS")
    for o in slow:
        out(f"  {o.ms:7.0f} ms  [{o.engine}] {o.case.category}: {o.case.name}")

    failures = [o for o in outcomes if not o.ok]
    if failures:
        out(f"\nFAILURES ({len(failures)})")
        for o in failures[:40]:
            out(f"  [{o.case.category}] [{o.engine}] {o.case.name}\n      {o.detail}")
    return "\n".join(lines), not failures


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else 20261003
    only = set(argv[argv.index("--only") + 1].split(",")) if "--only" in argv else None
    report_path = argv[argv.index("--report") + 1] if "--report" in argv else None
    rng = random.Random(seed)
    matrix = Matrix(verbose)
    started = time.perf_counter()
    cases = [*build_relational_cases(matrix.oracle, rng), *build_limit_boundary_cases(matrix.oracle), *build_error_cases(matrix.oracle), *semantic_error_cases()]
    wanted = lambda category: only is None or category in only
    print(f"== generated {len(cases)} relational/error cases, {len(build_semantic_cases())} semantic cases, seed={seed}")
    for case in cases:
        if wanted(case.category):
            matrix.run_case(case)
    if wanted("semantic"):
        for case in build_semantic_cases():
            matrix.run_semantic(case)
    if wanted("optimizer"):
        for case in build_optimizer_cases(rng):
            matrix.run_optimizer(case)
    text, ok = report(matrix, time.perf_counter() - started)
    print(text)
    if report_path:
        with open(report_path, "w") as handle:
            handle.write(text + "\n")
    matrix.oracle.close()
    matrix.connections.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
