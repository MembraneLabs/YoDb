"""End-to-end YoDb run against a real PostgreSQL (see docs/dev/local-postgres-e2e.md).

For every case it prints the logical query, the physical plan, the SQL each
source actually received (with parameters and row counts), the result rows, and
whether the result equals an independent native SQL join computed by this
harness. Usage:

    PYTHONPATH=src .venv/bin/python tests/e2e/run_e2e.py [-q] [name-substring ...]
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from datetime import UTC, datetime
from dataclasses import dataclass, field

import psycopg

from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.connections import (
    MappingPostgresConnectionResolver,
    PostgresConnectionAdapter,
    PostgresConnectionSettings,
)
from yodb.errors import YoDbError
from yodb.execution import PostgresQueryExecutionAdapter, QueryExecutionAdapterRegistry, QueryExecutionEngine
from yodb.inspection import InspectionAdapterBinding, PostgresCatalogValidator, PostgresSourceInspector, SourceInspectionRegistry
from yodb.runtime import InMemoryCatalogRuntime

HERE = Path(__file__).parent
CONNINFO = os.environ.get(
    "YODB_E2E_CONNINFO",
    "host=localhost port=55432 dbname=yodb_e2e user=yodb_ro password=yodb_ro",
)
REFS = ("e2e-crm", "e2e-billing", "e2e-support")

# --- native-SQL oracle (test harness only; YoDb never sees this) ---------------

FROM = (
    "crm.accounts a "
    "LEFT JOIN billing.customers b ON b.customer_id = a.account_id "
    "LEFT JOIN support.tickets_summary s ON s.customer_id = a.account_id"
)
COLUMN = {
    "id": "a.account_id", "name": "a.company_name", "status": "a.account_status", "seats": "a.seats",
    "signup_at": "a.signup_at", "is_vip": "a.is_vip", "country": "a.country",
    "plan": "b.plan", "mrr": "b.mrr", "auto_renew": "b.auto_renew",
    "tier": "s.tier", "open_tickets": "s.open_tickets",
}


@dataclass
class Case:
    name: str
    query: dict
    where: str = "TRUE"            # oracle WHERE over the aliases above
    order: str = "a.account_id"     # oracle ORDER BY (must include id tiebreak)
    limit: int | None = None
    expect_error: str | None = None  # substring of the error code, if an error is expected
    oracle_sql: str | None = None   # full override (non-customer datasets)
    note: str = ""


def q(select, where=None, order=None, first=100, dataset="customer", after=None):
    query = {"from": {"dataset": dataset}, "select": list(select), "page": {"first": first}}
    if after:
        query["page"]["after"] = after
    if where is not None:
        query["where"] = where
    if order:
        query["order_by"] = [{"field": f, "direction": d} for f, d in order]
    return query


def p(field_, op, value=None):
    predicate = {"field": field_, "op": op}
    if op not in ("is_null", "is_not_null"):
        predicate["value"] = value
    return predicate


def AND(*items): return {"all": list(items)}
def OR(*items): return {"any": list(items)}
def NOT(item): return {"not": item}


def customer(name, select, where=None, sql_where="TRUE", order=None, sql_order="a.account_id", first=100, **kw):
    return Case(name, q(select, where, order, first), sql_where, sql_order, first, **kw)


CASES = [
    # ---------------- single source (CRM): everything pushable ----------------
    customer("S01 eq", ["name", "status"], p("status", "eq", "active"), "a.account_status = 'active'"),
    customer("S02 in", ["name", "country"], p("country", "in", ["US", "UK"]), "a.country IN ('US','UK')"),
    customer("S03 not_in (NULL country excluded)", ["country"], p("country", "not_in", ["US"]), "a.country NOT IN ('US')"),
    customer("S04 ne (NULL name excluded)", ["name"], p("name", "ne", "Bravo Inc"), "a.company_name <> 'Bravo Inc'"),
    customer("S05 int range", ["name", "seats"], AND(p("seats", "gte", 10), p("seats", "lt", 60)), "a.seats >= 10 AND a.seats < 60"),
    customer("S06 timestamp range", ["name", "signup_at"], p("signup_at", "gte", "2024-01-01T00:00:00Z"), "a.signup_at >= '2024-01-01'"),
    customer("S07 bool", ["name", "is_vip"], p("is_vip", "eq", True), "a.is_vip"),
    customer("S08 is_null", ["name", "seats"], p("seats", "is_null"), "a.seats IS NULL"),
    customer("S09 is_not_null", ["name", "signup_at"], p("signup_at", "is_not_null"), "a.signup_at IS NOT NULL"),
    customer("S10 any + not", ["name", "status", "country"], OR(p("country", "eq", "DE"), NOT(p("status", "eq", "active"))),
             "(a.country = 'DE' OR NOT (a.account_status = 'active'))"),
    customer("S11 all(any)", ["name", "seats"], AND(p("status", "eq", "active"), OR(p("seats", "lt", 10), p("is_vip", "eq", True))),
             "a.account_status = 'active' AND (a.seats < 10 OR a.is_vip)"),
    customer("S12 order desc, limit (NULLS FIRST, id tiebreak)", ["name"], None, "TRUE", [("name", "desc")],
             "a.company_name DESC, a.account_id ASC", 5),
    customer("S13 order asc, limit (NULLS LAST, duplicate name tie)", ["name"], None, "TRUE", [("name", "asc")],
             "a.company_name ASC, a.account_id ASC", 4),
    customer("S14 empty result", ["name"], p("status", "eq", "nope"), "a.account_status = 'nope'"),
    customer("S15 select only id", ["id"], p("country", "eq", "IN"), "a.country = 'IN'"),
    customer("S16 page size only", ["name"], None, "TRUE", None, "a.account_id", 3),
    # ---------------- single source: coordinator-only operators ---------------
    customer("C01 contains is case-sensitive (coordinator)", ["name"], p("name", "contains", "Acme"),
             "position('Acme' in a.company_name) > 0", note="not pushed; Postgres sends all CRM rows"),
    customer("C02 starts_with (coordinator)", ["name"], p("name", "starts_with", "acme"), "left(a.company_name, 4) = 'acme'"),
    customer("C03 contains + order + limit stay local", ["name"], p("name", "contains", "o"),
             "position('o' in a.company_name) > 0", [("name", "asc")], "a.company_name ASC, a.account_id ASC", 3),
    # ---------------- multi-source (CRM + Billing [+ Support]) ----------------
    customer("M01 AND across sources", ["name", "plan"], AND(p("status", "eq", "active"), p("plan", "eq", "enterprise")),
             "a.account_status = 'active' AND b.plan = 'enterprise'"),
    customer("M02 OR across sources (nothing pushed)", ["name", "plan"], OR(p("status", "eq", "pending"), p("plan", "eq", "enterprise")),
             "(a.account_status = 'pending' OR b.plan = 'enterprise')"),
    customer("M03 NOT across sources", ["name", "plan"], NOT(p("plan", "eq", "basic")), "NOT (b.plan = 'basic')"),
    customer("M04 contributor-only filter", ["name"], p("plan", "eq", "pro"), "b.plan = 'pro'"),
    customer("M05 contributor is_null (the fixed bug)", ["name", "plan"], p("plan", "is_null"), "b.plan IS NULL",
             note="includes customers with NO billing row (c04,c09,c12) and the NULL-plan row (c06)"),
    customer("M06 contributor is_not_null", ["name", "plan"], p("plan", "is_not_null"), "b.plan IS NOT NULL"),
    customer("M07 contributor ne (missing row = NULL, excluded)", ["name", "plan"], p("plan", "ne", "basic"), "b.plan <> 'basic'"),
    customer("M08 contributor in", ["name", "plan"], p("plan", "in", ["pro", "basic"]), "b.plan IN ('pro','basic')"),
    customer("M09 contributor float range", ["name", "mrr"], p("mrr", "gt", 1000), "b.mrr > 1000"),
    customer("M10 contributor bool", ["name", "auto_renew"], p("auto_renew", "eq", True), "b.auto_renew"),
    customer("M11 three sources AND", ["name", "plan", "tier"],
             AND(p("status", "eq", "active"), p("plan", "eq", "enterprise"), p("tier", "eq", "gold")),
             "a.account_status = 'active' AND b.plan = 'enterprise' AND s.tier = 'gold'"),
    customer("M12 three sources OR", ["name", "plan", "tier"], OR(p("tier", "eq", "bronze"), p("plan", "eq", "pro")),
             "(s.tier = 'bronze' OR b.plan = 'pro')"),
    customer("M13 AND(anchor, OR(contributors))", ["name", "plan", "tier"],
             AND(p("status", "eq", "active"), OR(p("plan", "eq", "basic"), p("tier", "eq", "gold"))),
             "a.account_status = 'active' AND (b.plan = 'basic' OR s.tier = 'gold')"),
    customer("M14 order by contributor field (NULLs)", ["name", "plan"], None, "TRUE", [("plan", "desc")],
             "b.plan DESC, a.account_id ASC", 6),
    customer("M15 global page after filter", ["name", "plan"], p("plan", "eq", "enterprise"), "b.plan = 'enterprise'",
             [("name", "asc")], "a.company_name ASC, a.account_id ASC", 2),
    customer("M16 anchor is_null AND contributor is_null", ["name", "seats", "plan"],
             AND(p("seats", "is_null"), p("plan", "is_null")), "a.seats IS NULL AND b.plan IS NULL"),
    customer("M17 coordinator-only op AND contributor eq", ["name", "plan"],
             AND(p("name", "contains", "Acme"), p("plan", "eq", "enterprise")),
             "position('Acme' in a.company_name) > 0 AND b.plan = 'enterprise'"),
    customer("M18 any within one contributor", ["name", "plan"], OR(p("plan", "eq", "pro"), p("plan", "eq", "basic")),
             "(b.plan = 'pro' OR b.plan = 'basic')", note="whole-expression push to a contributor is not implemented"),
    customer("M19 select contributor fields only, no filter", ["plan", "tier"]),
    customer("M20 filter field not selected", ["name"], p("tier", "eq", "gold"), "s.tier = 'gold'"),
    customer("M21 NOT(any(anchor, contributor))", ["name", "plan"], NOT(OR(p("status", "eq", "active"), p("plan", "eq", "enterprise"))),
             "NOT (a.account_status = 'active' OR b.plan = 'enterprise')"),
    customer("M22 all twelve fields", ["name", "status", "seats", "signup_at", "is_vip", "country", "plan", "mrr", "auto_renew", "tier", "open_tickets"],
             None, "TRUE", None, "a.account_id", 12),
    customer("M23 same-source AND on contributor, mixed ops", ["name", "mrr", "plan"],
             AND(p("plan", "ne", "basic"), p("mrr", "lt", 6000)), "b.plan <> 'basic' AND b.mrr < 6000"),
    # ---------------- single-source dataset with 10,500 rows -----------------
    Case("B01 pushable filter on big table", q(["label"], p("label", "in", ["event-1", "event-10500"]), dataset="event"),
         oracle_sql="SELECT event_id, label FROM crm.events WHERE label IN ('event-1','event-10500') ORDER BY event_id"),
    Case("B02 pushable order+limit on big table", q(["label"], p("label", "ne", "zzz"), [("label", "asc")], 3, "event"),
         oracle_sql="SELECT event_id, label FROM crm.events WHERE label <> 'zzz' ORDER BY label ASC, event_id ASC LIMIT 3"),
    Case("B03 coordinator op on big table hits the 10,000-row guard", q(["label"], p("label", "contains", "event-1"), dataset="event"),
         expect_error="row_limit"),
    # ---------------- error / safety cases -----------------------------------
    Case("E02 unknown field", q(["name", "ssn"]), expect_error=""),
    Case("E03 raw SQL is not accepted", {**q(["name"]), "sql": "SELECT 1"}, expect_error=""),
    Case("E04 unsupported operator", q(["name"], {"field": "name", "op": "like", "value": "%a%"}), expect_error=""),
    Case("E05 cursor not supported yet", q(["name"], after="abc"), expect_error="not_supported"),
    Case("E07 naive timestamp (no timezone) is rejected", q(["name"], p("signup_at", "gte", "2024-01-01T00:00:00")), expect_error="type_invalid"),
    Case("E06 type mismatch (string vs int)", q(["name"], p("seats", "eq", "many")), expect_error=""),
]


# --- harness ------------------------------------------------------------------

class RecordingExecutor(PostgresQueryExecutionAdapter):
    def __init__(self, connections):
        super().__init__(connections)
        self.log: list[dict] = []

    def execute(self, query, *, timeout_seconds=None):
        started = time.perf_counter()
        rows = super().execute(query, timeout_seconds=timeout_seconds)
        self.log.append({"source": query.source_name, "sql": query.sql, "params": query.parameters,
                         "rows": len(rows), "ms": (time.perf_counter() - started) * 1000})
        return rows


def oracle_rows(connection, case: Case):
    if case.oracle_sql:
        sql = case.oracle_sql
    else:
        fields = ["id", *[f for f in case.query["select"] if f != "id"]]
        sql = f"SELECT {', '.join(COLUMN[f] for f in fields)} FROM {FROM} WHERE {case.where} ORDER BY {case.order}"
        if case.limit is not None:
            sql += f" LIMIT {case.limit}"
    with connection.cursor() as cursor:
        cursor.execute(sql)
        # YoDb returns timestamps as UTC-aware; normalize the oracle the same way.
        utc = lambda v: v.replace(tzinfo=UTC) if isinstance(v, datetime) and v.tzinfo is None else v
        return [tuple(utc(v) for v in row) for row in cursor.fetchall()], sql


def format_explain(explanation) -> list[str]:
    lines = []
    for node in explanation.nodes:
        extra = []
        if node.fields:
            extra.append(f"fields={list(node.fields)}")
        if node.pushed_filter_fields:
            extra.append(f"pushed={list(node.pushed_filter_fields)}")
        if node.residual_filter:
            extra.append("residual_filter=yes")
        if node.ordering:
            extra.append(f"order={list(node.ordering)}")
        if node.limit is not None:
            extra.append(f"limit={node.limit}")
        if node.key_transfer_max_keys:
            extra.append(f"key_transfer<={node.key_transfer_max_keys}")
        lines.append(f"    {node.kind:<22}@{node.location:<12}{' '.join(extra)}")
    return lines


def main(argv: list[str]) -> int:
    quiet = "-q" in argv
    filters = [a for a in argv if not a.startswith("-")]
    resolver = MappingPostgresConnectionResolver({ref: PostgresConnectionSettings(conninfo=CONNINFO) for ref in REFS})
    connections = PostgresConnectionAdapter(resolver, max_size=4, acquire_timeout_seconds=10)
    inspector = PostgresSourceInspector(connections)
    runtime_registry = SourceInspectionRegistry([InspectionAdapterBinding(
        source_kind=inspector.source_kind, inspector=inspector, validator=PostgresCatalogValidator(),
    )])
    runtime = InMemoryCatalogRuntime(HERE / "catalog", runtime_registry)
    refresh = runtime.refresh()
    print(f"== catalog activation: {refresh.status.value}")
    if refresh.candidate:
        for name, state in refresh.candidate.sources.items():
            print(f"   source {name:<12} {state.status.value}")
            if state.validation:
                for finding in state.validation.findings:
                    print(f"      [{finding.severity.value}] {finding.message}")
    if runtime.active is None:
        print("catalog not activated; aborting")
        connections.close()
        return 2

    # A contributor whose identity column is not unique must never activate.
    bad = InMemoryCatalogRuntime(HERE / "catalog_dup", runtime_registry).refresh()
    print(f"\n== catalog with duplicate contributor identity: {bad.status.value}")
    for name, state in (bad.candidate.sources if bad.candidate else {}).items():
        for finding in (state.validation.findings if state.validation else ()):
            print(f"   [{finding.severity.value}] {name}: {finding.message}")
    dup_ok = bad.status.value == "rejected" and runtime.active is not None
    print(f"   => {'PASS (rejected before any query can run)' if dup_ok else 'FAIL (activated!)'}")

    executor = RecordingExecutor(connections)
    engine = QueryExecutionEngine(runtime, QueryCompilerRegistry([PostgresQueryCompiler()]), QueryExecutionAdapterRegistry([executor]))
    oracle_connection = psycopg.connect(CONNINFO)
    passed = 1 if dup_ok else 0
    failed = 0 if dup_ok else 1
    failures = [] if dup_ok else ["catalog with duplicate identity must be rejected"]
    for case in CASES:
        if filters and not any(f.lower() in case.name.lower() for f in filters):
            continue
        executor.log.clear()
        print(f"\n=== {case.name}")
        if case.note:
            print(f"    note: {case.note}")
        print(f"    query: {json.dumps(case.query)}")
        outcome = ""
        try:
            try:
                print("    plan:")
                print("\n".join(format_explain(engine.explain(case.query))))
            except YoDbError as error:
                print(f"    plan: <rejected at planning: {error.code.value}>")
            result = engine.execute(case.query, timeout_seconds=10)
            rows = result.rows
            for entry in executor.log:
                sql = entry["sql"].replace("\n", " ")
                print(f"    sql[{entry['source']}] {sql}  params={entry['params']}  -> {entry['rows']} rows ({entry['ms']:.1f} ms)")
            if case.expect_error is not None:
                outcome = "FAIL (expected an error, got rows)"
            else:
                fields = ["id", *[f for f in case.query["select"] if f != "id"]]
                got = [tuple(row.get(f) for f in fields) for row in rows]
                expected, oracle_sql = oracle_rows(oracle_connection, case)
                if not quiet:
                    for row in got[:12]:
                        print(f"      {row}")
                    if len(got) > 12:
                        print(f"      ... {len(got)} rows total")
                if got == expected:
                    outcome = f"PASS ({len(got)} rows match native SQL oracle)"
                else:
                    outcome = "FAIL (mismatch)"
                    print(f"      expected: {expected}\n      got:      {got}\n      oracle:   {oracle_sql}")
        except YoDbError as error:
            code = error.code.value
            for entry in executor.log:
                print(f"    sql[{entry['source']}] {entry['sql'].replace(chr(10), ' ')}  params={entry['params']}  -> {entry['rows']} rows")
            print(f"    error: {code}: {error.detail.message}")
            if case.expect_error is not None and case.expect_error in code:
                outcome = f"PASS (rejected with {code})"
            else:
                outcome = f"FAIL (unexpected error {code})"
        print(f"    => {outcome}")
        if outcome.startswith("PASS"):
            passed += 1
        else:
            failed += 1
            failures.append(case.name)
    oracle_connection.close()
    connections.close()
    print(f"\n== {passed} passed, {failed} failed")
    for name in failures:
        print(f"   FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
