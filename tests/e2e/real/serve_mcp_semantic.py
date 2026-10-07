"""An MCP server over the real-data catalog *with* a semantic filter, for run_mcp.py.

`yodb mcp` cannot configure a semantic filter (it needs providers), so this builds the server from
Python, the way an application would:

    db = yodb.connect(catalog, connections, semantic=...)
    yodb.mcp_server.serve(db)

    python serve_mcp_semantic.py exact|shortlist

``exact`` verifies every candidate (the reference); ``shortlist`` always shortlists by vector search.  The judge is the dataset's own intent label.
"""

from __future__ import annotations

from pathlib import Path
import sys

import psycopg

HERE = Path(__file__).parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

import yodb                                                                    # noqa: E402
from providers import FastEmbedder, LabelVerifier                             # noqa: E402
from run_realdata import RECORDS, VECTORS, write_catalog                       # noqa: E402
from yodb.mcp_server import serve                                              # noqa: E402
from yodb.semantic import SemanticExtension, SemanticPlanPreference, SemanticPolicy, SemanticRuntime   # noqa: E402


def main(mode: str) -> None:
    embedder = FastEmbedder("minishlab/potion-base-8M")
    with psycopg.connect(RECORDS) as records:
        truth = dict(records.execute("SELECT message_id, intent FROM support.truth").fetchall())
    preference = SemanticPlanPreference.VERIFY_ALL if mode == "exact" else SemanticPlanPreference.VECTOR_SHORTLIST
    policy = SemanticPolicy(
        embedder=embedder.info, embedder_dimensions=embedder.dimensions, preference=preference, maximum_candidates=20_000
    )
    semantic = SemanticExtension(SemanticRuntime(LabelVerifier(truth), embedder, verification_batch_size=100), policy=policy)
    catalog = write_catalog(embedder.info.model, embedder.dimensions)
    with yodb.connect(catalog, {"e2e-records": RECORDS, "e2e-vectors": VECTORS}, semantic=semantic) as db:
        serve(db, timeout_seconds=120)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "exact")
