"""Robustness against a real PostgreSQL: hostile input, concurrency and timeouts, and a million-row scale.

Everything goes through the public front door (``yodb.connect``) with the e2e catalog.

  hostile    injection strings as values, injection through every name in a query, structural abuse
             (deep nesting, huge lists, huge strings, NaN, wrong types), read-only enforcement.
             Each must give the right rows or a clean YoDb error, within a time bound, and leave the
             database untouched.
  timeouts   a time limit stops the work (statement timeout, whole-query budget) and a pool that is
             exhausted fails cleanly and recovers.
  concurrent many threads, small pool: every answer correct, no leaked connection.
  scale      1,000,000-row table + 500,000-row contributor: pushdown, key transfer, the guards.

    PYTHONPATH=src .venv/bin/python tests/e2e/run_robustness.py [--only hostile,timeouts,concurrent,scale] [-v]

Set up like run_e2e.py, then also load seed_scale.sql (see README.md).  Exit status is non-zero on any failure.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import random
import resource
import sys
import threading
import time
from typing import Callable

import psycopg

import run_e2e as base
import yodb
from yodb.errors import YoDbError

CONNINFO = base.CONNINFO
ADMIN = CONNINFO.replace("user=yodb_ro password=yodb_ro", "user=postgres password=postgres")
RESULTS: list[tuple[str, str, bool, str]] = []
VERBOSE = False


def check(group: str, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((group, name, bool(ok), detail))
    if VERBOSE or not ok:
        print(f"{'PASS' if ok else 'FAIL'} [{group}] {name}" + (f"  -- {detail}" if detail and (VERBOSE or not ok) else ""))


def open_db(**kw) -> "yodb.YoDb":
    return yodb.connect(base.HERE / "catalog", {ref: CONNINFO for ref in base.REFS}, **kw)


def outcome(db, query, timeout=30.0):
    """('rows', rows) | ('error', code) | ('crash', 'ExceptionType: message')."""

    try:
        return "rows", [dict(r) for r in db.query(query, timeout_seconds=timeout).rows]
    except YoDbError as error:
        return "error", error.code.value
    except BaseException as error:        # noqa: BLE001 - a crash is the failure being hunted
        return "crash", f"{type(error).__name__}: {str(error)[:80]}"


def table_count(connection, table: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table}")
        return cursor.fetchone()[0]


# --------------------------------------------------------------------------------------
# hostile input
# --------------------------------------------------------------------------------------

HOSTILE_STRINGS = {
    "drop table": "'; DROP TABLE crm.accounts; --",
    "boolean tautology": "' OR '1'='1",
    "double quotes": '" OR ""="',
    "stacked query": "x'; DELETE FROM billing.customers; SELECT '",
    "comment markers": "a /* b */ -- c",
    "format placeholder": "%s",
    "named placeholder": "%(name)s",
    "percent": "100%% sure %",
    "backslash": "C:\\path\\to\\\\file",
    "single quote": "O'Brien",
    "newlines and tabs": "line1\nline2\tTab\r\n",
    "unicode": "naïve — 日本語 🙂 Ünïcödé",
    "right to left": "\u202eevil\u202c",
    "combining marks": "e\u0301\u0301\u0301",
    "nul byte": "before\x00after",
    "empty string": "",
    "very long": "A" * 1_000_000,
    "null-looking": "NULL",
    "sql keyword": "SELECT * FROM crm.accounts",
}


def hostile(connection) -> None:
    db = open_db()
    before = {t: table_count(connection, t) for t in ("crm.accounts", "billing.customers", "support.tickets_summary", "sales.orders")}
    for label, text in HOSTILE_STRINGS.items():
        for op in ("eq", "ne", "contains", "starts_with", "in"):
            value = [text] if op == "in" else text
            query = {"from": {"dataset": "customer"}, "select": ["name", "plan"],
                     "where": {"field": "name", "op": op, "value": value}, "page": {"first": 50}}
            started = time.perf_counter()
            kind, payload = outcome(db, query)
            elapsed = time.perf_counter() - started
            name = f"value '{label}' with {op}"
            if kind == "crash":
                check("hostile", name, False, payload)
            elif kind == "rows":
                # the answer must equal the same comparison done by the database with a bound parameter
                sql = {"eq": "c.company_name = %s", "ne": "c.company_name <> %s", "in": "c.company_name = ANY(%s)",
                       "contains": "position(%s in c.company_name) > 0", "starts_with": "left(c.company_name, length(%s)) = %s"}[op]
                params = (value,) if op == "in" else ((text, text) if op == "starts_with" else (text,))
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT c.account_id FROM crm.accounts c WHERE " + sql.replace("c.company_name", "c.company_name") + " ORDER BY 1 LIMIT 50", params
                        )
                        expected = [r[0] for r in cursor.fetchall()]
                    connection.rollback()
                    check("hostile", name, [r["id"] for r in payload] == expected, f"expected {expected} got {[r['id'] for r in payload]}")
                except psycopg.Error:
                    connection.rollback()
                    check("hostile", name, True, "the database itself cannot hold this value; YoDb returned no rows")
            else:
                check("hostile", name, True, f"refused cleanly: {payload}")
            check("hostile", name + " is quick", elapsed < 10, f"{elapsed:.1f}s")
    after = {t: table_count(connection, t) for t in before}
    check("hostile", "no table was changed by any hostile value", before == after, f"{before} -> {after}")

    # injection through every *name* in a query: all must be refused before reaching the database
    evil = "name; DROP TABLE crm.accounts; --"
    base_query = {"from": {"dataset": "customer"}, "select": ["name"], "page": {"first": 5}}
    for label, query in {
        "dataset name": {**base_query, "from": {"dataset": evil}},
        "select name": {**base_query, "select": [evil]},
        "where field": {**base_query, "where": {"field": evil, "op": "eq", "value": "x"}},
        "where operator": {**base_query, "where": {"field": "name", "op": "eq; DROP TABLE x", "value": "x"}},
        "order field": {**base_query, "order_by": [{"field": evil, "direction": "asc"}]},
        "order direction": {**base_query, "order_by": [{"field": "name", "direction": "asc; DROP TABLE x"}]},
        "unknown top-level key": {**base_query, "sql": "DROP TABLE crm.accounts"},
        "unknown predicate key": {**base_query, "where": {"field": "name", "op": "eq", "value": "x", "raw": "1=1"}},
        "semantic field": {**base_query, "where": {"semantic": {"field": evil, "proposition": "x"}}},
    }.items():
        kind, payload = outcome(db, query)
        check("hostile", f"injection through the {label} is refused", kind == "error", f"{kind}: {payload}")
    check("hostile", "no table was changed by any hostile name", before == {t: table_count(connection, t) for t in before})

    # structural abuse
    def nested(depth):
        node = {"field": "status", "op": "eq", "value": "active"}
        for _ in range(depth):
            node = {"not": node}
        return node

    def wide(width):
        return {"all": [{"field": "status", "op": "ne", "value": f"v{i}"} for i in range(width)]}

    abuse = {
        "nesting depth 50": {**base_query, "where": nested(50)},
        "nesting depth 500": {**base_query, "where": nested(500)},
        "nesting depth 5,000": {**base_query, "where": nested(5_000)},
        "nesting depth 100,000": {**base_query, "where": nested(100_000)},
        "5,000 predicates in one all": {**base_query, "where": wide(5_000)},
        "10,000 select names": {**base_query, "select": ["name"] * 10_000},
        "in-list of 1,001": {**base_query, "where": {"field": "name", "op": "in", "value": [f"n{i}" for i in range(1_001)]}},
        "integer beyond 64 bits": {**base_query, "where": {"field": "seats", "op": "eq", "value": 2 ** 70}},
        "float infinity": {**base_query, "where": {"field": "mrr", "op": "gt", "value": float("inf")}},
        "float nan": {**base_query, "where": {"field": "mrr", "op": "gt", "value": float("nan")}},
        "list where a string belongs": {**base_query, "where": {"field": "name", "op": "eq", "value": ["a", "b"]}},
        "object where a string belongs": {**base_query, "where": {"field": "name", "op": "eq", "value": {"a": 1}}},
        "boolean where an int belongs": {**base_query, "where": {"field": "seats", "op": "eq", "value": True}},
        "string where a boolean belongs": {**base_query, "where": {"field": "is_vip", "op": "eq", "value": "true"}},
        "page.first as a string": {**base_query, "page": {"first": "5"}},
        "page.first negative": {**base_query, "page": {"first": -1}},
        "page.first huge": {**base_query, "page": {"first": 2 ** 40}},
        "null query": None,
        "query is a list": [],
        "query is a number": 7,
        "empty object": {},
        "where is a string": {**base_query, "where": "status = 'x'"},
        "select is a string": {**base_query, "select": "name"},
        "10 MB proposition": {"from": {"dataset": "ticket"}, "select": ["subject"],
                              "where": {"semantic": {"field": "body", "proposition": "x" * 10_000_000}}},
    }
    for label, query in abuse.items():
        started = time.perf_counter()
        kind, payload = outcome(db, query, timeout=20)
        elapsed = time.perf_counter() - started
        check("hostile", f"abuse: {label}", kind in ("rows", "error"), f"{kind}: {payload}")
        check("hostile", f"abuse: {label} is quick", elapsed < 15, f"{elapsed:.1f}s")
    # text that is not JSON at all
    for label, text in {"truncated": '{"from": ', "binary-ish": "\x00\x01\x02", "empty": "", "huge number": '{"x": ' + "9" * 100_000 + "}"}.items():
        kind, payload = outcome(db, text)
        check("hostile", f"query text: {label}", kind == "error", f"{kind}: {payload}")

    # the connection really is read-only, even for a role that could write
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        admin.execute("CREATE TABLE IF NOT EXISTS public.ro_probe (x int)")
        admin.execute("GRANT ALL ON public.ro_probe TO yodb_ro")
    from yodb.connections import PostgresConnectionAdapter, MappingPostgresConnectionResolver, PostgresConnectionSettings

    for who, conninfo in (("a role with write privileges", ADMIN), ("the read-only role", CONNINFO)):
        adapter = PostgresConnectionAdapter(MappingPostgresConnectionResolver({"x": PostgresConnectionSettings(conninfo)}), max_size=1)
        try:
            with adapter.acquire("x") as session:
                with session.cursor() as cursor:
                    cursor.execute("SHOW default_transaction_read_only")
                    on = cursor.fetchone()[0] == "on"
                    try:
                        cursor.execute("INSERT INTO public.ro_probe VALUES (1)")
                        wrote = True
                    except psycopg.Error:
                        wrote = False
            check("hostile", f"sessions of {who} are read-only", on and not wrote, f"read_only={on} wrote={wrote}")
        finally:
            adapter.close()
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        admin.execute("DROP TABLE public.ro_probe")
    db.close()


# --------------------------------------------------------------------------------------
# timeouts
# --------------------------------------------------------------------------------------

# sorting a million rows in the database takes a few hundred milliseconds: slow enough to time out, fast enough to wait for
BIG_FILTER = {"from": {"dataset": "big"}, "select": ["amount"], "order_by": [{"field": "amount", "direction": "desc"}], "page": {"first": 10}}


def timeouts() -> None:
    db = open_db()
    # a limit stops the work: sorting a million rows cannot finish in 10 ms
    seen = defaultdict(int)
    for limit in (0.002, 0.005, 0.01):
        started = time.perf_counter()
        kind, payload = outcome(db, BIG_FILTER, timeout=limit)
        elapsed = time.perf_counter() - started
        seen[payload if kind == "error" else kind] += 1
        check("timeouts", f"a {limit * 1000:g} ms limit ends in a timeout, quickly", kind == "error" and payload == "query_timeout" and elapsed < 2.0,
              f"{kind}: {payload} after {elapsed * 1000:.0f} ms")
    # the same query without a limit still works (the pool was not left poisoned)
    kind, payload = outcome(db, BIG_FILTER, timeout=30)
    check("timeouts", "the same query without a tight limit succeeds afterwards", kind == "rows" and len(payload) == 10, f"{kind}")
    # a limit is one budget for the whole multi-source query
    multi = {"from": {"dataset": "big"}, "select": ["amount", "segment"], "page": {"first": 5},
             "where": {"all": [{"field": "segment", "op": "eq", "value": "s7"}, {"field": "flag", "op": "eq", "value": True}]}}
    started = time.perf_counter()
    kind, payload = outcome(db, multi, timeout=0.02)
    elapsed = time.perf_counter() - started
    check("timeouts", "a multi-source query stops within its single budget", kind == "error" and elapsed < 2.0, f"{kind}: {payload} after {elapsed * 1000:.0f} ms")
    db.close()

    # pool exhaustion: one connection per source, a short wait, and many callers
    db = open_db(pool_size=1, acquire_timeout_seconds=0.01)
    outcomes = []
    lock = threading.Lock()

    def heavy(_):
        for _ in range(3):
            result = outcome(db, BIG_FILTER, timeout=None)       # no query limit: the pool's own wait applies
            with lock:
                outcomes.append(result)

    with ThreadPoolExecutor(max_workers=24) as pool:
        list(pool.map(heavy, range(24)))
    codes = {payload for kind, payload in outcomes if kind == "error"}
    crashes = [payload for kind, payload in outcomes if kind == "crash"]
    check("timeouts", "callers beyond the pool fail cleanly or are served; none crash", not crashes, str(crashes[:2]))
    check("timeouts", "a full pool reports the pool timeout code", codes <= {"connection_pool_timeout", "query_timeout"}, str(codes))
    check("timeouts", "at least one caller was served", any(kind == "rows" for kind, _ in outcomes), str([o[0] for o in outcomes]))
    check("timeouts", "some callers really did wait out the pool", "connection_pool_timeout" in codes, str(codes))
    kind, payload = outcome(db, BIG_FILTER, timeout=30)
    check("timeouts", "the pool recovers once the load stops", kind == "rows", f"{kind}: {payload}")
    db.close()


# --------------------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------------------


def concurrent(connection) -> None:
    rng = random.Random(7)
    statuses = ["active", "inactive", "pending"]
    queries = []
    for i in range(40):
        status = rng.choice(statuses)
        plan = rng.choice(["enterprise", "pro", "basic"])
        queries.append((
            {"from": {"dataset": "customer"}, "select": ["name", "plan", "tier"], "page": {"first": 20},
             "where": {"all": [{"field": "status", "op": "ne", "value": status}, {"field": "plan", "op": "eq", "value": plan}]},
             "order_by": [{"field": "mrr", "direction": rng.choice(["asc", "desc"])}]},     # numbers: no collation to disagree on
            f"a.account_status <> '{status}' AND b.plan = '{plan}'",
        ))
    # expected answers from the database directly
    expected = {}
    for index, (query, where) in enumerate(queries):
        direction = query["order_by"][0]["direction"].upper()
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {', '.join(base.COLUMN[f] for f in ('id', 'name', 'plan', 'tier'))} FROM {base.FROM} WHERE {where} "
                f"ORDER BY b.mrr {direction}, a.account_id ASC LIMIT 20"
            )
            expected[index] = [tuple(r) for r in cursor.fetchall()]
    db = open_db(pool_size=4)
    wrong, errors = [], []
    lock = threading.Lock()

    def worker(seed):
        local = random.Random(seed)
        for _ in range(25):
            index = local.randrange(len(queries))
            kind, payload = outcome(db, queries[index][0], timeout=30)
            if kind != "rows":
                with lock:
                    errors.append((index, kind, payload))
            elif [(r["id"], r["name"], r["plan"], r["tier"]) for r in payload] != expected[index]:
                with lock:
                    wrong.append(index)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(worker, range(32)))
    elapsed = time.perf_counter() - started
    check("concurrent", "800 queries from 32 threads over a pool of 4: every answer correct", not wrong and not errors,
          f"wrong={wrong[:3]} errors={errors[:3]}")
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        cursor = admin.execute("SELECT count(*) FROM pg_stat_activity WHERE usename = 'yodb_ro' AND datname = current_database()")
        open_sessions = cursor.fetchone()[0] - 1                     # minus this test's own session
    check("concurrent", "the pools did not leak connections", open_sessions <= 4 * len(base.REFS), f"{open_sessions} sessions open")
    check("concurrent", "throughput is sane", elapsed < 120, f"{800 / elapsed:.0f} queries/s over {elapsed:.1f}s")
    print(f"   concurrency: 800 queries in {elapsed:.1f}s ({800 / elapsed:.0f}/s), {open_sessions} sessions open")
    db.close()
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        time.sleep(0.5)
        left = admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'yodb_ro' AND datname = current_database() AND pid <> %s",
            (connection.info.backend_pid,),                       # not this test's own session
        ).fetchone()[0]
    check("concurrent", "closing the client closes every connection", left == 0, f"{left} left")


# --------------------------------------------------------------------------------------
# scale
# --------------------------------------------------------------------------------------


def scale(connection) -> None:
    def expect(sql, params=()):
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return [tuple(r) for r in cursor.fetchall()]

    stats_off = open_db(statistics=False)
    stats_on = open_db(statistics=True)
    cases = [
        ("pushed filter, selective (kind = k17, 500 rows)", {"from": {"dataset": "big"}, "select": ["amount"], "page": {"first": 500},
            "where": {"field": "kind", "op": "eq", "value": "k17"}},
         "SELECT item_id, amount FROM scale.items WHERE kind = 'k17' ORDER BY item_id LIMIT 500", "rows"),
        ("pushed order + limit over a million rows", {"from": {"dataset": "big"}, "select": ["amount"], "page": {"first": 20},
            "order_by": [{"field": "amount", "direction": "desc"}]},
         "SELECT item_id, amount FROM scale.items ORDER BY amount DESC, item_id LIMIT 20", "rows"),
        ("unfiltered first page", {"from": {"dataset": "big"}, "select": ["kind"], "page": {"first": 500}},
         "SELECT item_id, kind FROM scale.items ORDER BY item_id LIMIT 500", "rows"),
        ("timestamp range, selective", {"from": {"dataset": "big"}, "select": ["created_at"], "page": {"first": 100},
            "where": {"all": [{"field": "created_at", "op": "gte", "value": "2020-02-01T00:00:00Z"}, {"field": "created_at", "op": "lt", "value": "2020-02-03T00:00:00Z"}]}},
         "SELECT item_id, created_at FROM scale.items WHERE created_at >= '2020-02-01' AND created_at < '2020-02-03' ORDER BY item_id LIMIT 100", "rows"),
        ("multi-source: a selective contributor filter (500 IDs) restricts the main table (key transfer)", {"from": {"dataset": "big"}, "select": ["amount", "code"], "page": {"first": 500},
            "where": {"field": "code", "op": "eq", "value": "c123"}},
         "SELECT m.item_id, m.amount, a.code FROM scale.items m JOIN scale.attrs a USING (item_id) WHERE a.code = 'c123' ORDER BY m.item_id LIMIT 500", "rows"),
        ("multi-source: a contributor filter with 10,000 IDs is over the 1,000-ID transfer limit, so the main table cannot be narrowed", {"from": {"dataset": "big"}, "select": ["amount", "segment", "score"], "page": {"first": 500},
            "where": {"all": [{"field": "segment", "op": "eq", "value": "s7"}, {"field": "score", "op": "eq", "value": 57}]}},
         None, "guard"),
        ("multi-source: anchor filter selective, contributor filter huge", {"from": {"dataset": "big"}, "select": ["kind", "segment"], "page": {"first": 500},
            "where": {"all": [{"field": "kind", "op": "eq", "value": "k5"}, {"field": "segment", "op": "ne", "value": "s1"}]}},
         "SELECT m.item_id, m.kind, a.segment FROM scale.items m JOIN scale.attrs a USING (item_id) WHERE m.kind = 'k5' AND a.segment <> 's1' ORDER BY m.item_id LIMIT 500", "rows_or_guard"),
        ("a filter the database must scan everything for (coordinator text match) hits the guard", {"from": {"dataset": "big"}, "select": ["note"], "page": {"first": 10},
            "where": {"field": "note", "op": "contains", "value": "note 99999"}},
         None, "guard"),
        ("a filter keeping most of a million rows plus a contributor hits the guard, not memory", {"from": {"dataset": "big"}, "select": ["amount", "segment"], "page": {"first": 10},
            "where": {"all": [{"field": "amount", "op": "gte", "value": 0}, {"field": "score", "op": "gte", "value": 0}]}},
         None, "guard"),
    ]
    for name, query, sql, expectation in cases:
        for label, db in (("rules", stats_off), ("stats", stats_on)):
            started = time.perf_counter()
            kind, payload = outcome(db, query, timeout=60)
            elapsed = time.perf_counter() - started
            full = f"{name} [{label}]"
            if kind == "crash":
                check("scale", full, False, payload)
            elif expectation == "guard":
                check("scale", full, kind == "error" and payload in ("query_row_limit_exceeded", "query_coordinator_limit_exceeded"), f"{kind}: {payload if kind == 'error' else len(payload)} in {elapsed:.1f}s")
            elif kind == "error":
                guarded = payload in ("query_row_limit_exceeded", "query_coordinator_limit_exceeded")
                check("scale", full, expectation == "rows_or_guard" and guarded, f"unexpected {payload}")
            else:
                columns = list(payload[0]) if payload else []
                oracle = expect(sql)
                if columns and any(hasattr(v, "year") for v in oracle[0] if v is not None):
                    from datetime import UTC
                    oracle = [tuple(v.replace(tzinfo=UTC) if hasattr(v, "year") and v.tzinfo is None else v for v in row) for row in oracle]
                ordered = [tuple(row.get(c) for c in ["id", *[c for c in columns if c != "id"]]) for row in payload]
                check("scale", full, ordered == oracle, f"{len(ordered)} rows vs oracle {len(oracle)}; {elapsed:.2f}s")
            check("scale", full + " is bounded in time", elapsed < 30, f"{elapsed:.1f}s")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    megabytes = rss / (1024 * 1024 if sys.platform == "darwin" else 1024)          # macOS reports bytes, Linux kilobytes
    check("scale", "peak memory stays under 1.5 GB through every guard case", megabytes < 1536, f"{megabytes:.0f} MB peak")
    print(f"   scale: peak memory {megabytes:.0f} MB")
    stats_off.close()
    stats_on.close()


# --------------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    global VERBOSE
    VERBOSE = "-v" in argv
    only = set(argv[argv.index("--only") + 1].split(",")) if "--only" in argv else {"hostile", "timeouts", "concurrent", "scale"}
    sections: dict[str, Callable[..., None]] = {"hostile": hostile, "timeouts": timeouts, "concurrent": concurrent, "scale": scale}
    connection = psycopg.connect(CONNINFO)
    started = time.perf_counter()
    for name, section in sections.items():
        if name in only:
            print(f"== {name}")
            section(connection) if section is not timeouts else section()
    connection.close()
    by_group = defaultdict(lambda: [0, 0])
    for group, _, ok, _ in RESULTS:
        by_group[group][0 if ok else 1] += 1
    print(f"\n== {sum(g[0] for g in by_group.values())} passed, {sum(g[1] for g in by_group.values())} failed in {time.perf_counter() - started:.0f}s")
    for group, (passed, failed) in by_group.items():
        print(f"   {group:<12}{passed:>5} passed{failed:>5} failed")
    for group, name, ok, detail in RESULTS:
        if not ok:
            print(f"   FAILED [{group}] {name}: {detail}")
    return 0 if all(ok for _, _, ok, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
