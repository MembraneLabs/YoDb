"""A separate vector store: catalog, resolution, planning, cost-based choice and execution."""

from __future__ import annotations

from dataclasses import replace
import unittest

from yodb.catalog import CatalogValidationError, SourceKind
from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.errors import ErrorCode, QueryError
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine
from yodb.execution.federated import FederatedPlanExecutor
from yodb.planning import (
    CapabilityPlanningAdapter,
    FederatedPhysicalPlanner,
    PlannerPolicy,
    POSTGRES_CAPABILITIES,
    PostgresPlanningAdapter,
    RecordAssembly,
    SourcePlanningRegistry,
    SourceStatistics,
    StatisticsService,
    StepRole,
    transform_plan,
)
from yodb.query import bind_query, parse_query, resolve_query_sources
from yodb.semantic import SemanticExtension, SemanticPlanKind, SemanticPlanPreference, SemanticPolicy, SemanticRuntime

from support.catalogs import StaticRuntime
from support.statistics import MapProvider
from support.tickets import EMBEDDER_INFO, FakeEmbedder, KeywordVerifier, OWNER, prio, q, sem
from support.vector_store import SOURCES, VectorStoreExecutor, store_catalog

ACTIVE = store_catalog()
BODIES = ["the price is high", "love it", "price hike", "app crashed", "price and cancel", "fine", "great price", "no comment"]
RECORDS = {
    "helpdesk": tuple({"id": f"t{i}", "subject": f"S{i}", "body": body, "priority": 5} for i, body in enumerate(BODIES, 1)),
    "directory": ({"id": "t1", "owner": "ann"}, {"id": "t3", "owner": "ann"}, {"id": "t4", "owner": "bob"}, {"id": "t5", "owner": "ann"}),
}
# Tickets that mention price sit near the embedded query (1, 0, 0.5); the others are far away.
VECTORS = {
    f"t{i}": ((1.0, 0.01 * i, 0.5) if "price" in body else (0.0, 1.0, 0.0)) for i, body in enumerate(BODIES, 1)
}
PRICE = {"t1", "t3", "t5", "t7"}


def resolve(raw):
    return resolve_query_sources(bind_query(parse_query(raw), ACTIVE), ACTIVE)


def policy(**kw):
    return SemanticPolicy(embedder=EMBEDDER_INFO, embedder_dimensions=3, **kw)


def planner(*, semantic=None, planner_policy=PlannerPolicy(), statistics=None, adapter=None):
    extension = SemanticExtension(policy=semantic or policy())
    registry = SourcePlanningRegistry([adapter or PostgresPlanningAdapter()])
    return FederatedPhysicalPlanner(registry, policy=planner_policy, statistics=statistics, extensions=(extension,))


def assembly(planned) -> RecordAssembly:
    node = planned.plan
    while not isinstance(node, RecordAssembly):
        node = node.input
    return node


def schedule(planned):
    return [(s.source_name, s.role.value) for s in assembly(planned).schedule]


def verify_node(planned):
    node = planned.plan
    while not hasattr(node, "choice_reasons"):
        node = node.input
    return node


class CatalogTests(unittest.TestCase):
    def test_a_representation_that_maps_only_the_identity_may_hold_the_vectors(self) -> None:
        store = ACTIVE.catalog.sources["vectors"].datasets["ticket"]
        self.assertEqual(set(store.fields), {"id"})
        self.assertIn("body", store.embeddings)

    def test_one_field_cannot_have_its_embeddings_declared_twice(self) -> None:
        twice = SOURCES.replace(
            "          priority: {physical_name: priority}\n",
            "          priority: {physical_name: priority}\n        embeddings:\n          body: {column: e2, model: embed-v1, dimensions: 3}\n",
            1,
        )
        with self.assertRaises(CatalogValidationError) as caught:
            store_catalog(twice)
        self.assertIn("declare them once", str(caught.exception))

    def test_a_store_must_map_the_dataset_identity(self) -> None:
        broken = SOURCES.replace(
            "        identity: [id]\n        fields:\n          id: {physical_name: ticket_id}\n        embeddings:",
            "        identity: [key]\n        fields:\n          key: {physical_name: ticket_id}\n        embeddings:",
        )
        with self.assertRaises(CatalogValidationError):
            store_catalog(broken)


class ResolutionTests(unittest.TestCase):
    def test_the_store_is_bound_by_identity_but_never_read_as_a_contributor(self) -> None:
        resolved = resolve(q({"all": [prio(), sem()]}))
        self.assertEqual([s.source_name for s in resolved.sources], ["helpdesk"])
        (store,) = resolved.extension_sources
        self.assertEqual((store.source_name, store.resource, set(store.embeddings)), ("vectors", "vec.ticket_vectors", {"body"}))
        self.assertEqual(store.logical_id.physical_name, "ticket_id")

    def test_a_query_without_a_semantic_term_does_not_touch_the_store(self) -> None:
        self.assertEqual(resolve(q(prio())).extension_sources, ())


class PlanTests(unittest.TestCase):
    def test_the_shortlist_is_read_from_the_store_before_the_anchor(self) -> None:
        planned = planner().plan(resolve(q(sem(), first=2)))
        self.assertEqual(schedule(planned), [("vectors", "shortlist"), ("helpdesk", "anchor")])
        store_scan = next(c for c in assembly(planned).contributors if c.source.source_name == "vectors")
        self.assertEqual((store_scan.vector_search.shortlist_size, store_scan.limit), (20, 20))
        self.assertEqual([f.field.name for f in store_scan.projection], ["id"])
        self.assertIsNone(assembly(planned).anchor.vector_search)          # the anchor no longer ranks
        self.assertEqual(verify_node(planned).plan, SemanticPlanKind.VECTOR_SHORTLIST)
        self.assertIn("shortlist of 20 from vector store 'vectors'", verify_node(planned).choice_reasons)

    def test_required_contributors_are_read_first_and_the_shortlist_is_ranked_among_their_ids(self) -> None:
        planned = planner().plan(resolve(q({"all": [OWNER, sem()]}, select=("subject", "owner"))))
        self.assertEqual(schedule(planned), [("directory", "required"), ("vectors", "shortlist"), ("helpdesk", "anchor")])

    def test_the_explanation_names_the_store_and_the_vector_search(self) -> None:
        planned = planner().plan(resolve(q(sem())))
        kinds = [(n.kind, n.location) for n in planned.explain.nodes]
        self.assertIn(("remote_scan", "vectors"), kinds)
        self.assertTrue(any("vector_search=cosine" in d for n in planned.explain.nodes for d in n.detail))

    def test_verify_all_never_touches_the_store(self) -> None:
        planned = planner(semantic=policy(preference=SemanticPlanPreference.VERIFY_ALL)).plan(resolve(q(sem())))
        self.assertNotIn("vectors", [n.location for n in planned.explain.nodes])

    def test_the_shortlist_size_is_bounded_by_what_the_anchor_can_be_restricted_by(self) -> None:
        planned = planner(planner_policy=PlannerPolicy(maximum_transfer_keys=15)).plan(resolve(q(sem(), first=20)))
        store_scan = next(c for c in assembly(planned).contributors if c.source.source_name == "vectors")
        self.assertEqual(store_scan.vector_search.shortlist_size, 15)

    def test_without_key_transfer_the_shortlist_could_not_narrow_the_anchor_so_it_is_not_offered(self) -> None:
        planned = planner(planner_policy=PlannerPolicy(maximum_transfer_keys=0)).plan(resolve(q(sem())))
        self.assertNotIn("vectors", [n.location for n in planned.explain.nodes])
        self.assertTrue(any("key transfer is disabled" in r for r in verify_node(planned).choice_reasons))

    def test_an_anchor_that_cannot_be_restricted_by_ids_gets_no_store_shortlist(self) -> None:
        no_lookup = CapabilityPlanningAdapter(replace(POSTGRES_CAPABILITIES, key_lookup=None))
        planned = planner(adapter=no_lookup).plan(resolve(q(sem())))
        self.assertEqual(verify_node(planned).plan, SemanticPlanKind.VERIFY_ALL)
        self.assertTrue(any("cannot be restricted by IDs" in r for r in verify_node(planned).choice_reasons))

    def test_a_demanded_shortlist_that_cannot_run_is_a_planning_error(self) -> None:
        strict = policy(preference=SemanticPlanPreference.VECTOR_SHORTLIST)
        with self.assertRaises(QueryError) as caught:
            planner(semantic=strict, planner_policy=PlannerPolicy(maximum_transfer_keys=0)).plan(resolve(q(sem())))
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_PLAN_UNSUPPORTED)

    def test_a_filter_left_to_yodb_keeps_the_shortlist_from_preceding_it(self) -> None:
        contains = {"field": "subject", "op": "contains", "value": "S"}
        planned = planner().plan(resolve(q({"all": [contains, sem()]})))
        self.assertEqual(verify_node(planned).plan, SemanticPlanKind.VERIFY_ALL)


class CostBasedTests(unittest.TestCase):
    def statistics(self, tickets, vectors, owners=10):
        provider = MapProvider({
            "public.tickets": SourceStatistics(row_count=tickets),
            "vec.ticket_vectors": SourceStatistics(row_count=vectors),
            "public.owners": SourceStatistics(row_count=owners),
        })
        return StatisticsService({SourceKind.POSTGRES: provider})

    def plan(self, raw, statistics):
        return planner(statistics=statistics).plan(resolve(raw))

    def test_a_pool_too_big_to_verify_is_shortlisted_from_the_store(self) -> None:
        planned = self.plan(q(sem(), first=20, constraints={"minimum_quality": 0.3}), self.statistics(5_000, 5_000))
        self.assertEqual(planned.explain.optimizer[0], "strategy=cost_based")
        self.assertEqual(schedule(planned), [("vectors", "shortlist"), ("helpdesk", "anchor")])

    def test_a_small_pool_is_verified_in_full_and_the_store_is_not_read(self) -> None:
        planned = self.plan(q(sem(), first=20), self.statistics(100, 100))
        self.assertEqual(schedule(planned) if isinstance(planned.plan, RecordAssembly) else [], [])
        self.assertEqual(verify_node(planned).plan, SemanticPlanKind.VERIFY_ALL)
        self.assertNotIn("vectors", [n.location for n in planned.explain.nodes])

    def test_a_missing_statistic_for_the_store_keeps_the_fixed_rules_and_says_so(self) -> None:
        provider = MapProvider({"public.tickets": SourceStatistics(row_count=5_000)})
        planned = self.plan(q(sem()), StatisticsService({SourceKind.POSTGRES: provider}))
        self.assertEqual(planned.explain.optimizer[0], "strategy=rules")
        self.assertIn("vectors", planned.explain.optimizer[1])
        self.assertEqual(schedule(planned), [("vectors", "shortlist"), ("helpdesk", "anchor")])    # the rule plan still shortlists


class ExecutionTests(unittest.TestCase):
    def engine(self, *, semantic=None, planner_policy=PlannerPolicy(), verifier=None):
        self.executor = VectorStoreExecutor(RECORDS, VECTORS)
        self.verifier = verifier or KeywordVerifier()
        runtime = SemanticRuntime(self.verifier, FakeEmbedder(), verification_batch_size=3)
        extension = SemanticExtension(runtime, policy=semantic or policy())
        planner_ = FederatedPhysicalPlanner(
            SourcePlanningRegistry([PostgresPlanningAdapter()]),
            policy=planner_policy,
            extensions=(extension,),
        )
        return QueryExecutionEngine(
            StaticRuntime(ACTIVE),
            QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([self.executor]),
            planner=planner_,
            extensions=(extension,),
        )

    def test_the_store_ranks_ids_and_the_anchor_is_read_only_for_them(self) -> None:
        result = self.engine().execute(q(sem("mentions price"), first=10))
        self.assertEqual({row["id"] for row in result.rows}, PRICE)
        store_sql, = self.executor.sql_for("vectors")
        self.assertIn("<=>", store_sql)
        self.assertIn('"ticket_id" AS "id"', store_sql)
        (anchor_sql,) = self.executor.sql_for("helpdesk")
        self.assertIn('"id" IN (', anchor_sql)                              # restricted by the shortlist
        report = result.reports["semantic"]
        self.assertEqual((report.stats.plan, report.stats.embedding_model_calls), (SemanticPlanKind.VECTOR_SHORTLIST, 1))

    def test_a_small_shortlist_verifies_only_that_many_records(self) -> None:
        engine = self.engine(semantic=policy(minimum_shortlist=2, shortlist_oversample=1))
        result = engine.execute(q(sem("mentions price"), first=2))
        self.assertEqual(result.reports["semantic"].stats.verified, 2)
        self.assertEqual(len(result.rows), 2)
        self.assertTrue({row["id"] for row in result.rows} <= PRICE)
        self.assertEqual(len(self.executor.ids_read("helpdesk")[0]), 2)     # two IDs, nothing else read

    def test_a_required_contributor_narrows_the_ids_the_store_ranks(self) -> None:
        result = self.engine().execute(q({"all": [OWNER, sem("mentions price")]}, select=("subject", "owner")))
        self.assertEqual({row["id"] for row in result.rows}, {"t1", "t3", "t5"})     # ann's tickets that mention price
        self.assertEqual([q_.source_name for q_ in self.executor.queries], ["directory", "vectors", "helpdesk"])
        # ranked among exactly the IDs the contributor returned (this fake ignores the pushed owner
        # filter, so that is all four owners; the coordinator filter still drops bob's ticket)
        self.assertEqual(set(self.executor.ids_read("vectors")[0]), {"t1", "t3", "t4", "t5"})

    def test_ids_too_many_for_the_store_still_give_correct_results(self) -> None:
        # three IDs cannot restrict the store (limit 2): it ranks among all its vectors, then the rest intersects
        engine = self.engine(planner_policy=PlannerPolicy(maximum_transfer_keys=2))
        result = engine.execute(q({"all": [OWNER, sem("mentions price")]}, select=("subject", "owner")))
        self.assertTrue({row["id"] for row in result.rows} <= {"t1", "t3", "t5"})
        self.assertEqual(self.executor.queries[1].source_name, "vectors")
        self.assertNotIn("IN (", self.executor.queries[1].sql)                     # read unrestricted

    def test_only_shortlisted_records_survive_even_if_the_anchor_is_read_without_the_restriction(self) -> None:
        verifier = KeywordVerifier()
        extension = SemanticExtension(
            SemanticRuntime(verifier, FakeEmbedder(), verification_batch_size=3),
            policy=policy(minimum_shortlist=2, shortlist_oversample=1),
        )
        planned = planner(semantic=policy(minimum_shortlist=2, shortlist_oversample=1)).plan(resolve(q(sem("mentions price"), first=2)))

        def unrestricted_anchor(node):
            if isinstance(node, RecordAssembly):
                steps = tuple(replace(s, restrict=False) if s.role is StepRole.ANCHOR else s for s in node.schedule)
                return replace(node, schedule=steps)
            return node

        self.executor = VectorStoreExecutor(RECORDS, VECTORS)
        executor = FederatedPlanExecutor(
            QueryCompilerRegistry([PostgresQueryCompiler()]), QueryExecutionAdapterRegistry([self.executor]), extensions=(extension,)
        )
        rows = executor.execute(transform_plan(planned.plan, unrestricted_anchor))
        self.assertEqual(len(self.executor.ids_read("helpdesk")[0]), 0)             # the anchor really was read in full
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(len(call) for call in verifier.calls), 2)             # only the shortlisted records were judged
        self.assertTrue({row["id"] for row in rows} <= PRICE)

    def test_a_shortlist_that_finds_nothing_ends_the_query_without_reading_the_anchor(self) -> None:
        engine = self.engine()
        self.executor.vectors = {}
        self.assertEqual(engine.execute(q(sem("mentions price"))).rows, ())
        self.assertEqual(self.executor.sql_for("helpdesk"), [])

    def test_the_embedding_model_must_match_the_stores_declared_space(self) -> None:
        other = SemanticPolicy(embedder=replace(EMBEDDER_INFO, model="other-model"), embedder_dimensions=3)
        planned = planner(semantic=other).plan(resolve(q(sem())))
        self.assertEqual(verify_node(planned).plan, SemanticPlanKind.VERIFY_ALL)
        self.assertTrue(any("does not match the stored embedding" in r for r in verify_node(planned).choice_reasons))


if __name__ == "__main__":
    unittest.main()
