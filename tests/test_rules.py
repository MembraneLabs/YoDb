"""Rules: a named filter used as ``{"rule": name}``, expanded into ordinary filters before planning."""

from __future__ import annotations

import time
import unittest

from yodb.catalog import RuleSpec
from yodb.errors import ErrorCode, QueryError
from yodb.query import bind_query, parse_query

from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine

from support.catalogs import SourceRowsExecutor, StaticRuntime, multi_source_catalog


def rule(name, condition, *, visible=True, definition=None):
    return RuleSpec(name=name, definition=definition or f"The {name} customers.", condition=condition, visible=visible)


def eq(field, value):
    return {"field": field, "op": "eq", "value": value}


RULES = {
    "customer": (
        rule("paying", {"field": "plan", "op": "in", "value": ["pro", "team"]}, definition="On a paid plan."),
        rule("active", eq("status", "active")),
        rule("active_paying", {"all": [{"rule": "active"}, {"rule": "paying"}]}),
        rule("enterprise_tier", eq("plan", "enterprise"), visible=False, definition="The top tier."),
        rule("vip", {"any": [{"rule": "enterprise_tier"}, eq("name", "Acme")]}),
    )
}


def bind(where, rules=RULES):
    active = multi_source_catalog(rules)
    return bind_query(parse_query({"from": {"dataset": "customer"}, "select": ["name"], "where": where}), active)


def error_of(where, rules=RULES) -> QueryError:
    with unittest.TestCase().assertRaises(QueryError) as caught:
        bind(where, rules)
    return caught.exception


class ExpansionTests(unittest.TestCase):
    def assertSameAsTyped(self, with_rule, typed):
        by_rule, by_hand = bind(with_rule), bind(typed)
        self.assertEqual(by_rule.where, by_hand.where)
        self.assertEqual(by_rule.query_fingerprint, by_hand.query_fingerprint)

    def test_a_rule_is_the_filter_it_stands_for(self) -> None:
        self.assertSameAsTyped({"rule": "paying"}, {"field": "plan", "op": "in", "value": ["pro", "team"]})

    def test_a_rule_sits_beside_other_terms_under_all_any_and_not(self) -> None:
        paying = {"field": "plan", "op": "in", "value": ["pro", "team"]}
        self.assertSameAsTyped({"all": [{"rule": "paying"}, eq("name", "x")]}, {"all": [paying, eq("name", "x")]})
        self.assertSameAsTyped({"any": [{"rule": "paying"}, eq("name", "x")]}, {"any": [paying, eq("name", "x")]})
        self.assertSameAsTyped({"not": {"rule": "paying"}}, {"not": paying})

    def test_a_rule_can_be_built_from_other_rules(self) -> None:
        self.assertSameAsTyped(
            {"rule": "active_paying"},
            {"all": [eq("status", "active"), {"field": "plan", "op": "in", "value": ["pro", "team"]}]},
        )

    def test_a_hidden_rule_works_inside_a_visible_one(self) -> None:
        self.assertSameAsTyped({"rule": "vip"}, {"any": [eq("plan", "enterprise"), eq("name", "Acme")]})

    def test_the_query_records_which_rules_it_used_and_which_it_named(self) -> None:
        uses = {use.name: use for use in bind({"rule": "vip"}).rules}

        self.assertEqual(set(uses), {"vip", "enterprise_tier"})
        self.assertTrue(uses["vip"].named_in_query)
        self.assertFalse(uses["enterprise_tier"].named_in_query)
        self.assertFalse(uses["enterprise_tier"].visible)
        self.assertEqual(uses["enterprise_tier"].definition, "The top tier.")
        self.assertIn('"plan"', uses["enterprise_tier"].condition)

    def test_a_query_without_rules_uses_none(self) -> None:
        self.assertEqual(bind(eq("name", "x")).rules, ())
        self.assertEqual(bind(eq("name", "x"), rules=None).rules, ())

    def test_a_rule_named_twice_is_recorded_once(self) -> None:
        self.assertEqual([u.name for u in bind({"all": [{"rule": "paying"}, {"rule": "paying"}]}).rules], ["paying"])


class RefusalTests(unittest.TestCase):
    def test_an_unknown_rule_is_refused_with_the_near_matches(self) -> None:
        detail = error_of({"rule": "payng"}).detail

        self.assertEqual(detail.code, ErrorCode.RULE_NOT_FOUND)
        self.assertIn("'paying'", detail.message)
        self.assertEqual(detail.location, "where")

    def test_a_hidden_rule_cannot_be_named_and_is_refused_like_an_unknown_one(self) -> None:
        hidden, unknown = error_of({"rule": "enterprise_tier"}).detail, error_of({"rule": "no_such_rule"}).detail

        self.assertEqual(hidden.code, unknown.code)
        self.assertEqual(hidden.message.replace("enterprise_tier", "X"), unknown.message.replace("no_such_rule", "X"))

    def test_a_hidden_rule_is_not_offered_as_a_near_match(self) -> None:
        self.assertNotIn("enterprise_tier", error_of({"rule": "enterprise_tie"}).detail.message)

    def test_a_catalog_with_no_rules_refuses_every_rule(self) -> None:
        self.assertEqual(error_of({"rule": "paying"}, rules=None).detail.code, ErrorCode.RULE_NOT_FOUND)

    def test_the_location_points_at_the_rule_term(self) -> None:
        self.assertEqual(error_of({"all": [eq("name", "x"), {"rule": "nope"}]}).detail.location, "where.all[1]")


class ParsingTests(unittest.TestCase):
    def parse(self, where):
        return parse_query({"from": {"dataset": "customer"}, "where": where})

    def test_a_rule_term_holds_only_its_name(self) -> None:
        with self.assertRaises(QueryError) as caught:
            self.parse({"rule": "paying", "field": "plan"})
        self.assertEqual(caught.exception.detail.code, ErrorCode.QUERY_EXPRESSION_INVALID)

    def test_a_rule_name_is_a_non_empty_string(self) -> None:
        for name in ("", 3, None, ["a"]):
            with self.subTest(name=name), self.assertRaises(QueryError):
                self.parse({"rule": name})


class LimitTests(unittest.TestCase):
    def chain(self, depth, fan=2):
        """r0 = all[r1, r1], r1 = all[r2, r2], ...: a small catalog that expands without end."""

        rules = [rule(f"r{i}", {"all": [{"rule": f"r{i + 1}"}] * fan}) for i in range(depth)]
        rules.append(rule(f"r{depth}", eq("name", "x")))
        return {"customer": tuple(rules)}

    def test_rules_that_expand_to_too_much_are_refused_before_they_are_built(self) -> None:
        started = time.perf_counter()
        detail = error_of({"rule": "r0"}, rules=self.chain(7, fan=4)).detail

        self.assertEqual(detail.code, ErrorCode.QUERY_LIMIT_INVALID)
        self.assertLess(time.perf_counter() - started, 2)

    def test_rules_nested_too_deeply_are_refused(self) -> None:
        self.assertEqual(error_of({"rule": "r0"}, rules=self.chain(12, fan=1)).detail.code, ErrorCode.QUERY_LIMIT_INVALID)

    def test_a_loop_that_got_past_the_catalog_is_refused_not_followed(self) -> None:
        rules = {"customer": (rule("a", {"rule": "b"}), rule("b", {"rule": "a"}))}

        self.assertEqual(error_of({"rule": "a"}, rules=rules).detail.code, ErrorCode.QUERY_EXPRESSION_INVALID)

    def test_the_expanded_filter_is_held_to_the_depth_limit(self) -> None:
        rules = {"customer": tuple(rule(f"n{i}", {"not": {"rule": f"n{i + 1}"}}) for i in range(7))
                 + (rule("n7", eq("name", "x")),)}
        where = {"rule": "n0"}
        for _ in range(30):
            where = {"not": where}

        self.assertEqual(error_of(where, rules=rules).detail.code, ErrorCode.QUERY_LIMIT_INVALID)


class EngineTests(unittest.TestCase):
    ROWS = {
        "crm": (
            {"id": "c1", "name": "Zulu", "status": "active"},
            {"id": "c3", "name": "Bravo", "status": "active"},
        ),
        "billing": ({"id": "c1", "plan": "pro"}, {"id": "c3", "plan": "enterprise"}),
    }

    def execute(self, where):
        executor = SourceRowsExecutor(self.ROWS)
        engine = QueryExecutionEngine(
            StaticRuntime(multi_source_catalog(RULES)),
            QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([executor]),
        )
        query = {"from": {"dataset": "customer"}, "select": ["name", "plan"], "where": where}
        return engine, query, engine.execute(query), executor

    def test_a_rule_runs_exactly_the_queries_the_typed_filter_runs(self) -> None:
        _, _, by_rule, rule_executor = self.execute({"rule": "active_paying"})
        _, _, typed, typed_executor = self.execute(
            {"all": [eq("status", "active"), {"field": "plan", "op": "in", "value": ["pro", "team"]}]}
        )

        self.assertEqual([dict(r) for r in by_rule.rows], [dict(r) for r in typed.rows])
        self.assertEqual(
            [(q.source_name, q.sql, q.parameters) for q in rule_executor.queries],
            [(q.source_name, q.sql, q.parameters) for q in typed_executor.queries],
        )
        self.assertEqual(by_rule.query_fingerprint, typed.query_fingerprint)

    def test_the_result_names_the_rules_the_query_named(self) -> None:
        _, _, result, _ = self.execute({"all": [{"rule": "active"}, {"rule": "vip"}]})

        self.assertEqual(
            result.applied,
            {"rules": [
                {"name": "active", "dataset": "customer", "definition": "The active customers."},
                {"name": "vip", "dataset": "customer", "definition": "The vip customers."},
            ]},
        )

    def test_the_result_leaves_out_hidden_rules_and_rules_reached_through_another(self) -> None:
        _, _, result, _ = self.execute({"rule": "vip"})

        self.assertEqual([entry["name"] for entry in result.applied["rules"]], ["vip"])

    def test_a_query_without_rules_has_an_empty_list(self) -> None:
        self.assertEqual(self.execute(eq("status", "active"))[2].applied, {"rules": []})

    def test_explain_lists_hidden_and_nested_rules_with_their_conditions(self) -> None:
        engine, query, _, _ = self.execute({"rule": "vip"})
        uses = {use.name: use for use in engine.explain(query).rules}

        self.assertEqual(set(uses), {"vip", "enterprise_tier"})
        self.assertFalse(uses["enterprise_tier"].visible)
        self.assertEqual(uses["enterprise_tier"].condition, '{"field":"plan","op":"eq","value":"enterprise"}')


if __name__ == "__main__":
    unittest.main()
