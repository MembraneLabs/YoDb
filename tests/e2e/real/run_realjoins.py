"""Joins across two servers: customers on one, their support messages on another.

``crm.customers`` (2,000 rows) lives on the vectors server; ``support.messages`` (13,083 real messages)
on the records server.  No SQL join can span them.  The oracle reads both tables into memory and joins
them in Python; YoDb's join must give exactly the same rows, order and page.  One case also puts a
semantic condition (judged by the dataset's own intent labels) on the messages side of the join.

    PYTHONPATH=src .venv/bin/python tests/e2e/real/run_realjoins.py [-v]

Set up the two servers and load them as for run_realdata.py.
"""

from __future__ import annotations

from functools import cmp_to_key
from pathlib import Path
import sys
import time

import psycopg

HERE = Path(__file__).parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import yodb                                                                    # noqa: E402
from providers import PROPOSITIONS, FastEmbedder, LabelVerifier               # noqa: E402
from run_realdata import RECORDS, VECTORS, write_catalog                       # noqa: E402
from yodb.errors import YoDbError                                              # noqa: E402
from yodb.semantic import SemanticExtension, SemanticPlanPreference, SemanticPolicy, SemanticRuntime   # noqa: E402

PROPOSITION = "the customer is asking when their card will arrive"


def compare(a, b, descending):
    if a is None and b is None:
        return 0
    if a is None:
        return -1 if descending else 1
    if b is None:
        return 1 if descending else -1
    if a == b:
        return 0
    result = -1 if a < b else 1
    return -result if descending else result


def oracle(customers, messages, *, left_ok=lambda c: True, right_ok=lambda m: True, optional=False, order=(), first=100,
           left_columns, right_columns, alias="message", left_key="id", right_key="customer_id"):
    pairs = []
    by_customer = {}
    for message in messages:
        if right_ok(message) and message[right_key] is not None:
            by_customer.setdefault(message[right_key], []).append(message)
    for customer in customers:
        if not left_ok(customer):
            continue
        matches = by_customer.get(customer[left_key], [])
        if not matches and optional:
            pairs.append((customer, None))
        pairs.extend((customer, m) for m in matches)

    def value(pair, column):
        left, right = pair
        if column.startswith(alias + "."):
            return None if right is None else right.get(column[len(alias) + 1:])
        return left.get(column)

    terms = [*order, ("id", False), (f"{alias}.id", False)]

    def cmp(a, b):
        for column, descending in terms:
            result = compare(value(a, column), value(b, column), descending)
            if result:
                return result
        return 0

    pairs.sort(key=cmp_to_key(cmp))
    columns = [*left_columns, *(f"{alias}.{c}" for c in right_columns)]
    return [{c: value(pair, c) for c in columns} for pair in pairs[:first]]


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    embedder = FastEmbedder("minishlab/potion-base-8M")
    with psycopg.connect(RECORDS) as records, psycopg.connect(VECTORS) as vectors:
        customers = [dict(zip(("id", "name", "region", "tier"), r)) for r in vectors.execute("SELECT customer_id, name, region, tier FROM crm.customers").fetchall()]
        truth = dict(records.execute("SELECT message_id, intent FROM support.truth").fetchall())
        messages = [
            dict(zip(("id", "body", "split", "word_count", "customer_id"), r))
            for r in records.execute("SELECT message_id, body, split, word_count, customer_id FROM support.messages").fetchall()
        ]
    for message in messages:
        message["intent"] = truth[message["id"]]
    print(f"{len(customers)} customers on the vectors server, {len(messages)} messages on the records server")

    verifier = LabelVerifier(truth)
    exact = SemanticExtension(
        SemanticRuntime(verifier, embedder, verification_batch_size=100),
        policy=SemanticPolicy(embedder=embedder.info, embedder_dimensions=embedder.dimensions, preference=SemanticPlanPreference.VERIFY_ALL, maximum_candidates=20_000),
    )
    catalog = write_catalog(embedder.info.model, embedder.dimensions)
    connections = {"e2e-records": RECORDS, "e2e-vectors": VECTORS}
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        if verbose or not ok:
            print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  -- {detail}" if detail and (verbose or not ok) else ""))

    def step(alias="message", **kw):
        return {"relationship": "customer_messages", "as": alias, **kw}

    def where(field, op, value=None):
        leaf = {"field": field, "op": op}
        if op not in ("is_null", "is_not_null"):
            leaf["value"] = value
        return leaf

    def run(db, name, raw, expected, *, columns):
        started = time.perf_counter()
        try:
            got = [{c: r.get(c) for c in columns} for r in db.query(raw, timeout_seconds=60).rows]
        except YoDbError as error:
            check(name, False, f"{error.code.value}: {error.detail.message}")
            return
        elapsed = time.perf_counter() - started
        check(name, got == expected, f"{len(got)} rows vs oracle {len(expected)}")
        notes = " ".join(db.explain(raw).optimizer)
        print(f"   {name}: {len(got)} rows in {elapsed * 1000:.0f} ms  [{notes}]")

    left_cols, right_cols = ["id", "name", "region"], ["id", "split", "word_count"]
    columns = [*left_cols, *(f"message.{c}" for c in right_cols)]
    with yodb.connect(catalog, connections, semantic=exact) as db:
        raw = {"from": {"dataset": "customer"}, "select": ["name", "region"], "where": where("region", "eq", "r3"),
               "traverse": [step(select=["split", "word_count"], where={"all": [where("split", "eq", "test"), where("word_count", "gte", 12)]})],
               "order_by": [{"field": "message.word_count", "direction": "desc"}, {"field": "name", "direction": "asc"}], "page": {"first": 50}}
        run(db, "customers of region r3 with long test messages (inner)", raw, oracle(
            customers, messages, left_ok=lambda c: c["region"] == "r3", right_ok=lambda m: m["split"] == "test" and m["word_count"] >= 12,
            order=[("message.word_count", True), ("name", False)], first=50, left_columns=left_cols, right_columns=right_cols), columns=columns)

        raw = {"from": {"dataset": "customer"}, "select": ["name", "region"], "where": where("tier", "eq", "team"),
               "traverse": [step(select=["split", "word_count"], where=where("word_count", "gte", 60), optional=True)],
               "order_by": [{"field": "region", "direction": "desc"}], "page": {"first": 500}}
        expected = oracle(customers, messages, left_ok=lambda c: c["tier"] == "team", right_ok=lambda m: m["word_count"] >= 60, optional=True,
                          order=[("region", True)], first=500, left_columns=left_cols, right_columns=right_cols)
        run(db, "team customers with their very long messages, or none (left join)", raw, expected, columns=columns)
        check("the left join kept customers with no match", any(r["message.id"] is None for r in expected) and any(r["message.id"] is not None for r in expected))

        # a selective message filter: the messages drive, and customers are read only for those keys
        raw = {"from": {"dataset": "customer"}, "select": ["name"], "traverse": [step(select=["word_count"], where=where("word_count", "gte", 70))],
               "order_by": [{"field": "message.word_count", "direction": "desc"}], "page": {"first": 100}}
        expected = oracle(customers, messages, right_ok=lambda m: m["word_count"] >= 70, order=[("message.word_count", True)], first=100,
                          left_columns=["id", "name"], right_columns=["id", "word_count"])
        run(db, "customers who wrote a very long message (the messages drive)", raw, expected, columns=["id", "name", "message.id", "message.word_count"])
        check("the traversed side drove the join", "driver=right" in db.explain(raw).optimizer, str(db.explain(raw).optimizer))

        # reverse: from a message to its customer
        raw = {"from": {"dataset": "message"}, "select": ["split"], "where": {"all": [where("word_count", "gte", 55), where("split", "eq", "train")]},
               "traverse": [{"relationship": "customer_messages", "as": "customer", "direction": "reverse", "select": ["name", "tier"], "where": where("tier", "eq", "pro")}],
               "order_by": [{"field": "word_count", "direction": "desc"}], "page": {"first": 40}}
        got = [dict(r) for r in db.query(raw, timeout_seconds=60).rows]
        by_customer = {c["id"]: c for c in customers}
        expected = sorted(
            ([m["id"], m["split"], m["customer_id"], by_customer[m["customer_id"]]["name"], by_customer[m["customer_id"]]["tier"], m["word_count"]]
             for m in messages if m["word_count"] >= 55 and m["split"] == "train" and by_customer[m["customer_id"]]["tier"] == "pro"),
            key=lambda r: (-r[5], r[0], r[2]))[:40]
        check("reverse: messages with their customer", [[r["id"], r["split"], r["customer.id"], r["customer.name"], r["customer.tier"]] for r in got] == [r[:5] for r in expected],
              f"{len(got)} rows vs {len(expected)}")

        # a semantic condition on the messages side, across two servers
        raw = {"from": {"dataset": "customer"}, "select": ["name"], "where": where("region", "eq", "r5"),
               "traverse": [step(select=["body"], where={"semantic": {"field": "body", "proposition": PROPOSITION}})], "page": {"first": 200}}
        intents = PROPOSITIONS[PROPOSITION]
        expected = oracle(customers, messages, left_ok=lambda c: c["region"] == "r5", right_ok=lambda m: m["intent"] in intents, first=200,
                          left_columns=["id", "name"], right_columns=["id", "body"])
        run(db, "customers of region r5 whose messages ask when a card will arrive (semantic, across servers)", raw, expected,
            columns=["id", "name", "message.id", "message.body"])

        # every customer joined to every message: 2,000 customers go to the messages side in two batches of keys
        raw = {"from": {"dataset": "customer"}, "select": ["name"], "traverse": [step(select=["word_count"])],
               "order_by": [{"field": "message.word_count", "direction": "desc"}, {"field": "name", "direction": "asc"}], "page": {"first": 25}}
        expected = oracle(customers, messages, order=[("message.word_count", True), ("name", False)], first=25,
                          left_columns=["id", "name"], right_columns=["id", "word_count"])
        run(db, "all customers with all their messages (two batches of keys)", raw, expected, columns=["id", "name", "message.id", "message.word_count"])

    failed = [r for r in results if not r[1]]
    print(f"\n== {len(results) - len(failed)} passed, {len(failed)} failed")
    for name, ok, detail in failed:
        print(f"   FAILED {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
