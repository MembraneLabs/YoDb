"""Plan A (verify all) and Plan B (vector shortlist): planning, SQL, execution."""

from __future__ import annotations

import unittest
from dataclasses import replace

from yodb.catalog import VectorMetric
from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.errors import ErrorCode, QueryError, QueryExecutionError
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine
from yodb.planning import CoordinatorFilter, RecordAssembly, RemoteScan
from yodb.query import bind_query, parse_query, resolve_query_sources
from yodb.semantic import (
    ProviderInfo,
    SemanticExtension,
    SemanticPlanKind,
    SemanticPlanPreference,
    SemanticPolicy,
    SemanticRuntime,
    SemanticVerify,
)

from support.catalogs import SourceRowsExecutor, StaticRuntime
from support.tickets import (
    EMBEDDER_INFO,
    FakeEmbedder,
    JUDGE_INFO,
    KeywordVerifier,
    prio,
    q,
    sem,
    semantic_planner,
    ticket_catalog,
    TICKETS,
)


PRICE_IDS = ["t1", "t3", "t5"]


def planner_for(policy=SemanticPolicy()):
    return semantic_planner(policy)


def shortlist_policy(**kwargs):
    return SemanticPolicy(embedder=EMBEDDER_INFO, embedder_dimensions=3, **kwargs)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.active = ticket_catalog()

    def plan(self, raw, policy=SemanticPolicy()):
        bound = bind_query(parse_query(raw), self.active)
        return planner_for(policy).plan(resolve_query_sources(bound, self.active))

    @staticmethod
    def verify_node(planned) -> SemanticVerify:
        node = planned.plan
        while not isinstance(node, SemanticVerify):
            node = node.input
        return node

    @staticmethod
    def scans(planned):
        node = planned.plan
        while not isinstance(node, (RemoteScan, RecordAssembly)):
            node = node.input
        return (node,) if isinstance(node, RemoteScan) else (node.anchor, *node.contributors)


class PlanChoiceTests(Base):
    def notes(self, planned):
        return [d for n in planned.explain.nodes for d in n.detail if d.startswith("note:")]

    def test_without_an_embedder_the_plan_verifies_everything_and_says_why(self) -> None:
        planned = self.plan(q(AND(prio(), sem())))
        node = self.verify_node(planned)
        (scan,) = self.scans(planned)
        self.assertEqual(node.plan, SemanticPlanKind.VERIFY_ALL)
        self.assertIn("note: no embedding provider is configured", self.notes(planned))
        self.assertEqual((scan.limit, scan.order_by, scan.maximum_rows, scan.vector_search), (None, (), 10_000, None))
        self.assertEqual(scan.pushed_filter.field.name, "priority")  # the rest of the filter still pushes down

    def test_a_matching_embedder_selects_the_shortlist_with_a_bounded_size(self) -> None:
        for first, size in ((5, 50), (1, 20), (100, 1_000)):
            with self.subTest(first=first):
                planned = self.plan(q(sem(), first=first), shortlist_policy())
                (scan,) = self.scans(planned)
                self.assertEqual(self.verify_node(planned).plan, SemanticPlanKind.VECTOR_SHORTLIST)
                self.assertEqual((scan.vector_search.shortlist_size, scan.limit), (size, size))
                self.assertEqual((scan.vector_search.metric, scan.vector_search.column), (VectorMetric.COSINE, "body_embedding"))
                self.assertIsNone(scan.maximum_rows)
                self.assertNotIn(CoordinatorFilter, [type(n) for n in self.chain(planned)])

    @staticmethod
    def chain(planned):
        node = planned.plan
        while True:
            yield node
            if not hasattr(node, "input"):
                return
            node = node.input

    def test_ineligible_shortlists_fall_back_to_verify_all_with_a_reason(self) -> None:
        cases = {
            "embedding model differs": (q(sem()), SemanticPolicy(embedder=ProviderInfo("f", "other", "1"), embedder_dimensions=3)),
            "dimensions differ": (q(sem()), SemanticPolicy(embedder=EMBEDDER_INFO, embedder_dimensions=8)),
            "field owned by a contributor": (q(sem(field="memo")), shortlist_policy()),
            "a filter term is not enforced by its source": (
                q(AND(sem(), {"field": "subject", "op": "contains", "value": "x"})),
                shortlist_policy(),
            ),
            "preference is verify_all": (q(sem()), shortlist_policy(preference=SemanticPlanPreference.VERIFY_ALL)),
        }
        for fragment, (raw, policy) in cases.items():
            with self.subTest(fragment):
                planned = self.plan(raw, policy)
                self.assertEqual(self.verify_node(planned).plan, SemanticPlanKind.VERIFY_ALL)
                self.assertTrue(any(fragment.split(" ")[0] in note for note in self.notes(planned)), self.notes(planned))

    def test_a_required_shortlist_is_an_error_when_unavailable(self) -> None:
        policy = SemanticPolicy(preference=SemanticPlanPreference.VECTOR_SHORTLIST)
        with self.assertRaises(QueryError) as caught:
            self.plan(q(sem()), policy)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_PLAN_UNSUPPORTED)
        self.assertEqual(self.verify_node(self.plan(q(sem()), shortlist_policy(preference=SemanticPlanPreference.VECTOR_SHORTLIST))).plan, SemanticPlanKind.VECTOR_SHORTLIST)

    def test_residual_filters_stay_and_fingerprints_distinguish_plans_not_propositions(self) -> None:
        contains = {"field": "subject", "op": "contains", "value": "x"}
        with_residual = self.plan(q(AND(sem(), contains)))
        self.assertIn(CoordinatorFilter, [type(n) for n in self.chain(with_residual)])
        a = self.plan(q(sem("mentions price")))
        a2 = self.plan(q(sem("something else entirely")))
        b = self.plan(q(sem("mentions price")), shortlist_policy())
        self.assertEqual(a.plan_fingerprint, a2.plan_fingerprint)
        self.assertNotEqual(a.plan_fingerprint, b.plan_fingerprint)

    def test_explain_names_the_plan_and_the_vector_search(self) -> None:
        planned = self.plan(q(sem()), shortlist_policy())
        details = [d for n in planned.explain.nodes for d in n.detail]
        self.assertIn("plan=vector_shortlist", details)
        self.assertTrue(any(d.startswith("vector_search=cosine top ") for d in details))


class CompileTests(Base):
    def compiled(self, metric=VectorMetric.COSINE, **scan_changes):
        planned = self.plan(q(AND(prio(), sem()), first=1), shortlist_policy())
        (scan,) = self.scans(planned)
        scan = replace(scan, vector_search=replace(scan.vector_search, metric=metric, query_vector=(1.0, 0.0, 0.5)), **scan_changes)
        return PostgresQueryCompiler().compile_scan(scan)

    def test_vector_scan_orders_by_distance_under_the_other_filters(self) -> None:
        sql = self.compiled().sql
        self.assertIn('WHERE ("priority" >= %s) AND "body_embedding" IS NOT NULL', sql)
        self.assertIn('ORDER BY "body_embedding" <=> %s::vector, "id" ASC', sql)
        self.assertTrue(sql.endswith("LIMIT %s"))
        self.assertEqual(self.compiled().parameters, (3, "[1.0,0.0,0.5]", 20))

    def test_each_metric_uses_its_pgvector_operator(self) -> None:
        for metric, operator in ((VectorMetric.COSINE, "<=>"), (VectorMetric.L2, "<->"), (VectorMetric.INNER_PRODUCT, "<#>")):
            self.assertIn(f"{operator} %s::vector", self.compiled(metric).sql)

    def test_key_restriction_comes_before_the_ranking_and_parameters_follow_sql_order(self) -> None:
        compiled = self.compiled(key_filter=("a", "b"))
        self.assertIn('AND "id" IN (%s, %s)', compiled.sql)
        self.assertEqual(compiled.parameters, (3, "a", "b", "[1.0,0.0,0.5]", 20))

    def test_an_unembedded_vector_scan_cannot_compile(self) -> None:
        planned = self.plan(q(sem()), shortlist_policy())
        (scan,) = self.scans(planned)
        with self.assertRaises(QueryError) as caught:
            PostgresQueryCompiler().compile_scan(scan)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_COMPILATION_UNSUPPORTED)


class EngineBase(Base):
    def engine(self, rows, *, verifier=None, embedder=None, runtime=True, batch=None, policy=None):
        self.executor = SourceRowsExecutor(rows)
        self.verifier = verifier or KeywordVerifier()
        self.embedder = embedder
        semantic = SemanticRuntime(self.verifier, embedder, batch) if runtime else None
        return QueryExecutionEngine(
            StaticRuntime(self.active),
            QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([self.executor]),
            extensions=(SemanticExtension(semantic, policy=policy),),
        )

    def run_query(self, raw, rows=None, **kwargs):
        engine = self.engine({"helpdesk": TICKETS if rows is None else rows, "directory": ()}, **kwargs)
        return engine.execute(raw)


class PlanAExecutionTests(EngineBase):
    def test_only_records_the_proposition_holds_for_are_returned_with_provenance(self) -> None:
        result = self.run_query(q(sem()))
        self.assertEqual([r["id"] for r in result.rows], PRICE_IDS)
        report = result.reports.get("semantic")
        self.assertEqual(report.stats.plan, SemanticPlanKind.VERIFY_ALL)
        self.assertEqual((report.stats.candidates_considered, report.stats.verified, report.stats.qualified), (8, 6, 3))
        self.assertIsNone(report.stats.shortlisted)
        self.assertEqual(sorted(report.records), PRICE_IDS)
        record = report.records["t1"]
        self.assertEqual((record.holds, record.confidence, record.info), (True, 0.9, JUDGE_INFO))
        self.assertEqual(report.stats.usage.model_calls, 2)
        self.assertAlmostEqual(report.stats.usage.cost, 0.06)

    def test_records_without_text_are_never_verified_or_returned(self) -> None:
        self.run_query(q(sem()))
        sent = {i for call in self.verifier.calls for i in call}
        self.assertEqual(sent, {"t1", "t2", "t3", "t4", "t5", "t8"})  # t6 None, t7 blank

    def test_the_rest_of_the_filter_is_pushed_to_sql_and_no_vector_is_used(self) -> None:
        self.run_query(q(AND(prio(), sem())))
        (query,) = self.executor.queries
        self.assertIn('WHERE "priority" >= %s', query.sql)
        self.assertNotIn("::vector", query.sql)

    def test_verification_follows_the_callers_order_and_stops_when_the_page_is_full(self) -> None:
        result = self.run_query(q(sem(), first=2, order_by=[{"field": "subject", "direction": "desc"}]), batch=2)
        # candidates by subject desc: t8 t5 t4 t3 t2 t1; matches are t5 and t3
        self.assertEqual([r["id"] for r in result.rows], ["t5", "t3"])
        self.assertEqual(self.verifier.calls, [["t8", "t5"], ["t4", "t3"]])  # t2, t1 never sent
        self.assertEqual(result.reports.get("semantic").stats.verified, 4)

    def test_early_stop_returns_the_same_page_as_verifying_everything(self) -> None:
        for first in (1, 2, 3, 5):
            with self.subTest(first=first):
                small = self.run_query(q(sem(), first=first), batch=1)
                full = self.run_query(q(sem(), first=10), batch=10)
                self.assertEqual([r["id"] for r in small.rows], [r["id"] for r in full.rows][:first])

    def test_minimum_quality_drops_low_confidence_positives(self) -> None:
        verifier = KeywordVerifier(confidence={"t1": 0.95, "t3": 0.5, "t5": 0.8})
        result = self.run_query(q(sem(), constraints={"minimum_quality": 0.8}), verifier=verifier)
        self.assertEqual([r["id"] for r in result.rows], ["t1", "t5"])
        self.assertEqual(sorted(result.reports.get("semantic").records), ["t1", "t5"])

    def test_the_candidate_cap_stops_the_query_before_any_model_call(self) -> None:
        engine = self.engine({"helpdesk": TICKETS, "directory": ()}, policy=SemanticPolicy(maximum_candidates=5))
        with self.assertRaises(QueryExecutionError) as caught:
            engine.execute(q(sem()))
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_SEMANTIC_BUDGET_EXCEEDED)
        self.assertEqual(self.verifier.calls, [])

    def test_the_cost_budget_is_enforced_between_batches(self) -> None:
        with self.assertRaises(QueryExecutionError) as caught:
            self.run_query(q(sem(), constraints={"maximum_cost": 0.04}), batch=2)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_SEMANTIC_BUDGET_EXCEEDED)

    def test_provider_failures_are_structured_errors(self) -> None:
        for verifier in (KeywordVerifier(fail=True), KeywordVerifier(drop=True)):
            with self.subTest(fail=verifier.fail), self.assertRaises(QueryExecutionError) as caught:
                self.run_query(q(sem()), verifier=verifier)
            self.assertEqual(caught.exception.code, ErrorCode.SEMANTIC_PROVIDER_FAILED)

    def test_a_semantic_query_without_a_configured_verifier_fails_clearly(self) -> None:
        with self.assertRaises(QueryExecutionError) as caught:
            self.run_query(q(sem()), runtime=False)
        self.assertEqual(caught.exception.code, ErrorCode.SEMANTIC_PROVIDER_UNAVAILABLE)

    def test_queries_without_a_semantic_term_carry_no_report(self) -> None:
        self.assertIsNone(self.run_query({"from": {"dataset": "ticket"}, "select": ["subject"]}).reports.get("semantic"))

    def test_multi_source_records_are_enriched_and_filtered_before_verification(self) -> None:
        rows = {
            "helpdesk": TICKETS,
            "directory": ({"id": "t1", "owner": "ann"}, {"id": "t2", "owner": "ann"}),  # what owner='ann' selects
        }
        engine = self.engine(rows)
        owner = {"field": "owner", "op": "eq", "value": "ann"}
        result = engine.execute(q(AND(owner, sem()), select=("subject", "owner")))
        self.assertEqual([(r["id"], r["owner"]) for r in result.rows], [("t1", "ann")])
        sent = {i for call in self.verifier.calls for i in call}
        self.assertEqual(sent, {"t1", "t2"})  # owner filter narrowed candidates first


class PlanBExecutionTests(EngineBase):
    def run_b(self, raw, rows=None, **kwargs):
        embedder = kwargs.pop("embedder", FakeEmbedder())
        return self.run_query(raw, rows=rows, embedder=embedder, **kwargs)

    def test_the_proposition_is_embedded_once_and_the_source_returns_a_ranked_shortlist(self) -> None:
        shortlist = tuple(r for r in TICKETS if r["id"] in {"t1", "t3", "t5", "t2"})
        result = self.run_b(q(AND(prio(), sem()), first=2), rows=shortlist)
        self.assertEqual(self.embedder.requests, [("mentions price",)])
        (query,) = self.executor.queries
        self.assertIn('ORDER BY "body_embedding" <=> %s::vector, "id" ASC', query.sql)
        self.assertEqual(query.parameters, (3, "[1.0,0.0,0.5]", 20))
        self.assertEqual([r["id"] for r in result.rows], ["t1", "t3"])
        stats = result.reports.get("semantic").stats
        self.assertEqual(stats.plan, SemanticPlanKind.VECTOR_SHORTLIST)
        self.assertEqual((stats.candidates_considered, stats.shortlisted, stats.embedding_model_calls), (4, 4, 1))

    def test_the_shortlist_verifies_far_fewer_candidates_than_verify_all(self) -> None:
        shortlist = tuple(r for r in TICKETS if r["id"] in {"t1", "t3", "t5"})
        b = self.run_b(q(sem()), rows=shortlist)
        a = self.run_query(q(sem()))
        self.assertEqual([r["id"] for r in a.rows], [r["id"] for r in b.rows])
        self.assertLess(b.reports["semantic"].stats.verified, a.reports["semantic"].stats.verified)
        self.assertLess(b.reports["semantic"].stats.usage.cost, a.reports["semantic"].stats.usage.cost)

    def test_a_required_contributor_runs_first_and_its_ids_restrict_the_ranked_scan(self) -> None:
        rows = {"helpdesk": TICKETS, "directory": ({"id": "t1", "owner": "ann"}, {"id": "t3", "owner": "ann"})}
        engine = self.engine(rows, embedder=FakeEmbedder())
        owner = {"field": "owner", "op": "eq", "value": "ann"}
        engine.execute(q(AND(owner, sem()), select=("subject", "owner")))
        self.assertEqual([x.source_name for x in self.executor.queries], ["directory", "helpdesk"])
        anchor = self.executor.queries[1]
        self.assertIn('"id" IN (%s, %s)', anchor.sql)
        self.assertIn("<=>", anchor.sql)
        self.assertEqual(anchor.parameters[:2], ("t1", "t3"))

    def test_an_embedder_that_does_not_match_the_stored_space_is_refused_at_run_time(self) -> None:
        planner_policy = shortlist_policy()  # planner believes the space matches
        engine = self.engine(
            {"helpdesk": TICKETS, "directory": ()},
            embedder=FakeEmbedder(info=ProviderInfo("fake", "embed-v2", "1")),
            policy=planner_policy,
        )
        with self.assertRaises(QueryExecutionError) as caught:
            engine.execute(q(sem()))
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_PLAN_INVARIANT_VIOLATION)
        self.assertEqual(self.verifier.calls, [])

    def test_embedding_failures_are_structured_errors(self) -> None:
        with self.assertRaises(QueryExecutionError) as caught:
            self.run_b(q(sem()), embedder=FakeEmbedder(fail=True))
        self.assertEqual(caught.exception.code, ErrorCode.SEMANTIC_PROVIDER_FAILED)


def AND(*items):
    return {"all": list(items)}


if __name__ == "__main__":
    unittest.main()
