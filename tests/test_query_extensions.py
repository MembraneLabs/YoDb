"""A new filter term is added through the term registry alone; no query-layer code changes."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.errors import ErrorCode, QueryError
from yodb.execution import QueryExecutionAdapterRegistry
from yodb.execution.federated import FederatedPlanExecutor
from yodb.operators import OperatorKind
from yodb.planning import (
    Claim,
    ExtensionPlan,
    FederatedPhysicalPlanner,
    PostgresPlanningAdapter,
    SourcePlanningRegistry,
    Strategy,
    Variant,
)
from yodb.planning.contracts import properties_from
from yodb.planning.optimizer import Estimate
from yodb.query import (
    bind_query,
    BoundExtensionTerm,
    DEFAULT_TERMS,
    extension_terms,
    ExtensionTerm,
    FieldUse,
    parse_query,
    QueryValidationPolicy,
    resolve_query_sources,
    TermExtension,
    TermRegistry,
)
from yodb.query.extensions import strip_terms
from yodb.query.fields import bind_public_field
from yodb.query.shape import mapping, non_empty_string, reject_unknown

from support.catalogs import SourceRowsExecutor
from support.tickets import ACTIVE, DIRECTORY, HELPDESK, prio, q
from support.toys import KeepEqual, Plugin


@dataclass(frozen=True)
class Exactly(ExtensionTerm):
    field: str
    value: str


@dataclass(frozen=True)
class BoundExactly(BoundExtensionTerm):
    field: object
    value: str


def _parse(body, location):
    body = mapping(body, location)
    reject_unknown(body, frozenset({"field", "value"}), location)
    return Exactly(non_empty_string(body.get("field"), f"{location}.field"), non_empty_string(body.get("value"), f"{location}.value"))


def _bind(term, root, location, policy):
    return BoundExactly(bind_public_field(root, term.field, f"{location}.exactly.field"), term.value)


def make_extension(**changes):
    base = dict(
        key="exactly",
        parsed_type=Exactly,
        bound_type=BoundExactly,
        parse=_parse,
        bind=_bind,
        describe=lambda t: {"exactly": {"field": t.field.name, "value": t.value}},
        uses=lambda t: ((t.field, FieldUse.FILTER),),
        maximum=1,
        noun="exact match",
    )
    return TermExtension(**{**base, **changes})


EXACTLY = make_extension()
TERMS = TermRegistry([*DEFAULT_TERMS, EXACTLY])


def exactly(value="S2"):
    return {"exactly": {"field": "subject", "value": value}}


def bind(raw, terms=TERMS):
    return bind_query(parse_query(raw, terms), ACTIVE, terms=terms)


class RegistryTests(unittest.TestCase):
    def test_keys_are_unique_and_lookups_work(self) -> None:
        self.assertEqual(TERMS.keys(), ("semantic", "exactly"))
        self.assertIs(TERMS.by_key("exactly"), EXACTLY)
        self.assertIsNone(TERMS.by_key("nope"))
        with self.assertRaises(ValueError):
            TermRegistry([EXACTLY, make_extension()])

    def test_an_unregistered_term_type_is_a_programming_error_not_a_silent_pass(self) -> None:
        with self.assertRaises(AssertionError):
            DEFAULT_TERMS.for_bound(BoundExactly(object(), "x"))

    def test_the_default_registry_does_not_know_the_new_term(self) -> None:
        with self.assertRaises(QueryError):
            parse_query(q(exactly()), DEFAULT_TERMS)   # 'exactly' is an unknown predicate key there


class ParseBindResolveTests(unittest.TestCase):
    def test_the_term_parses_binds_and_is_found_in_the_bound_filter(self) -> None:
        bound = bind(q({"all": [prio(), exactly()]}))
        (term,) = extension_terms(bound.where)
        self.assertIsInstance(term, BoundExactly)
        self.assertEqual((term.field.name, term.value), ("subject", "S2"))

    def test_malformed_bodies_are_rejected_with_the_term_s_own_location(self) -> None:
        for body in ({"field": "subject"}, {"field": "subject", "value": "x", "extra": 1}, "S2", {"field": "", "value": "x"}):
            with self.subTest(body=body), self.assertRaises(QueryError):
                parse_query(q({"exactly": body}), TERMS)

    def test_a_term_must_be_alone_in_its_object(self) -> None:
        with self.assertRaises(QueryError) as caught:
            parse_query(q({"exactly": {"field": "subject", "value": "x"}, "all": []}), TERMS)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_EXPRESSION_INVALID)
        self.assertIn("exact match", caught.exception.detail.message)

    def test_a_root_level_term_key_points_to_where(self) -> None:
        with self.assertRaises(QueryError) as caught:
            parse_query({**q(prio()), "exactly": {}}, TERMS)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_FEATURE_NOT_SUPPORTED)
        self.assertIn("where", caught.exception.detail.message)

    def test_the_term_s_own_binding_rules_apply(self) -> None:
        with self.assertRaises(QueryError) as caught:
            bind(q({"exactly": {"field": "ghost", "value": "x"}}))
        self.assertEqual(caught.exception.code, ErrorCode.FIELD_NOT_FOUND)

    def test_the_fingerprint_comes_from_the_term_s_description(self) -> None:
        self.assertNotEqual(bind(q(exactly("S1"))).query_fingerprint, bind(q(exactly("S2"))).query_fingerprint)
        self.assertEqual(bind(q(exactly("S1"))).query_fingerprint, bind(q(exactly("S1"))).query_fingerprint)

    def test_the_fields_a_term_reads_join_source_resolution(self) -> None:
        resolved = resolve_query_sources(bind(q(exactly(), select=("owner",))), ACTIVE, TERMS)
        names = {f.field.name for source in resolved.sources for f in source.fields}
        self.assertIn("subject", names)            # read only by the term
        uses = {f.field.name: f.uses for source in resolved.sources for f in source.fields}
        self.assertIn(FieldUse.FILTER, uses["subject"])


class PlacementTests(unittest.TestCase):
    def test_conjunctive_only_terms_are_refused_under_any_or_not(self) -> None:
        for where in ({"any": [prio(), exactly()]}, {"not": exactly()}):
            with self.subTest(where=list(where)), self.assertRaises(QueryError) as caught:
                bind(q(where))
            self.assertEqual(caught.exception.code, ErrorCode.QUERY_EXPRESSION_INVALID)
            self.assertIn("exact match", caught.exception.detail.message)

    def test_a_term_that_allows_any_position_is_accepted_there(self) -> None:
        terms = TermRegistry([make_extension(conjunctive_only=False)])
        bound = bind_query(parse_query(q({"any": [prio(), exactly()]}), terms), ACTIVE, terms=terms)
        self.assertEqual(len(extension_terms(bound.where)), 1)

    def test_the_per_query_maximum_comes_from_the_term(self) -> None:
        with self.assertRaises(QueryError) as caught:
            bind(q({"all": [exactly("a"), exactly("b")]}))
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_LIMIT_INVALID)
        self.assertIn("at most 1 exact match(s)", caught.exception.detail.message)

    def test_the_deployment_can_raise_or_lower_a_terms_limit_by_name(self) -> None:
        two = q({"all": [exactly("a"), exactly("b")]})
        policy = QueryValidationPolicy(limits={"exactly": 2})
        bind_query(parse_query(two, TERMS), ACTIVE, policy=policy, terms=TERMS)
        with self.assertRaises(QueryError):
            bind_query(parse_query(q(exactly()), TERMS), ACTIVE, policy=QueryValidationPolicy(limits={"exactly": 0}), terms=TERMS)
        with self.assertRaises(ValueError):
            QueryValidationPolicy(limits={"exactly": -1})

    def test_minimum_quality_needs_a_term_that_declares_it(self) -> None:
        with self.assertRaises(QueryError):
            bind(q(exactly(), constraints={"minimum_quality": 0.5}))
        terms = TermRegistry([make_extension(uses_minimum_quality=True)])
        bind_query(parse_query(q(exactly(), constraints={"minimum_quality": 0.5}), terms), ACTIVE, terms=terms)

    def test_strip_terms_removes_only_the_requested_type(self) -> None:
        bound = bind(q({"all": [prio(), exactly(), {"semantic": {"field": "body", "proposition": "x"}}]}))
        stripped = strip_terms(bound.where, BoundExactly)
        kinds = {type(t).__name__ for t in extension_terms(stripped)}
        self.assertEqual(kinds, {"BoundSemanticPredicate"})


class ExactlyOperator:
    """A planning operator for the new term: claims it, builds the toy node."""

    kind = OperatorKind.DISTINCT

    def claim(self, where):
        found = [t for t in extension_terms(where) if isinstance(t, BoundExactly)]
        return Claim(tuple(found), strip_terms(where, BoundExactly)) if found else None

    def plan(self, claim, core, scans, query):
        (term,) = claim.terms

        def build(node, notes):
            return KeepEqual(node, term.field.name, term.value, "exactly", properties_from(node.properties))

        strategy = Strategy(Variant("exactly", cost=lambda c: Estimate(1.0, 0.0)), build)
        return ExtensionPlan(self.kind, (strategy,), strategy)


class EndToEndTests(unittest.TestCase):
    def test_a_new_term_runs_end_to_end_with_no_change_to_parser_validator_resolver_planner_or_executor(self) -> None:
        raw = q({"all": [prio(), exactly("S2")]}, select=("subject",))
        resolved = resolve_query_sources(bind(raw), ACTIVE, TERMS)
        planner = FederatedPhysicalPlanner(SourcePlanningRegistry([PostgresPlanningAdapter()]), extensions=(Plugin(ExactlyOperator()),))
        planned = planner.plan(resolved)
        self.assertIn("keep_equal", [entry.kind for entry in planned.explain.nodes])
        rows = SourceRowsExecutor({"helpdesk": HELPDESK, "directory": DIRECTORY})
        executor = FederatedPlanExecutor(
            QueryCompilerRegistry([PostgresQueryCompiler()]), QueryExecutionAdapterRegistry([rows]), extensions=(Plugin(ExactlyOperator()),)
        )
        self.assertEqual([r["subject"] for r in executor.execute(planned.plan)], ["S2"])

    def test_a_term_nobody_claims_is_refused_at_planning(self) -> None:
        resolved = resolve_query_sources(bind(q(exactly())), ACTIVE, TERMS)
        planner = FederatedPhysicalPlanner(SourcePlanningRegistry([PostgresPlanningAdapter()]), extensions=())
        with self.assertRaises(QueryError) as caught:
            planner.plan(resolved)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_FEATURE_NOT_SUPPORTED)
        self.assertIn("BoundExactly", caught.exception.detail.message)


if __name__ == "__main__":
    unittest.main()
