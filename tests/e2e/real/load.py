"""Load the real dataset into two *separate* Postgres servers: records and vectors.

records server : support.messages (text, split, word_count), triage.assignments (synthetic owner/priority),
                 support.truth (labelled intent; used only by the test oracle, never in the catalog)
vectors server : vec.message_vectors (message_id, embedding vector(N)) -- a different server and database

    PYTHONPATH=src .venv/bin/python tests/e2e/real/load.py --records "<admin conninfo>" --vectors "<admin conninfo>" [--model NAME]
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import urllib.request

import psycopg

HERE = Path(__file__).parent
DATA = Path(tempfile.gettempdir()) / "yodb-e2e-data"      # the downloaded CSVs are cached here
BASE = "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/master/banking_data/"
OWNERS = ("ann", "bob", "cai", "dee", "eli")


def fetch() -> list[dict]:
    """The BANKING77 customer-support messages (13,083 labelled with one of 77 intents)."""

    DATA.mkdir(parents=True, exist_ok=True)
    rows = []
    for split in ("train", "test"):
        path = DATA / f"banking_{split}.csv"
        if not path.exists():
            urllib.request.urlretrieve(f"{BASE}{split}.csv", path)
        with path.open(newline="") as handle:
            rows += [{"text": r["text"], "intent": r["category"], "split": split} for r in csv.DictReader(handle)]
    for index, row in enumerate(rows, 1):
        row["id"] = f"m{index:05d}"
        row["word_count"] = len(row["text"].split())
        digest = int(hashlib.sha256(row["id"].encode()).hexdigest(), 16)
        row["assigned"] = digest % 10 < 7                      # synthetic: 70% of messages are assigned
        row["owner"] = OWNERS[(digest >> 8) % len(OWNERS)]
        row["priority"] = 1 + (digest >> 16) % 5
    return rows


def load_records(conninfo: str, rows: list[dict]) -> None:
    with psycopg.connect(conninfo, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA IF EXISTS support, triage CASCADE; CREATE SCHEMA support; CREATE SCHEMA triage")
        cursor.execute("CREATE TABLE support.messages (message_id text PRIMARY KEY, body text, split text, word_count integer)")
        cursor.execute("CREATE TABLE support.truth (message_id text PRIMARY KEY, intent text)")
        cursor.execute("CREATE TABLE triage.assignments (message_id text PRIMARY KEY, owner text, priority integer)")
        with cursor.copy("COPY support.messages FROM STDIN") as copy:
            for r in rows:
                copy.write_row((r["id"], r["text"], r["split"], r["word_count"]))
        with cursor.copy("COPY support.truth FROM STDIN") as copy:
            for r in rows:
                copy.write_row((r["id"], r["intent"]))
        with cursor.copy("COPY triage.assignments FROM STDIN") as copy:
            for r in rows:
                if r["assigned"]:
                    copy.write_row((r["id"], r["owner"], r["priority"]))
        cursor.execute("""
            DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yodb_ro') THEN
              CREATE ROLE yodb_ro LOGIN PASSWORD 'yodb_ro'; END IF; END $$;
            GRANT USAGE ON SCHEMA support, triage TO yodb_ro;
            GRANT SELECT ON ALL TABLES IN SCHEMA support, triage TO yodb_ro;
            ANALYZE""")


def load_vectors(conninfo: str, rows: list[dict], vectors: list[tuple[float, ...]], dimensions: int) -> None:
    with psycopg.connect(conninfo, autocommit=True) as connection, connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cursor.execute("DROP SCHEMA IF EXISTS vec CASCADE; CREATE SCHEMA vec")
        cursor.execute(f"CREATE TABLE vec.message_vectors (message_id text PRIMARY KEY, embedding vector({dimensions}))")
        with cursor.copy("COPY vec.message_vectors FROM STDIN") as copy:
            for r, v in zip(rows, vectors):
                copy.write_row((r["id"], "[" + ",".join(repr(x) for x in v) + "]"))
        cursor.execute("""
            DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yodb_ro') THEN
              CREATE ROLE yodb_ro LOGIN PASSWORD 'yodb_ro'; END IF; END $$;
            GRANT USAGE ON SCHEMA vec TO yodb_ro; GRANT SELECT ON ALL TABLES IN SCHEMA vec TO yodb_ro;
            ANALYZE""")


def main(argv: list[str]) -> int:
    option = lambda name, default=None: argv[argv.index(name) + 1] if name in argv else default
    records, vectors = option("--records"), option("--vectors")
    model = option("--model", "minishlab/potion-base-8M")
    if not records or not vectors:
        print(__doc__)
        return 2
    sys.path.insert(0, str(HERE))
    from providers import FastEmbedder

    rows = fetch()
    print(f"messages: {len(rows)}  intents: {len({r['intent'] for r in rows})}")
    embedder = FastEmbedder(model)
    started = time.perf_counter()
    embedded = embedder.embed_many([r["text"] for r in rows])
    print(f"embedded with {model} ({embedder.dimensions} dims) in {time.perf_counter() - started:.1f}s")
    load_records(records, rows)
    load_vectors(vectors, rows, embedded, embedder.dimensions)
    print(json.dumps({"model": model, "dimensions": embedder.dimensions, "messages": len(rows)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
