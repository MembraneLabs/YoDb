"""Joins between two datasets in different sources, against a real PostgreSQL, compared with native SQL joins.

``buyer`` (50,000 rows; its score in a second source) is joined to ``purchase`` (300,000 rows,
skewed towards a few buyers; its carrier in a second source) over the declared ``buyer_purchases``
relationship.  The four sources are four tables read through four connection references.

Every generated join runs with and without statistics.  A join must give exactly the rows, order
and page of the equivalent SQL join, or refuse with one of YoDb's guard errors; it may never give
different rows.  The report also counts the joins that were possible but refused (the planner
started from the larger side), by mode.

    PYTHONPATH=src .venv/bin/python tests/e2e/run_joins.py [-v] [--seed N]

Set up like run_e2e.py, then load seed_scale.sql.  Exit status is non-zero on any wrong result.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
import random
import sys
import time

from run_matrix import Matrix
from yodb.errors import YoDbError

GUARDS = {"query_row_limit_exceeded", "query_coordinator_limit_exceeded"}
DRIVER_CAP, JOINED_CAP, PROBE_CAP = 10_000, 50_000, 50_000

# (YoDb filter, SQL over b = buyers with their score) -- the root side
LEFT = {
    "none": (None, "TRUE"),
    "region = r07": ({"field": "region", "op": "eq", "value": "r07"}, "b.region = 'r07'"),
    "tier = enterprise": ({"field": "tier", "op": "eq", "value": "enterprise"}, "b.tier = 'enterprise'"),
    "score >= 90": ({"field": "score", "op": "gte", "value": 90}, "b.score >= 90"),
    "score is null": ({"field": "score", "op": "is_null"}, "b.score IS NULL"),
    "region in r01,r02 and tier != free": (
        {"all": [{"field": "region", "op": "in", "value": ["r01", "r02"]}, {"field": "tier", "op": "ne", "value": "free"}]},
        "b.region IN ('r01','r02') AND b.tier <> 'free'"),
    "joined 2021-03": (
        {"all": [{"field": "joined_at", "op": "gte", "value": "2021-03-01T00:00:00Z"}, {"field": "joined_at", "op": "lt", "value": "2021-04-01T00:00:00Z"}]},
        "b.joined_at >= '2021-03-01' AND b.joined_at < '2021-04-01'"),
    "name = buyer 123": ({"field": "name", "op": "eq", "value": "buyer 123"}, "b.name = 'buyer 123'"),
    "region = r03 or score >= 99": (
        {"any": [{"field": "region", "op": "eq", "value": "r03"}, {"field": "score", "op": "gte", "value": 99}]},
        "(b.region = 'r03' OR b.score >= 99)"),
}
# the traversed side, SQL over p = purchases with their carrier
RIGHT = {
    "none": (None, "TRUE"),
    "status = failed": ({"field": "status", "op": "eq", "value": "failed"}, "p.status = 'failed'"),
    "status = refunded": ({"field": "status", "op": "eq", "value": "refunded"}, "p.status = 'refunded'"),
    "amount >= 995": ({"field": "amount", "op": "gte", "value": 995}, "p.amount >= 995"),
    "carrier = dhl and amount >= 950": (
        {"all": [{"field": "carrier", "op": "eq", "value": "dhl"}, {"field": "amount", "op": "gte", "value": 950}]},
        "p.carrier = 'dhl' AND p.amount >= 950"),
    "carrier is null and status = paid and amount < 3": (
        {"all": [{"field": "carrier", "op": "is_null"}, {"field": "status", "op": "eq", "value": "paid"}, {"field": "amount", "op": "lt", "value": 3}]},
        "p.carrier IS NULL AND p.status = 'paid' AND p.amount < 3"),
    "purchased 2022-06": (
        {"all": [{"field": "purchased_at", "op": "gte", "value": "2022-06-01T00:00:00Z"}, {"field": "purchased_at", "op": "lt", "value": "2022-06-02T00:00:00Z"}]},
        "p.purchased_at >= '2022-06-01' AND p.purchased_at < '2022-06-02'"),
    "amount < 5 or status = failed": (
        {"any": [{"field": "amount", "op": "lt", "value": 5}, {"field": "status", "op": "eq", "value": "failed"}]},
        "(p.amount < 5 OR p.status = 'failed')"),
}
ORDERS = [
    [], [("name", False)], [("purchase.amount", True)], [("purchase.amount", True), ("name", False)],
    [("region", False), ("purchase.status", False)], [("score", True), ("purchase.id", True)], [("joined_at", True)],
]
SQL_COLUMN = {
    "id": "b.buyer_id", "name": "b.name", "region": "b.region", "tier": "b.tier", "joined_at": "b.joined_at", "score": "b.score",
    "purchase.id": "p.purchase_id", "purchase.amount": "p.amount", "purchase.status": "p.status", "purchase.carrier": "p.carrier",
    "purchase.purchased_at": "p.purchased_at", "purchase.buyer_id": "p.buyer_id",
}
LEFT_COLUMNS = ["id", "name", "region", "score"]
RIGHT_COLUMNS = ["id", "amount", "status", "carrier"]
OUTPUT = [*LEFT_COLUMNS, *(f"purchase.{c}" for c in RIGHT_COLUMNS)]

# the oracle reads pre-joined copies of the same tables (see seed_scale.sql), so that a SQL join is fast
BUYERS = "scale.buyers_flat b"
PURCHASES = "scale.purchases_flat"


def join_sql(left_sql, right_sql, optional, order, columns, limit):
    kind = "LEFT" if optional else "INNER"
    order_sql = ", ".join(f"{SQL_COLUMN[c]} {'DESC' if d else 'ASC'}" for c, d in order)
    order_sql = (order_sql + ", " if order_sql else "") + "b.buyer_id ASC, p.purchase_id ASC"
    return (f"SELECT {', '.join(SQL_COLUMN[c] for c in columns)} FROM {BUYERS} {kind} JOIN {PURCHASES} p ON p.buyer_id = b.buyer_id AND ({right_sql}) "
            f"WHERE ({left_sql}) ORDER BY {order_sql} LIMIT {limit}")


def query(left, right, optional, order, first, **extra):
    step = {"relationship": "buyer_purchases", "as": "purchase", "select": ["amount", "status", "carrier"]}
    if right is not None:
        step["where"] = right
    if optional:
        step["optional"] = True
    raw = {"from": {"dataset": "buyer"}, "select": ["name", "region", "score"], "page": {"first": first}, "traverse": [step]}
    if left is not None:
        raw["where"] = left
    if order:
        raw["order_by"] = [{"field": c, "direction": "desc" if d else "asc"} for c, d in order]
    return {**raw, **extra}


def normalize(value):
    return value.replace(tzinfo=UTC) if isinstance(value, datetime) and value.tzinfo is None else value


class Runner:
    def __init__(self, verbose: bool) -> None:
        self.verbose = verbose
        self.matrix = Matrix(False)
        self.oracle = self.matrix.oracle
        self.engines = {"rules": self.matrix.engine(), "stats": self.matrix.engine(statistics=self.matrix.statistics)}
        self.results: list[tuple[str, str, bool, str]] = []
        self.timings: list[tuple[float, str]] = []
        self.refused_feasible = Counter()
        self.driver_choice = defaultdict(Counter)
        self.feasible_total = 0
        self.last_error = ""
        self.refusals = []

    def check(self, group, name, ok, detail=""):
        self.results.append((group, name, bool(ok), detail))
        if self.verbose or not ok:
            print(f"{'PASS' if ok else 'FAIL'} [{group}] {name}" + (f"  -- {detail}" if detail and (self.verbose or not ok) else ""))

    def scalar(self, sql):
        with self.oracle.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchone()[0]

    def rows(self, sql):
        with self.oracle.cursor() as cursor:
            cursor.execute(sql)
            return [tuple(normalize(v) for v in row) for row in cursor.fetchall()]

    def run(self, mode, raw, timeout=60):
        self.matrix.executor.log.clear()
        started = time.perf_counter()
        try:
            result = self.engines[mode].execute(raw, timeout_seconds=timeout)
            outcome = ("rows", [tuple(normalize(r.get(c)) for c in OUTPUT) for r in result.rows])
        except YoDbError as error:
            outcome = ("error", error.code.value)
            self.last_error = error.detail.message
        elapsed = time.perf_counter() - started
        return outcome, elapsed

    # ------------------------------------------------------------------------------------------------

    def generated(self, rng):
        for index in range(150):
            (lname, (left, left_sql)), (rname, (right, right_sql)) = rng.choice(list(LEFT.items())), rng.choice(list(RIGHT.items()))
            optional, order, first = rng.random() < 0.3, rng.choice(ORDERS), rng.choice([5, 50, 500])
            n_left = self.scalar(f"SELECT count(*) FROM {BUYERS} WHERE ({left_sql})")
            n_right = self.scalar(f"SELECT count(*) FROM {PURCHASES} p WHERE ({right_sql})")
            n_pairs = self.scalar(f"SELECT count(*) FROM {BUYERS} {'LEFT' if optional else 'INNER'} JOIN {PURCHASES} p ON p.buyer_id = b.buyer_id AND ({right_sql}) WHERE ({left_sql})")
            driver_size = n_left if optional else min(n_left, n_right)
            feasible = driver_size <= DRIVER_CAP and n_pairs <= JOINED_CAP
            expected = self.rows(join_sql(left_sql, right_sql, optional, order, OUTPUT, first))
            raw = query(left, right, optional, order, first)
            name = f"#{index} left[{lname}] {'LEFT' if optional else 'INNER'} right[{rname}] order={[c for c, _ in order]} first={first} (left {n_left}, right {n_right}, pairs {n_pairs})"
            self.feasible_total += feasible
            for mode in ("rules", "stats"):
                (kind, payload), elapsed = self.run(mode, raw)
                self.timings.append((elapsed, f"[{mode}] {name}"))
                if kind == "rows":
                    self.check("generated", f"[{mode}] {name}", payload == expected, f"{len(payload)} rows vs oracle {len(expected)}")
                    note = self.engines[mode].explain(raw).optimizer
                    self.driver_choice[mode][next((n for n in note if n.startswith("driver=")), "?")] += 1
                else:
                    self.check("generated", f"[{mode}] {name}", payload in GUARDS, f"refused with {payload}")
                    self.refused_feasible[mode] += feasible
                    if feasible:
                        self.refusals.append((mode, name[:90], self.last_error))

    def batching(self):
        """A right-hand filter that matches thousands of purchases from thousands of buyers is probed in key batches."""

        right = {"field": "amount", "op": "gte", "value": 985}
        sql_right = "p.amount >= 985"
        n_right = self.scalar(f"SELECT count(*) FROM {PURCHASES} p WHERE {sql_right}")
        n_keys = self.scalar(f"SELECT count(DISTINCT buyer_id) FROM {PURCHASES} p WHERE {sql_right} AND buyer_id IS NOT NULL")
        raw = query({"field": "region", "op": "in", "value": [f"r{i:02d}" for i in range(20)]}, right, False, [], 500)
        expected = self.rows(join_sql("TRUE", sql_right, False, [], OUTPUT, 500))
        # the 10,000-row driver cap applies to whichever side drives: this one has to drive from the right
        (kind, payload), elapsed = self.run("stats", raw)
        probes = [e for e in self.matrix.executor.log if e["source"] == "buyers_db"]
        self.check("batching", f"{n_right} purchases from {n_keys} buyers: correct page", kind == "rows" and payload == expected, f"{kind}: {payload if kind == 'error' else len(payload)}")
        self.check("batching", "the buyers were read in batches of at most 1,000 keys", len(probes) >= -(-n_keys // 1000) and all(e["rows"] <= 1000 for e in probes),
                   f"{len(probes)} reads for {n_keys} keys")
        print(f"   batching: {n_right} purchases, {n_keys} distinct buyers -> {len(probes)} probe reads, {elapsed * 1000:.0f} ms")

    def explicit(self):
        cases = {
            "unfiltered inner join is refused (50,000 buyers, 300,000 purchases)": query(None, None, False, [], 50),
            "unfiltered left join is refused": query(None, None, True, [], 50),
            "an unknown relationship": {**query(None, None, False, [], 5), "traverse": [{"relationship": "nope"}]},
        }
        for name, raw in cases.items():
            (kind, payload), _ = self.run("stats", raw)
            expected = kind == "error"
            self.check("explicit", name, expected and (payload in GUARDS or payload == "relationship_not_found"), f"{kind}: {payload}")
        # the reverse direction of a bidirectional relationship: the purchases of one buyer, walked from the purchase
        raw = {"from": {"dataset": "purchase"}, "select": ["amount", "status"], "page": {"first": 10},
               "where": {"field": "status", "op": "eq", "value": "failed"},
               "order_by": [{"field": "amount", "direction": "desc"}],
               "traverse": [{"relationship": "buyer_purchases", "as": "buyer", "direction": "reverse", "select": ["name", "region"],
                             "where": {"field": "region", "op": "eq", "value": "r07"}}]}
        expected = self.rows(
            "SELECT p.purchase_id, p.amount, p.status, b.buyer_id, b.name, b.region FROM scale.purchases p JOIN scale.buyers b ON b.buyer_id = p.buyer_id "
            "WHERE p.status = 'failed' AND b.region = 'r07' ORDER BY p.amount DESC, p.purchase_id, b.buyer_id LIMIT 10")
        self.matrix.executor.log.clear()
        try:
            got = [(r["id"], r["amount"], r["status"], r["buyer.id"], r["buyer.name"], r["buyer.region"]) for r in self.engines["stats"].execute(raw, timeout_seconds=60).rows]
            self.check("explicit", "reverse traversal from the purchase to its buyer", got == expected, f"{len(got)} rows")
        except YoDbError as error:
            self.check("explicit", "reverse traversal from the purchase to its buyer", False, error.code.value)
        # a time limit covers the whole join
        (kind, payload), elapsed = self.run("rules", query({"field": "region", "op": "eq", "value": "r07"}, None, False, [], 5), timeout=0.002)
        self.check("explicit", "a join stops within its time limit", kind == "error" and payload == "query_timeout" and elapsed < 2, f"{kind}: {payload} after {elapsed * 1000:.0f} ms")


def main(argv):
    verbose = "-v" in argv
    seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv else 20261005
    runner = Runner(verbose)
    started = time.perf_counter()
    runner.generated(random.Random(seed))
    runner.batching()
    runner.explicit()
    by_group = defaultdict(lambda: [0, 0])
    for group, _, ok, _ in runner.results:
        by_group[group][0 if ok else 1] += 1
    failed = [r for r in runner.results if not r[2]]
    print(f"\n== {len(runner.results) - len(failed)} passed, {len(failed)} failed in {time.perf_counter() - started:.0f}s")
    for group, (passed, bad) in by_group.items():
        print(f"   {group:<10}{passed:>5} passed{bad:>5} failed")
    print(f"\n   joins a SQL join could answer that YoDb refused with a guard error "
          f"(of {runner.feasible_total}): without statistics {runner.refused_feasible['rules']}, with statistics {runner.refused_feasible['stats']}")
    for mode, counts in runner.driver_choice.items():
        print(f"   driving side chosen ({mode}): " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    why = Counter()
    for mode, _, message in runner.refusals:
        import re as _re
        why[(mode, _re.sub(r"\d+", "N", message))] += 1
    for (mode, message), count in why.most_common(8):
        print(f"   refused though possible [{mode}] x{count}: {message[:150]}")
    if verbose:
        for mode, name, message in runner.refusals:
            if mode == "stats":
                print(f"   refused [{mode}] {name} :: {message[:70]}")
    print("   slowest: " + "; ".join(f"{t * 1000:.0f} ms {n[:70]}" for t, n in sorted(runner.timings, reverse=True)[:3]))
    for group, name, ok, detail in runner.results:
        if not ok:
            print(f"   FAILED [{group}] {name}: {detail}")
    runner.oracle.close()
    runner.matrix.connections.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
