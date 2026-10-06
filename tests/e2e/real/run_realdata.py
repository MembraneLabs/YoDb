"""Semantic filter on real data with the vectors in a separate database.

Records (13,083 BANKING77 support messages + a synthetic owner/priority) live on one
Postgres server; their embeddings (a real open model) live on another.  Queries ask
natural-language propositions; the judge is the dataset's own intent label (a perfect
verifier), so what is measured is retrieval: does the shortlist from the vector
store find the true matches, and how much verification does it save?

    PYTHONPATH=src .venv/bin/python tests/e2e/real/run_realdata.py [--model NAME] [-v] [--report FILE]

Set up the two servers and load them first (see tests/e2e/README.md).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile
import time

import psycopg

HERE = Path(__file__).parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

from providers import PROPOSITIONS, FastEmbedder, LabelVerifier   # noqa: E402
import run_e2e as base                                             # noqa: E402
from yodb.catalog import SourceKind                                # noqa: E402
from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry   # noqa: E402
from yodb.connections import MappingPostgresConnectionResolver, PostgresConnectionAdapter, PostgresConnectionSettings   # noqa: E402
from yodb.errors import YoDbError                                  # noqa: E402
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine   # noqa: E402
from yodb.inspection import InspectionAdapterBinding, PostgresCatalogValidator, PostgresSourceInspector, SourceInspectionRegistry   # noqa: E402
from yodb.planning import (                                        # noqa: E402
    FederatedPhysicalPlanner,
    ObservationStore,
    PlannerPolicy,
    PostgresPlanningAdapter,
    PostgresStatisticsProvider,
    SourcePlanningRegistry,
    StatisticsService,
)
from yodb.runtime import InMemoryCatalogRuntime                    # noqa: E402
from yodb.semantic import (   # noqa: E402
    PowerLawRecall,
    SemanticCosts,
    SemanticExtension,
    SemanticPlanPreference,
    SemanticPolicy,
    SemanticRuntime,
)

RECORDS = "host=localhost port=55432 dbname=records user=yodb_ro password=yodb_ro"
VECTORS = "host=localhost port=55433 dbname=vectors user=yodb_ro password=yodb_ro"
SERVER = {"inbox": "records:55432", "triage": "records:55432", "vectors": "vectors:55433"}

# filter flavours: (YoDb filter or None, oracle SQL over m = messages, a = assignments)
FLAVORS = {
    "none": (None, "TRUE"),
    "split=test": ({"field": "split", "op": "eq", "value": "test"}, "m.split = 'test'"),
    "words>=20": ({"field": "word_count", "op": "gte", "value": 20}, "m.word_count >= 20"),
    "owner=ann": ({"field": "owner", "op": "eq", "value": "ann"}, "a.owner = 'ann'"),
    "owner=ann & words>=12": (
        {"all": [{"field": "owner", "op": "eq", "value": "ann"}, {"field": "word_count", "op": "gte", "value": 12}]},
        "a.owner = 'ann' AND m.word_count >= 12"),
    "priority>=4": ({"field": "priority", "op": "gte", "value": 4}, "a.priority >= 4"),
}
ENGINE_ORDER = ("A_exact", "A_cap1000", "B_rules", "B_keys5k", "B_wide", "B_stats", "B_fit")
SHORTLIST_ENGINES = ("B_rules", "B_keys5k", "B_wide", "B_stats", "B_fit")
FROM = "support.messages m LEFT JOIN triage.assignments a USING (message_id) JOIN support.truth t USING (message_id)"


@dataclass
class Run:
    engine: str
    ok: bool
    refused: str = ""
    rows: int = 0
    exact_page: bool = False
    in_truth: int = 0
    plan: str = ""
    considered: int = 0
    shortlisted: int | None = None
    verified: int = 0
    cost: float = 0.0
    ms: float = 0.0
    k: int | None = None
    reads: dict = None
    notes: tuple = ()


def write_catalog(model: str, dims: int) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="yodb-real-"))
    for name in ("datasets", "sources", "relations"):
        text = (HERE / "catalog" / f"{name}.yaml").read_text().replace("@MODEL@", model).replace("@DIMS@", str(dims))
        (directory / f"{name}.yaml").write_text(text)
    return directory


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    model = argv[argv.index("--model") + 1] if "--model" in argv else "minishlab/potion-base-8M"
    report_path = argv[argv.index("--report") + 1] if "--report" in argv else None
    out_lines: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out_lines.append(line)

    embedder = FastEmbedder(model)
    oracle = psycopg.connect(RECORDS)
    with oracle.cursor() as cursor:
        cursor.execute("SELECT message_id, intent FROM support.truth")
        truth = dict(cursor.fetchall())
    verifier = LabelVerifier(truth)

    resolver = MappingPostgresConnectionResolver({
        "e2e-records": PostgresConnectionSettings(conninfo=RECORDS),
        "e2e-vectors": PostgresConnectionSettings(conninfo=VECTORS),
    })
    connections = PostgresConnectionAdapter(resolver, max_size=4, acquire_timeout_seconds=10)
    inspector = PostgresSourceInspector(connections)
    registry = SourceInspectionRegistry([InspectionAdapterBinding(
        source_kind=inspector.source_kind, inspector=inspector, validator=PostgresCatalogValidator())])
    runtime = InMemoryCatalogRuntime(write_catalog(model, embedder.dimensions), registry)
    refresh = runtime.refresh()
    say(f"model: {model} ({embedder.dimensions} dims)   catalog: {refresh.status.value}")
    if runtime.active is None:
        for name, state in (refresh.candidate.sources if refresh.candidate else {}).items():
            for finding in (state.validation.findings if state.validation else ()):
                say(f"   [{finding.severity.value}] {name}: {finding.message}")
        return 2
    executor = base.RecordingExecutor(connections)
    compilers = QueryCompilerRegistry([PostgresQueryCompiler()])
    executors = QueryExecutionAdapterRegistry([executor])
    statistics = StatisticsService({SourceKind.POSTGRES: PostgresStatisticsProvider(connections)}, observations=ObservationStore())
    semantic_runtime = SemanticRuntime(verifier, embedder, verification_batch_size=100)

    def engine(*, stats=False, planner_policy=PlannerPolicy(), costs=None, **policy):
        extension = SemanticExtension(semantic_runtime, costs=costs, policy=SemanticPolicy(
            embedder=embedder.info, embedder_dimensions=embedder.dimensions, **policy))
        planner = FederatedPhysicalPlanner(
            SourcePlanningRegistry([PostgresPlanningAdapter()]), policy=planner_policy,
            statistics=statistics if stats else None, extensions=(extension,))
        return QueryExecutionEngine(runtime, compilers, executors, planner=planner,
                                    statistics=statistics if stats else None, extensions=(extension,))

    wide = PlannerPolicy(maximum_transfer_keys=5_000)
    engines = {
        # verify every candidate; the reference answer (row guard raised so 13,083 rows may be read)
        "A_exact": engine(preference=SemanticPlanPreference.VERIFY_ALL, maximum_candidates=20_000,
                          planner_policy=PlannerPolicy(maximum_rows_per_source=50_000)),
        "A_cap1000": engine(preference=SemanticPlanPreference.VERIFY_ALL),            # the default limits
        "B_rules": engine(),                                                          # default limits, fixed rules
        "B_keys5k": engine(planner_policy=wide),                                      # may send 5,000 IDs to a source
        "B_wide": engine(planner_policy=wide, maximum_candidates=5_000, shortlist_oversample=50),
        "B_stats": engine(stats=True, planner_policy=wide),                           # cost-based, real pg_stats
        # cost-based with a recall curve fitted to this model on this data (see the curve in the report)
        "B_fit": engine(stats=True, planner_policy=wide, maximum_candidates=5_000,
                        costs=SemanticCosts(recall_model=PowerLawRecall(0.15))),
    }
    say("engines: " + "; ".join(engines))

    results: list[tuple[str, str, int, dict, list[Run]]] = []
    for proposition, intents in PROPOSITIONS.items():
        for flavor, (where, flavor_sql) in FLAVORS.items():
            with oracle.cursor() as cursor:
                cursor.execute(f"SELECT count(*) FROM {FROM} WHERE {flavor_sql}")
                candidates = cursor.fetchone()[0]
                cursor.execute(f"SELECT array_agg(message_id ORDER BY message_id) FROM {FROM} WHERE ({flavor_sql}) AND t.intent = ANY(%s)", (list(intents),))
                matches = cursor.fetchone()[0] or []
            for first in (20, 500):
                expected = matches[:first]
                terms = ([where] if where else []) + [{"semantic": {"field": "body", "proposition": proposition}}]
                query = {"from": {"dataset": "message"}, "select": ["body"], "page": {"first": first},
                         "where": terms[0] if len(terms) == 1 else {"all": terms}}
                runs = []
                for label, eng in engines.items():
                    executor.log.clear()
                    verifier.records_judged = 0
                    run = Run(label, False, reads={})
                    started = time.perf_counter()
                    try:
                        result = eng.execute(query, timeout_seconds=60)
                        stats = result.reports["semantic"].stats
                        got = [row["id"] for row in result.rows]
                        run.ok = True
                        run.rows = len(got)
                        run.exact_page = got == expected
                        run.in_truth = sum(1 for g in got if g in set(matches))
                        run.plan, run.considered, run.shortlisted = stats.plan.value, stats.candidates_considered, stats.shortlisted
                        run.verified, run.cost = stats.verified, stats.usage.cost
                    except YoDbError as error:
                        run.refused = error.code.value
                    run.ms = (time.perf_counter() - started) * 1000
                    try:
                        explanation = eng.explain(query)
                        run.notes = (*explanation.optimizer, *(d for node in explanation.nodes for d in node.detail if d.startswith(("schedule", "note:"))))
                    except YoDbError:
                        pass
                    for entry in executor.log:
                        run.reads[entry["source"]] = run.reads.get(entry["source"], 0) + entry["rows"]
                        if entry["source"] == "vectors":
                            run.k = entry["rows"]
                    runs.append(run)
                results.append((proposition, flavor, first, {"candidates": candidates, "matches": len(matches)}, runs))
                if verbose:
                    say(f"\n{proposition[:48]:<48} {flavor:<22} first={first:<4} candidates={candidates} true matches={len(matches)}")
                    for r in runs:
                        say(f"   {r.engine:<10} " + (f"REFUSED {r.refused}" if not r.ok else
                            f"plan={r.plan:<17} rows={r.rows:<4} exact_page={r.exact_page!s:<5} verified={r.verified:<5} shortlisted={r.shortlisted} reads={r.reads} {r.ms:.0f} ms")
                            + (f"\n              notes: {' | '.join(r.notes)}" if r.engine == "B_stats" else ""))

    summarize(say, results, verifier, embedder)
    recall_curve(say, embedder, truth)
    if report_path:
        Path(report_path).write_text("\n".join(out_lines) + "\n")
    oracle.close()
    connections.close()
    return 0


def summarize(say, results, verifier, embedder) -> None:
    say("\n" + "=" * 118)
    say(f"RESULTS: {len(results)} queries (8 propositions x 6 filters x 2 page sizes) x {len(ENGINE_ORDER)} engines")
    say("=" * 118)

    def run_of(runs, label):
        return next(r for r in runs if r.engine == label)

    answered = [r for _, _, _, _, runs in results for r in runs if r.ok]
    wrong = [r for r in answered if r.in_truth != r.rows]
    say(f"\nprecision: every returned row is a true match in {len(answered) - len(wrong)} of {len(answered)} answered runs (violations: {len(wrong)})")
    same = sum(1 for _, _, _, _, runs in results if run_of(runs, "A_exact").exact_page)
    say(f"A_exact (verify everything, no cap) returns exactly the SQL oracle's page in {same} of {len(results)} queries")

    say("\nBY ENGINE")
    say(f"  {'engine':<10}{'answered':>9}{'refused':>9}{'page=oracle':>13}{'page filled':>13}{'avg verified':>14}{'avg shortlist':>15}{'avg ms':>9}")
    for label in ENGINE_ORDER:
        runs = [run_of(rs, label) for _, _, _, _, rs in results]
        ok = [r for r in runs if r.ok]
        filled = sum(1 for (p, f, n, info, rs) in results if run_of(rs, label).ok and run_of(rs, label).rows >= min(n, info["matches"]))
        short = [r.shortlisted for r in ok if r.shortlisted is not None]
        say(f"  {label:<10}{len(ok):>9}{len(runs) - len(ok):>9}{sum(r.exact_page for r in ok):>13}{filled:>13}"
            f"{(sum(r.verified for r in ok) / max(1, len(ok))):>14.0f}{(sum(short) / len(short) if short else 0):>15.0f}{(sum(r.ms for r in ok) / max(1, len(ok))):>9.0f}")

    say("\nHOW MUCH VERIFICATION DOES THE SHORTLIST SAVE, AND WHAT DOES IT FIND? (first=500; recall = found / min(500, true matches))")
    say(f"  {'proposition':<40}{'filter':<22}{'cand':>6}{'true':>5} |{'A verified':>11} |" + "".join(f"{label:>22}" for label in SHORTLIST_ENGINES[:3]))
    say(f"  {'':<40}{'':<22}{'':>6}{'':>5} |{'':>11} |" + "".join(f"{'verified  found  recall':>22}" for _ in SHORTLIST_ENGINES[:3]))
    for p, f, n, info, runs in results:
        if n != 500 or f not in ("none", "owner=ann", "words>=20"):
            continue
        cells = ""
        for label in SHORTLIST_ENGINES[:3]:
            b = run_of(runs, label)
            recall = b.rows / min(500, info["matches"]) if b.ok and info["matches"] else float("nan")
            cells += f"{b.verified:>9}{b.rows if b.ok else 0:>7}{recall:>7.2f}"
        say(f"  {p[:38]:<40}{f:<22}{info['candidates']:>6}{info['matches']:>5} |{run_of(runs, 'A_exact').verified:>11} |{cells}")

    say("\nRECALL BY FILTER (first=500; averaged over the 8 propositions)")
    say(f"  {'filter':<24}{'candidates':>11}" + "".join(f"{label:>11}" for label in SHORTLIST_ENGINES))
    for flavor in FLAVORS:
        values = {label: [] for label in SHORTLIST_ENGINES}
        cand = []
        for p, f, n, info, runs in results:
            if f == flavor and n == 500 and info["matches"]:
                cand.append(info["candidates"])
                for label in SHORTLIST_ENGINES:
                    b = run_of(runs, label)
                    values[label].append(b.rows / min(500, info["matches"]) if b.ok else 0.0)
        say(f"  {flavor:<24}{(cand[0] if cand else 0):>11}" + "".join(f"{sum(v) / len(v):>11.2f}" for v in values.values()))

    say("\nPAGE OF 20 (first=20): same page as exact verification / page filled with true matches")
    for label in SHORTLIST_ENGINES:
        total = same = filled = 0
        for p, f, n, info, runs in results:
            if n == 20:
                r = run_of(runs, label)
                total += r.ok
                same += r.ok and r.exact_page
                filled += r.ok and r.rows >= min(20, info["matches"])
        say(f"  {label:<10} identical page {same}/{total}   page filled {filled}/{total}")

    say("\nWHERE THE WORK HAPPENED (average rows read per server per query, B_keys5k)")
    reads = defaultdict(list)
    for _, _, _, _, runs in results:
        r = run_of(runs, "B_keys5k")
        if r.ok:
            for source, rows in r.reads.items():
                reads[source].append(rows)
    for source, values in reads.items():
        say(f"  {source:<8} on {SERVER[source]:<16} avg rows read {sum(values) / len(values):8.0f}  in {len(values)} of {len(results)} queries")
    a_reads = [sum(run_of(runs, 'A_exact').reads.values()) for _, _, _, _, runs in results if run_of(runs, 'A_exact').ok]
    b_reads = [sum(run_of(runs, 'B_keys5k').reads.values()) for _, _, _, _, runs in results if run_of(runs, 'B_keys5k').ok]
    say(f"  total rows read per query: A_exact {sum(a_reads) / len(a_reads):.0f}   B_keys5k {sum(b_reads) / len(b_reads):.0f}")

    say("\nOPTIMIZER (B_stats)")
    plans = defaultdict(int)
    for _, _, _, _, runs in results:
        r = run_of(runs, "B_stats")
        plans[r.plan if r.ok else "refused:" + r.refused] += 1
    say("  plans used: " + ", ".join(f"{k}={v}" for k, v in plans.items()))
    refused = defaultdict(int)
    for _, _, _, _, runs in results:
        for r in runs:
            if not r.ok:
                refused[(r.engine, r.refused)] += 1
    for (engine, code), count in sorted(refused.items()):
        say(f"  refused: {engine:<10} {code:<34} {count}")
    say(f"\nreal embedding model: {embedder.calls} query embeddings in {embedder.seconds:.1f}s total")


def recall_curve(say, embedder, truth) -> None:
    """How well the real model ranks true matches first, against the planner's assumed curve."""

    import math

    ks = (50, 100, 200, 500, 1000, 2000, 5000)
    total = len(truth)
    say("\nRECALL OF THE REAL EMBEDDING MODEL: fraction of all true matches inside the nearest K of the whole corpus")
    say(f"  {'proposition':<46}{'true':>5}" + "".join(f"{'@' + str(k):>7}" for k in ks))
    average = {k: 0.0 for k in ks}
    exponents = []
    with psycopg.connect(VECTORS) as vectors:
        for proposition, intents in PROPOSITIONS.items():
            vector = embedder.embed_many([proposition])[0]
            ranked = [r[0] for r in vectors.execute(
                "SELECT message_id FROM vec.message_vectors ORDER BY embedding <=> %s::vector, message_id LIMIT 5000",
                ("[" + ",".join(repr(x) for x in vector) + "]",)).fetchall()]
            matches = sum(1 for v in truth.values() if v in intents)
            row = [sum(1 for m in ranked[:k] if truth[m] in intents) / matches for k in ks]
            for k, value in zip(ks, row):
                average[k] += value / len(PROPOSITIONS)
            exponents.append(math.log(max(1e-6, row[ks.index(1000)])) / math.log(1000 / total))
            say(f"  {proposition[:44]:<46}{matches:>5}" + "".join(f"{x:>7.2f}" for x in row))
    say(f"  {'average':<51}" + "".join(f"{average[k]:>7.2f}" for k in ks))
    say(f"  {'planner default (K/N)^0.5':<51}" + "".join(f"{(k / total) ** 0.5:>7.2f}" for k in ks))
    fitted = sum(exponents) / len(exponents)
    say(f"  {'fitted (K/N)^' + format(fitted, '.2f'):<51}" + "".join(f"{(k / total) ** fitted:>7.2f}" for k in ks))
    say(f"  the planner assumes exponent 0.50; this model on this data fits {fitted:.2f} (smaller means a better ranking)")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
