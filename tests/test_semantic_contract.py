"""The SemanticFilter contract: syntax, binding rules, catalog metadata, providers."""

from __future__ import annotations

import unittest
from datetime import datetime, UTC
from pathlib import Path
from tempfile import TemporaryDirectory

from yodb.catalog import CatalogValidationError, load_catalog, VectorMetric
from yodb.errors import ErrorCode, QueryError
from yodb.inspection import SourceInspection, SourceValidationReport
from yodb.query import (
    bind_query,
    BoundAllExpression,
    BoundSemanticPredicate,
    DatasetReference,
    parse_query,
    QueryRequest,
    QueryValidationPolicy,
    resolve_query_sources,
    SemanticPredicate,
)
from yodb.query.resolution import FieldUse
from yodb.runtime import CatalogEvaluation, SourceRuntimeState, SourceRuntimeStatus
from yodb.semantic import (
    batches,
    EmbeddingRequest,
    EmbeddingResult,
    passes_quality,
    ProviderInfo,
    SemanticExecutionStats,
    SemanticPlanKind,
    VerificationCandidate,
    VerificationRequest,
    VerificationResult,
    VerificationUsage,
    VerificationVerdict,
)

from support.tickets import semantic_planner


_NOW = datetime(2026, 10, 3, tzinfo=UTC)

DATASETS = """\
api_version: yodb/v0.1
catalog: {name: support, version: 1}
datasets:
  ticket:
    description: A support ticket.
    fields:
      id:        {type: id,     description: Identity.}
      subject:   {type: string, description: Subject line.}
      body:      {type: text,   description: Ticket text., semantic_eligible: true}
      note:      {type: text,   description: Private note., semantic_eligible: true, visibility: internal}
      priority:  {type: int,    description: Priority., semantic_eligible: true}
"""
SOURCES = """\
api_version: yodb/v0.1
sources:
  helpdesk:
    kind: postgres
    connection_ref: helpdesk
    read_only: true
    datasets:
      ticket:
        resource: public.tickets
        identity: [id]
        fields:
          id: {physical_name: id}
          subject: {physical_name: subject}
          body: {physical_name: body}
          note: {physical_name: note}
          priority: {physical_name: priority}
        embeddings:
          body: {column: body_embedding, model: embed-v1, dimensions: 3, metric: cosine, version: "2"}
resolution:
  ticket:
    identity_source: helpdesk
    field_sources: {id: helpdesk, subject: helpdesk, body: helpdesk, note: helpdesk, priority: helpdesk}
"""
RELATIONS = """\
api_version: yodb/v0.1
relationships:
  ticket_reference:
    from: ticket
    to: ticket
    description: Placeholder relationship required by the loader.
    cardinality: one_to_one
    direction: uni
    implementations:
      - from: {source: helpdesk, field: id}
        to: {source: helpdesk, field: id}
"""


def _write(directory: str, sources: str = SOURCES) -> Path:
    root = Path(directory)
    (root / "datasets.yaml").write_text(DATASETS, encoding="utf-8")
    (root / "sources.yaml").write_text(sources, encoding="utf-8")
    (root / "relations.yaml").write_text(RELATIONS, encoding="utf-8")
    return root


def _active() -> CatalogEvaluation:
    with TemporaryDirectory() as directory:
        catalog = load_catalog(_write(directory))
    return CatalogEvaluation(
        catalog=catalog,
        evaluated_at=_NOW,
        sources={
            name: SourceRuntimeState(
                source_name=name,
                status=SourceRuntimeStatus.VALID,
                inspection=SourceInspection(source_name=name, source_kind=source.kind, inspected_at=_NOW),
                validation=SourceValidationReport(source_name=name, inspected_at=_NOW),
            )
            for name, source in catalog.sources.items()
        },
    )


def sem(field="body", proposition="The customer is considering cancelling over price"):
    return {"semantic": {"field": field, "proposition": proposition}}


def query(where=None, **extra):
    raw = {"from": {"dataset": "ticket"}, "select": ["subject"], **extra}
    if where is not None:
        raw["where"] = where
    return raw


class SemanticContractBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.active = _active()

    def bind(self, raw, **kwargs):
        return bind_query(parse_query(raw), self.active, **kwargs)

    def assertCode(self, code, call, *args, **kwargs):
        with self.assertRaises(QueryError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class ParsingTests(SemanticContractBase):
    def test_semantic_leaf_parses_and_binds_with_a_trimmed_proposition(self) -> None:
        bound = self.bind(query(sem(proposition="  price is the reason  ")))
        self.assertIsInstance(bound.where, BoundSemanticPredicate)
        self.assertEqual((bound.where.field.name, bound.where.proposition), ("body", "price is the reason"))

    def test_malformed_semantic_terms_are_rejected(self) -> None:
        for where in (
            {"semantic": {"field": "body"}},                                        # no proposition
            {"semantic": {"field": "body", "proposition": "x", "k": 5}},            # unknown key
            {"semantic": {"field": "body", "proposition": 5}},                      # not a string
            {"semantic": "price"},                                                  # not an object
            {"semantic": {"field": "body", "proposition": "x"}, "all": []},         # mixed keys
        ):
            with self.subTest(where=where):
                with self.assertRaises(QueryError):
                    parse_query(query(where))

    def test_top_level_semantic_key_points_to_where(self) -> None:
        error = self.assertCode(ErrorCode.QUERY_FEATURE_NOT_SUPPORTED, parse_query, {**query(), "semantic": {}})
        self.assertIn("where", error.detail.message)


class BindingRuleTests(SemanticContractBase):
    def test_field_must_be_public_text_and_semantic_eligible(self) -> None:
        self.assertCode(ErrorCode.QUERY_OPERATOR_NOT_SUPPORTED, self.bind, query(sem("subject")))   # not eligible
        self.assertCode(ErrorCode.QUERY_OPERATOR_NOT_SUPPORTED, self.bind, query(sem("priority")))  # eligible but int
        self.assertCode(ErrorCode.FIELD_NOT_ACCESSIBLE, self.bind, query(sem("note")))              # internal
        self.assertCode(ErrorCode.FIELD_NOT_FOUND, self.bind, query(sem("nope")))

    def test_only_conjunctive_positions_are_allowed(self) -> None:
        eq = {"field": "subject", "op": "eq", "value": "x"}
        self.bind(query(sem()))
        self.bind(query({"all": [eq, sem()]}))
        bound = self.bind(query({"all": [eq, {"all": [sem(), eq]}]}))      # nested all stays conjunctive
        self.assertIsInstance(bound.where, BoundAllExpression)
        for where in (
            {"any": [eq, sem()]},
            {"not": sem()},
            {"all": [eq, {"any": [eq, sem()]}]},
            {"all": [eq, {"not": sem()}]},
        ):
            with self.subTest(where=where):
                self.assertCode(ErrorCode.QUERY_EXPRESSION_INVALID, self.bind, query(where))

    def test_semantic_condition_count_is_bounded_by_policy(self) -> None:
        two = {"all": [sem(), sem(proposition="mentions a refund")]}
        self.assertCode(ErrorCode.QUERY_LIMIT_INVALID, self.bind, query(two))
        self.bind(query(two), policy=QueryValidationPolicy(limits={"semantic": 2}))
        self.assertCode(ErrorCode.QUERY_LIMIT_INVALID, self.bind, query(sem()), policy=QueryValidationPolicy(limits={"semantic": 0}))

    def test_proposition_must_be_non_blank_and_bounded(self) -> None:
        self.assertCode(ErrorCode.QUERY_SHAPE_INVALID, self.bind, query(sem(proposition="   ")))  # parser
        built = QueryRequest(  # a request built in code bypasses the parser; the binder still refuses
            root=DatasetReference("ticket", "ticket"), select=("subject",), where=SemanticPredicate("body", "   ")
        )
        self.assertCode(ErrorCode.QUERY_VALUE_TYPE_INVALID, bind_query, built, self.active)
        self.assertCode(ErrorCode.QUERY_LIMIT_INVALID, self.bind, query(sem(proposition="x" * 1_001)))
        self.bind(query(sem(proposition="x" * 1_000)))

    def test_minimum_quality_requires_a_semantic_condition(self) -> None:
        self.assertCode(ErrorCode.QUERY_LIMIT_INVALID, self.bind, query(constraints={"minimum_quality": 0.8}))
        self.assertCode(ErrorCode.QUERY_LIMIT_INVALID, self.bind, query(sem(), constraints={"minimum_quality": 1.5}))
        self.assertEqual(self.bind(query(sem(), constraints={"minimum_quality": 0.8})).constraints.minimum_quality, 0.8)

    def test_fingerprint_tracks_the_proposition_and_ignores_whitespace_and_order(self) -> None:
        eq = {"field": "subject", "op": "eq", "value": "x"}
        base = self.bind(query(sem())).query_fingerprint
        self.assertNotEqual(base, self.bind(query(sem(proposition="something else"))).query_fingerprint)
        self.assertNotEqual(base, self.bind(query()).query_fingerprint)
        self.assertEqual(base, self.bind(query(sem(proposition="  The customer is considering cancelling over price "))).query_fingerprint)
        self.assertEqual(
            self.bind(query({"all": [eq, sem()]})).query_fingerprint,
            self.bind(query({"all": [sem(), eq]})).query_fingerprint,
        )


class DownstreamTests(SemanticContractBase):
    def test_resolution_marks_the_semantic_field_for_its_owning_source(self) -> None:
        resolved = resolve_query_sources(self.bind(query(sem())), self.active)
        (source,) = resolved.sources
        uses = {field.field.name: field.uses for field in source.fields}
        self.assertIn(FieldUse.EXTENSION, uses["body"])
        self.assertNotIn(FieldUse.FILTER, uses["body"])

    def test_planner_turns_the_semantic_term_into_a_verification_step(self) -> None:
        resolved = resolve_query_sources(self.bind(query(sem())), self.active)
        planner = semantic_planner()
        kinds = [node.kind for node in planner.plan(resolved).explain.nodes]
        self.assertEqual(kinds, ["remote_scan", "semantic_verify", "coordinator_sort_page", "result_project"])


class CatalogEmbeddingTests(unittest.TestCase):
    def load(self, sources: str):
        with TemporaryDirectory() as directory:
            return load_catalog(_write(directory, sources))

    def test_embedding_binding_loads_with_its_space_description(self) -> None:
        binding = self.load(SOURCES).sources["helpdesk"].datasets["ticket"].embeddings["body"]
        self.assertEqual(
            (binding.column, binding.model, binding.dimensions, binding.metric, binding.version),
            ("body_embedding", "embed-v1", 3, VectorMetric.COSINE, "2"),
        )

    def test_invalid_embedding_bindings_are_rejected(self) -> None:
        good = "body: {column: body_embedding, model: embed-v1, dimensions: 3, metric: cosine, version: \"2\"}"
        for replacement in (
            "subject: {column: c, model: m, dimensions: 3}",              # not semantic_eligible
            "priority: {column: c, model: m, dimensions: 3}",             # eligible but not text
            "ghost: {column: c, model: m, dimensions: 3}",                # unknown field
            "body: {column: c, model: m, dimensions: 0}",                 # non-positive dimensions
            "body: {column: c, model: m, dimensions: 3, metric: hamming}",  # unknown metric
            "body: {column: '  ', model: m, dimensions: 3}",              # blank column
        ):
            with self.subTest(replacement=replacement), self.assertRaises(CatalogValidationError):
                self.load(SOURCES.replace(good, replacement))

    def test_embeddings_are_optional(self) -> None:
        sources = SOURCES.split("        embeddings:")[0] + "resolution:" + SOURCES.split("resolution:")[1]
        self.assertEqual(self.load(sources).sources["helpdesk"].datasets["ticket"].embeddings, {})


class ProviderContractTests(unittest.TestCase):
    INFO = ProviderInfo("acme", "judge-1", "2026-10")

    def test_provider_info_and_requests_reject_blank_or_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            ProviderInfo("acme", " ", "1")
        with self.assertRaises(ValueError):
            EmbeddingRequest(())
        with self.assertRaises(ValueError):
            VerificationRequest("p", ())
        with self.assertRaises(ValueError):
            VerificationRequest("  ", (VerificationCandidate("a", "t"),))
        with self.assertRaises(ValueError):
            VerificationRequest("p", (VerificationCandidate("a", "t"), VerificationCandidate("a", "u")))

    def test_embedding_results_must_match_declared_dimensions_and_be_finite(self) -> None:
        EmbeddingResult(((0.1, 0.2), (0.3, 0.4)), self.INFO, 2)
        for vectors in (((0.1,),), ((float("nan"), 0.0),)):
            with self.assertRaises(ValueError):
                EmbeddingResult(vectors, self.INFO, 2)

    def test_a_verifier_must_return_exactly_one_verdict_per_candidate(self) -> None:
        request = VerificationRequest("p", (VerificationCandidate("a", "t"), VerificationCandidate("b", "u")))
        usage = VerificationUsage(model_calls=1)
        ok = VerificationResult((VerificationVerdict("a", True), VerificationVerdict("b", False)), usage, self.INFO)
        ok.require_complete_for(request)
        for verdicts in (
            (VerificationVerdict("a", True),),                                           # skipped b
            (VerificationVerdict("a", True), VerificationVerdict("a", True)),            # duplicated
            (VerificationVerdict("a", True), VerificationVerdict("z", True)),            # invented
        ):
            with self.subTest(verdicts=verdicts), self.assertRaises(ValueError):
                VerificationResult(verdicts, usage, self.INFO).require_complete_for(request)

    def test_confidence_and_usage_are_validated_and_usage_adds(self) -> None:
        with self.assertRaises(ValueError):
            VerificationVerdict("a", True, 1.2)
        with self.assertRaises(ValueError):
            VerificationUsage(cost=-1)
        total = VerificationUsage(1, 10, 2, 0.5, 4.0) + VerificationUsage(2, 5, 1, 0.25, 6.0)
        self.assertEqual(total, VerificationUsage(3, 15, 3, 0.75, 10.0))

    def test_quality_rule_only_ever_shrinks_the_result(self) -> None:
        yes = VerificationVerdict("a", True, 0.7)
        self.assertTrue(passes_quality(yes, None))
        self.assertTrue(passes_quality(yes, 0.7))
        self.assertFalse(passes_quality(yes, 0.71))
        self.assertTrue(passes_quality(VerificationVerdict("a", True), 0.99))     # no confidence -> 1.0
        self.assertFalse(passes_quality(VerificationVerdict("a", False, 1.0), None))  # a confident "no" is still no
        self.assertFalse(passes_quality(VerificationVerdict("a", False), 0.1))

    def test_stats_invariants_and_batching(self) -> None:
        usage = VerificationUsage(model_calls=2)
        SemanticExecutionStats(SemanticPlanKind.VERIFY_ALL, 10, None, 10, 4, usage)
        SemanticExecutionStats(SemanticPlanKind.VECTOR_SHORTLIST, 1000, 50, 50, 4, usage, embedding_model_calls=1)
        for args in (
            (SemanticPlanKind.VERIFY_ALL, 10, 5, 10, 4, usage),               # A has no shortlist
            (SemanticPlanKind.VECTOR_SHORTLIST, 10, None, 10, 4, usage),      # B must report one
            (SemanticPlanKind.VERIFY_ALL, 10, None, 11, 4, usage),            # verified > considered
            (SemanticPlanKind.VERIFY_ALL, 10, None, 10, 11, usage),           # qualified > verified
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                SemanticExecutionStats(*args)
        items = tuple(VerificationCandidate(i, "t") for i in range(5))
        self.assertEqual([len(b) for b in batches(items, 2)], [2, 2, 1])
        with self.assertRaises(ValueError):
            batches(items, 0)


if __name__ == "__main__":
    unittest.main()
