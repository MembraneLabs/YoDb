"""The MCP server: a real MCP client talks to the server in-process, over the shop fixture.

The sources are in-memory SQL (``SqlWorld``), so every query really filters, joins and pages.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import importlib.util
import json
import unittest
from unittest import mock

from yodb import cli
from yodb.client import YoDb
from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.errors import ErrorCode, ErrorDetail, QueryExecutionError
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine
from yodb.planning import FederatedPhysicalPlanner, PostgresPlanningAdapter, SourcePlanningRegistry
from yodb.semantic import SemanticExtension, SemanticPolicy, SemanticRuntime
from yodb.mcp_server import _page_size, build_server, describe_catalog, error_text, query_language

from support.catalogs import StaticRuntime
from support.shop import shop_active, shop_world
from support.tickets import KeywordVerifier
from test_client_cli import run

HAS_MCP = importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("mcp.server.mcpserver") is not None
ACTIVE = shop_active()


def open_db(world=None, *, semantic: bool = False) -> YoDb:
    runtime = StaticRuntime(ACTIVE)
    extensions = (SemanticExtension(SemanticRuntime(KeywordVerifier()), policy=SemanticPolicy()),) if semantic else ()
    engine = QueryExecutionEngine(
        runtime, QueryCompilerRegistry([PostgresQueryCompiler()]), QueryExecutionAdapterRegistry([world or shop_world()]),
        planner=FederatedPhysicalPlanner(SourcePlanningRegistry([PostgresPlanningAdapter()]), extensions=extensions),
        extensions=extensions,
    )
    return YoDb(runtime, engine, semantic=semantic)


def session(calls, *, db=None, **options):
    """Open a client on the server and run ``calls(client)``; return what it returns."""

    from mcp import Client

    async def main():
        async with Client(build_server(db or open_db(), **options)) as client:
            return await calls(client)

    return asyncio.run(main())


def call(tool, arguments=None, **kwargs):
    async def calls(client):
        return await client.call_tool(tool, arguments or {})

    return session(calls, **kwargs)


def text(result) -> str:
    return "\n".join(block.text for block in result.content)


def where(field, op, value=None):
    leaf = {"field": field, "op": op}
    if op not in ("is_null", "is_not_null"):
        leaf["value"] = value
    return leaf


class DescribeTests(unittest.TestCase):
    described = describe_catalog(ACTIVE.catalog)

    def test_it_names_the_catalog_and_every_dataset_with_its_description(self) -> None:
        self.assertEqual(self.described["catalog"], {"name": "shop", "version": 1})
        self.assertEqual(set(self.described["datasets"]), {"customer", "ticket"})
        self.assertEqual(self.described["datasets"]["ticket"]["description"], "A support ticket.")

    def test_a_field_carries_its_type_and_description(self) -> None:
        fields = self.described["datasets"]["ticket"]["fields"]
        self.assertEqual(fields["priority"], {"type": "int", "description": "Priority."})
        self.assertEqual(fields["body"], {"type": "text", "description": "Ticket text."})

    def test_a_semantic_field_is_marked_only_when_a_semantic_condition_can_be_answered(self) -> None:
        described = describe_catalog(ACTIVE.catalog, semantic=True)
        self.assertEqual(described["datasets"]["ticket"]["fields"]["body"], {"type": "text", "description": "Ticket text.", "semantic": True})
        self.assertNotIn("semantic", described["datasets"]["ticket"]["fields"]["subject"])

    def test_an_internal_field_is_not_listed(self) -> None:
        self.assertNotIn("legacy_key", self.described["datasets"]["ticket"]["fields"])
        self.assertNotIn("legacy_key", json.dumps(self.described))

    def test_nothing_physical_is_listed(self) -> None:
        dumped = json.dumps(self.described)
        for physical in ("crm.customers", "desk.tickets", "customer_id\"}", "physical_name", "connection_ref", "billing"):
            self.assertNotIn(physical, dumped)

    def test_a_relationship_says_what_it_joins_and_whether_it_can_be_reversed(self) -> None:
        relationships = self.described["relationships"]
        self.assertEqual(relationships["customer_has_ticket"], {
            "from": "customer", "to": "ticket", "description": "A ticket belongs to a customer.", "aliases": ["tickets"],
            "cardinality": "one_to_many", "reversible": False, "on": "customer.id = ticket.customer_id", "traversable": True,
        })
        self.assertTrue(relationships["customer_ticket_both_ways"]["reversible"])
        self.assertEqual(relationships["referred_by"]["on"], "customer.referrer_id = customer.id")

    def test_a_relationship_on_an_internal_field_is_marked_untraversable_without_naming_the_field(self) -> None:
        link = self.described["relationships"]["customer_legacy_link"]
        self.assertFalse(link["traversable"])
        self.assertNotIn("on", link)
        self.assertIn("not public", link["why_not"])

    def test_aliases_and_example_values_are_listed_when_declared(self) -> None:
        from support.sql_world import evaluation
        from support.shop import RELATIONS, SOURCES

        datasets = """\
api_version: yodb/v0.1
catalog: {name: shop, version: 2}
datasets:
  customer:
    description: A customer.
    aliases: [client, account]
    fields:
      id:          {type: id,     description: Identity.}
      name:        {type: string, description: Name., aliases: [full_name]}
      country:     {type: string, description: Country., example_values: [US, UK]}
      plan:        {type: string, description: Billing plan.}
      referrer_id: {type: id,     description: Referrer.}
      seats:       {type: int,    description: Seats., example_values: [1, 20]}
  ticket:
    description: A support ticket.
    fields:
      id:          {type: id,     description: Identity.}
      customer_id: {type: id,     description: Owning customer.}
      subject:     {type: string, description: Subject.}
      status:      {type: string, description: Status.}
      priority:    {type: int,    description: Priority.}
      assignee:    {type: string, description: Assignee.}
      legacy_key:  {type: id,     description: An old key., visibility: internal}
      body:        {type: text,   description: Ticket text., semantic_eligible: true}
"""
        described = describe_catalog(evaluation(datasets, SOURCES, RELATIONS).catalog)
        customer = described["datasets"]["customer"]
        self.assertEqual(customer["aliases"], ["client", "account"])
        self.assertEqual(customer["fields"]["name"]["aliases"], ["full_name"])
        self.assertEqual(customer["fields"]["country"]["example_values"], ["US", "UK"])
        self.assertEqual(customer["fields"]["seats"]["example_values"], [1, 20])


class HelperTests(unittest.TestCase):
    def test_the_page_size_is_what_the_query_asked_for_or_the_default(self) -> None:
        self.assertEqual(_page_size({"from": {"dataset": "x"}}, 100), 100)
        self.assertEqual(_page_size({"from": {"dataset": "x"}}, 25), 25)
        self.assertEqual(_page_size({"page": {"first": 7}}, 100), 7)
        self.assertEqual(_page_size('{"page": {"first": 9}}', 100), 9)

    def test_the_page_size_is_bounded_by_the_result_cap(self) -> None:
        capped = {"page": {"first": 50}, "constraints": {"maximum_results": 5}}
        self.assertEqual(_page_size(capped, 100), 5)
        self.assertEqual(_page_size({**capped, "traverse": [{"relationship": "r"}]}, 100), 5)
        self.assertEqual(_page_size({"constraints": {"maximum_results": 500}}, 100), 100)

    def test_bytes_and_numbers_json_cannot_carry_are_made_safe(self) -> None:
        self.assertEqual(cli._jsonable({"a": b"\x00\xff", "b": float("nan"), "c": float("inf"), "d": 1.5}), {"a": "AP8=", "b": None, "c": None, "d": 1.5})

    def test_a_guard_error_says_what_to_do(self) -> None:
        error = QueryExecutionError(ErrorDetail(code=ErrorCode.QUERY_ROW_LIMIT_EXCEEDED, message="Too many rows.", retryable=False))
        self.assertIn("Add a filter", error_text(error))

    def test_the_page_size_of_something_unreadable_is_unknown_not_a_crash(self) -> None:
        for odd in ("{", "[1]", "3"):
            self.assertIsNone(_page_size(odd, 100))
        self.assertEqual(_page_size({"page": {"first": True}}, 100), 100)
        self.assertEqual(_page_size({"page": "x"}, 100), 100)

    def test_an_error_is_one_line_with_the_code_the_place_and_the_message(self) -> None:
        error = QueryExecutionError(ErrorDetail(
            code=ErrorCode.FIELD_NOT_FOUND, message="Unknown field 'x'.", retryable=False, location="select[1]"))
        self.assertEqual(error_text(error), "[field_not_found] at select[1]: Unknown field 'x'.")

    def test_an_error_names_its_source_lists_its_details_and_says_when_it_can_be_retried(self) -> None:
        error = QueryExecutionError(ErrorDetail(
            code=ErrorCode.SOURCE_UNAVAILABLE, message="The source could not be reached.", retryable=True,
            source_name="crm", details={"sources": {"crm": ["down"]}}))
        self.assertEqual(
            error_text(error).splitlines(),
            ["[source_unavailable] (source crm): The source could not be reached.", "  crm: down",
             "  (retryable: the same call may succeed if repeated)"],
        )

    def test_the_query_guide_names_every_operator_the_query_model_accepts(self) -> None:
        from yodb.query.models import ComparisonOperator

        for operator in ComparisonOperator:
            self.assertIn(operator.value, query_language(open_db().limits), operator)

    def test_the_query_guide_states_the_limits_of_the_engine_it_describes(self) -> None:
        from yodb.planning import JoinPolicy, PlannerPolicy
        from yodb.query import QueryValidationPolicy

        runtime = StaticRuntime(ACTIVE)
        engine = QueryExecutionEngine(
            runtime, QueryCompilerRegistry([PostgresQueryCompiler()]), QueryExecutionAdapterRegistry([shop_world()]),
            validation_policy=QueryValidationPolicy(default_page_size=20, maximum_page_size=60, maximum_in_values=30),
            planner=FederatedPhysicalPlanner(SourcePlanningRegistry([PostgresPlanningAdapter()]), policy=PlannerPolicy(maximum_rows_per_source=777)),
            join_policy=JoinPolicy(maximum_joined_rows=4321),
        )
        guide = query_language(YoDb(runtime, engine).limits)
        for stated in ("default 20, at most 60", "a list of at most 30", "more than 777 rows from one source", "join more than 4,321"):
            self.assertIn(stated, guide)
        self.assertNotIn("@", guide)

    def test_a_relationship_is_described_by_the_implementation_a_query_would_use(self) -> None:
        from types import SimpleNamespace as N

        edge = N(edge_type="KNOWS", from_endpoint=N(field="id"), to_endpoint=N(field="id"))
        fields = N(edge_type=None, from_endpoint=N(field="id"), to_endpoint=N(field="customer_id"))
        base = dict(from_dataset="customer", to_dataset="ticket", description="d", aliases=(), cardinality=N(value="one_to_many"), direction="uni")
        from yodb.mcp_server import _describe_relationship

        mixed = _describe_relationship(ACTIVE.catalog, N(**base, implementations=(edge, fields)))
        self.assertTrue(mixed["traversable"])
        self.assertEqual(mixed["on"], "customer.id = ticket.customer_id")
        only_edges = _describe_relationship(ACTIVE.catalog, N(**base, implementations=(edge,)))
        self.assertFalse(only_edges["traversable"])
        self.assertIn("graph edges", only_edges["why_not"])


@unittest.skipUnless(HAS_MCP, 'needs the "mcp" package, version 2 or later')
class ProtocolTests(unittest.TestCase):
    def test_the_server_offers_three_read_only_tools_and_instructions(self) -> None:
        async def calls(client):
            return client.instructions, (await client.list_tools()).tools

        instructions, tools = session(calls)
        self.assertIn("describe_catalog first", instructions)
        self.assertEqual([tool.name for tool in tools], ["describe_catalog", "query", "explain"])
        for tool in tools:
            self.assertTrue(tool.annotations.read_only_hint, tool.name)
            self.assertFalse(tool.annotations.destructive_hint, tool.name)
            self.assertFalse(tool.annotations.open_world_hint, tool.name)
            self.assertTrue(tool.description)

    def test_the_query_tool_documents_the_query_language(self) -> None:
        async def calls(client):
            return {tool.name: tool for tool in (await client.list_tools()).tools}

        tools = session(calls)
        for word in ("traverse", "starts_with", "is_null", "order_by", "at most 500", "Not available"):
            self.assertIn(word, tools["query"].description)
        self.assertEqual(tools["query"].input_schema["required"], ["query"])

    def test_describe_catalog_returns_the_description_as_structured_content_and_as_text(self) -> None:
        result = call("describe_catalog")
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, describe_catalog(ACTIVE.catalog))
        self.assertEqual(json.loads(text(result)), result.structured_content)


@unittest.skipUnless(HAS_MCP, 'needs the "mcp" package, version 2 or later')
class QueryToolTests(unittest.TestCase):
    def rows(self, query, **kwargs):
        result = call("query", {"query": query}, **kwargs)
        self.assertFalse(result.is_error, text(result))
        self.assertEqual(result.structured_content["row_count"], len(result.structured_content["rows"]))
        return result.structured_content["rows"]

    def error(self, query, tool="query"):
        result = call(tool, {"query": query})
        self.assertTrue(result.is_error)
        return text(result)

    def test_a_filtered_ordered_page_of_one_dataset(self) -> None:
        rows = self.rows({
            "from": {"dataset": "customer"}, "select": ["name", "plan"], "where": where("country", "eq", "US"),
            "order_by": [{"field": "name", "direction": "desc"}], "page": {"first": 2},
        })
        self.assertEqual(rows, [{"id": "c6", "name": "Fay", "plan": "basic"}, {"id": "c3", "name": "Cai", "plan": "pro"}])

    def test_the_query_may_be_json_text(self) -> None:
        rows = self.rows(json.dumps({"from": {"dataset": "customer"}, "select": ["name"], "where": where("id", "eq", "c1")}))
        self.assertEqual(rows, [{"id": "c1", "name": "Ann"}])

    def test_a_join_returns_flat_rows_with_prefixed_fields(self) -> None:
        rows = self.rows({
            "from": {"dataset": "customer"}, "select": ["name"], "where": where("country", "eq", "US"),
            "traverse": [{"relationship": "customer_has_ticket", "as": "ticket", "select": ["subject"], "where": where("status", "eq", "open")}],
            "order_by": [{"field": "ticket.priority", "direction": "desc"}],
        })
        self.assertEqual(rows, [
            {"id": "c1", "name": "Ann", "ticket.id": "t3", "ticket.subject": "Crash"},
            {"id": "c3", "name": "Cai", "ticket.id": "t6", "ticket.subject": "Refund"},
            {"id": "c1", "name": "Ann", "ticket.id": "t1", "ticket.subject": "Login"},
        ])

    def test_a_left_join_keeps_unmatched_rows_with_nulls(self) -> None:
        rows = self.rows({
            "from": {"dataset": "customer"}, "select": ["name"], "where": where("country", "eq", "UK"),
            "traverse": [{"relationship": "tickets", "as": "t", "select": ["subject"], "optional": True}],
        })
        self.assertEqual(rows, [
            {"id": "c2", "name": "Bob", "t.id": "t14", "t.subject": "Cancel"},
            {"id": "c2", "name": "Bob", "t.id": "t4", "t.subject": "Slow"},
            {"id": "c7", "name": "Gus", "t.id": None, "t.subject": None},
        ])

    def test_a_full_page_is_noted_and_a_short_one_is_not(self) -> None:
        full = call("query", {"query": {"from": {"dataset": "customer"}, "select": ["name"], "page": {"first": 3}}})
        self.assertIn("more rows may match", full.structured_content["note"])
        self.assertIn("3 rows", full.structured_content["note"])
        short = call("query", {"query": {"from": {"dataset": "customer"}, "select": ["name"], "page": {"first": 50}}})
        self.assertEqual(short.structured_content["row_count"], 8)
        self.assertNotIn("note", short.structured_content)

    def test_the_result_cap_bounds_a_query_over_several_sources_and_the_note_says_so(self) -> None:
        capped = {"from": {"dataset": "customer"}, "select": ["name", "plan"], "constraints": {"maximum_results": 2}, "page": {"first": 50}}
        result = call("query", {"query": capped})
        self.assertEqual(result.structured_content["rows"], [{"id": "c1", "name": "Ann", "plan": "pro"}, {"id": "c2", "name": "Bob", "plan": "basic"}])
        self.assertIn("2 rows", result.structured_content["note"])
        one_source = call("query", {"query": {**capped, "select": ["name"]}})
        self.assertEqual(one_source.structured_content["row_count"], 2)
        self.assertIn("note", one_source.structured_content)

    def test_no_rows_is_an_answer_not_an_error(self) -> None:
        result = call("query", {"query": {"from": {"dataset": "customer"}, "where": where("country", "eq", "ZZ")}})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, {"rows": [], "row_count": 0})

    def test_values_json_cannot_carry_are_made_text(self) -> None:
        class Rows:
            source_kind = shop_world().source_kind

            def execute(self, query, *, timeout_seconds=None):
                moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
                return ({"id": "c1", "name": moment, "seats": Decimal("1.50")},) if query.source_name == "crm" else ()

        rows = self.rows({"from": {"dataset": "customer"}, "select": ["name", "seats"]}, db=open_db(Rows()))
        self.assertEqual(rows, [{"id": "c1", "name": "2026-01-02T03:04:05+00:00", "seats": "1.50"}])

    def test_a_wrong_name_is_a_tool_error_with_the_code_and_the_place(self) -> None:
        self.assertIn("[dataset_not_found] at from.dataset", self.error({"from": {"dataset": "nope"}}))
        self.assertIn("[field_not_found] at select[1]", self.error({"from": {"dataset": "customer"}, "select": ["name", "nope"]}))
        self.assertIn("[query_value_type_invalid] at where.value", self.error({"from": {"dataset": "customer"}, "where": where("seats", "eq", "3")}))
        self.assertIn("[query_operator_not_supported]", self.error({"from": {"dataset": "customer"}, "where": where("name", "gt", "a")}))

    def test_an_internal_field_cannot_be_read_and_the_error_does_not_leak_it(self) -> None:
        message = self.error({"from": {"dataset": "ticket"}, "select": ["legacy_key"]})
        self.assertIn("[field_not_accessible] at select[0]", message)
        self.assertNotIn("k1", message)

    def test_a_join_problem_is_located_in_its_step(self) -> None:
        base = {"from": {"dataset": "customer"}}
        self.assertIn("[relationship_not_found]", self.error({**base, "traverse": [{"relationship": "nope"}]}))
        self.assertIn("[relationship_not_applicable]", self.error({**base, "from": {"dataset": "ticket"}, "traverse": [{"relationship": "customer_has_ticket", "direction": "reverse"}]}))
        self.assertIn("[field_not_accessible]", self.error({**base, "traverse": [{"relationship": "customer_legacy_link"}]}))
        self.assertIn("traverse[0]", self.error({**base, "traverse": [{"relationship": "tickets", "where": where("nope", "eq", 1)}]}))

    def test_text_that_is_not_json_and_sql_are_refused(self) -> None:
        self.assertIn("[query_shape_invalid]", self.error("SELECT * FROM customers"))
        self.assertIn("[query_shape_invalid]", self.error('{"from": '))

    def test_what_is_not_implemented_is_refused_by_name(self) -> None:
        self.assertIn("[query_shape_invalid]", self.error({"from": {"dataset": "customer"}, "page": {"first": 2, "after": "x"}}))
        self.assertIn("[query_", self.error({"from": {"dataset": "customer"}, "group_by": ["country"]}))

    def test_a_missing_argument_is_reported_by_the_protocol_layer(self) -> None:
        result = call("query", {})
        self.assertTrue(result.is_error)
        self.assertIn("query", text(result))

    def test_an_unexpected_failure_says_nothing_about_its_cause(self) -> None:
        class Broken:
            source_kind = shop_world().source_kind

            def execute(self, query, *, timeout_seconds=None):
                raise RuntimeError("password=hunter2 host=10.0.0.9")

        with self.assertLogs("mcp", level="ERROR"):
            result = call("query", {"query": {"from": {"dataset": "customer"}}}, db=open_db(Broken()))
        self.assertTrue(result.is_error)
        self.assertNotIn("hunter2", text(result))
        self.assertNotIn("10.0.0.9", text(result))

    def test_the_time_limit_given_to_the_server_reaches_every_query(self) -> None:
        db = open_db()
        with mock.patch.object(db, "query", wraps=db.query) as spy:
            call("query", {"query": {"from": {"dataset": "customer"}}}, db=db, timeout_seconds=7.5)
        self.assertEqual(spy.call_args.kwargs, {"timeout_seconds": 7.5})

    def test_several_calls_in_one_session_and_at_once(self) -> None:
        async def calls(client):
            queries = [{"from": {"dataset": "customer"}, "select": ["name"], "where": where("id", "eq", f"c{n}")} for n in range(1, 9)]
            return await asyncio.gather(*(client.call_tool("query", {"query": q}) for q in queries))

        results = session(calls)
        self.assertEqual(
            [r.structured_content["rows"][0]["name"] for r in results], ["Ann", "Bob", "Cai", "Dee", "Eli", "Fay", "Gus", "Hal"]
        )


@unittest.skipUnless(HAS_MCP, 'needs the "mcp" package, version 2 or later')
class SemanticTests(unittest.TestCase):
    REFUND = {"semantic": {"field": "body", "proposition": "mentions refund"}}

    def tools(self, **kwargs):
        async def calls(client):
            return {tool.name: tool for tool in (await client.list_tools()).tools}

        return session(calls, **kwargs)

    def test_without_a_semantic_filter_the_server_does_not_offer_one(self) -> None:
        self.assertIn("semantic conditions (none is configured on this server)", self.tools()["query"].description)
        self.assertNotIn("proposition", self.tools()["query"].description)
        self.assertNotIn("semantic", call("describe_catalog").structured_content["datasets"]["ticket"]["fields"]["body"])

    def test_without_a_semantic_filter_a_semantic_condition_is_refused_in_plain_words(self) -> None:
        result = call("query", {"query": {"from": {"dataset": "ticket"}, "where": self.REFUND}})
        self.assertTrue(result.is_error)
        self.assertIn("[query_feature_not_supported] at where: The query has a semantic condition, but nothing is configured", text(result))
        self.assertNotIn("BoundSemanticPredicate", text(result))

    def test_with_a_semantic_filter_the_guide_and_the_catalog_say_so(self) -> None:
        db = open_db(semantic=True)
        description = self.tools(db=db)["query"].description
        self.assertIn('"proposition"', description)
        self.assertNotIn("none is configured", description)
        self.assertTrue(call("describe_catalog", db=db).structured_content["datasets"]["ticket"]["fields"]["body"]["semantic"])

    def test_a_semantic_condition_runs_and_the_answer_says_how(self) -> None:
        result = call("query", {"query": {
            "from": {"dataset": "ticket"}, "select": ["subject"], "where": {"all": [self.REFUND, where("status", "eq", "open")]}}},
            db=open_db(semantic=True))
        self.assertFalse(result.is_error, text(result))
        self.assertEqual(result.structured_content["rows"], [{"id": "t6", "subject": "Refund"}])
        report = result.structured_content["semantic"]
        self.assertEqual(report["plan"], "verify_all")
        self.assertTrue(report["exact"])
        self.assertEqual(report["records_that_qualified"], 1)
        self.assertGreaterEqual(report["records_judged"], 1)

    def test_a_semantic_condition_on_the_joined_side(self) -> None:
        result = call("query", {"query": {
            "from": {"dataset": "customer"}, "select": ["name"],
            "traverse": [{"relationship": "tickets", "as": "t", "select": ["subject"], "where": self.REFUND}]}},
            db=open_db(semantic=True))
        self.assertFalse(result.is_error, text(result))
        self.assertEqual(
            sorted((row["name"], row["t.subject"]) for row in result.structured_content["rows"]),
            [("Ann", "Invoice"), ("Ann", "Upgrade"), ("Bob", "Cancel"), ("Cai", "Refund")],
        )
        report = result.structured_content["semantic"]
        self.assertTrue(report["exact"])
        self.assertEqual(report["records_that_qualified"], 4, "every batch of keys is counted, not only the last")
        self.assertEqual(report["records_judged"], report["records_considered"])
        self.assertGreater(report["records_judged"], 4)

    def test_a_joins_semantic_report_adds_up_its_batches(self) -> None:
        from yodb.planning import JoinPolicy

        runtime = StaticRuntime(ACTIVE)
        extensions = (SemanticExtension(SemanticRuntime(KeywordVerifier()), policy=SemanticPolicy()),)
        engine = QueryExecutionEngine(
            runtime, QueryCompilerRegistry([PostgresQueryCompiler()]), QueryExecutionAdapterRegistry([shop_world()]),
            planner=FederatedPhysicalPlanner(SourcePlanningRegistry([PostgresPlanningAdapter()]), extensions=extensions),
            extensions=extensions, join_policy=JoinPolicy(batch_size=2),          # 8 customers: four batches of keys
        )
        result = call("query", {"query": {
            "from": {"dataset": "customer"}, "select": ["name"],
            # a left join starts from the customers, so the tickets are judged batch by batch
            "traverse": [{"relationship": "tickets", "as": "t", "select": ["subject"], "where": self.REFUND, "optional": True}]}},
            db=YoDb(runtime, engine, semantic=True))
        self.assertEqual(sum(row["t.id"] is not None for row in result.structured_content["rows"]), 4)
        self.assertEqual(result.structured_content["semantic"]["records_that_qualified"], 4)
        self.assertEqual(result.structured_content["semantic"]["records_judged"], 12, "the 12 tickets of customers c1..c8")

    def test_a_semantic_condition_on_a_field_that_is_not_eligible_is_refused(self) -> None:
        result = call("query", {"query": {
            "from": {"dataset": "ticket"}, "where": {"semantic": {"field": "subject", "proposition": "mentions refund"}}}},
            db=open_db(semantic=True))
        self.assertTrue(result.is_error)
        self.assertIn("where", text(result))


@unittest.skipUnless(HAS_MCP, 'needs the "mcp" package, version 2 or later')
class ExplainToolTests(unittest.TestCase):
    def test_it_lists_the_steps_and_reads_nothing(self) -> None:
        world = shop_world()
        result = call("explain", {"query": {"from": {"dataset": "customer"}, "select": ["name", "plan"], "where": where("country", "eq", "US")}}, db=open_db(world))
        self.assertFalse(result.is_error, text(result))
        steps = result.structured_content["steps"]
        scan = next(step for step in steps if step["kind"] == "remote_scan" and step["at"] == "crm")
        self.assertEqual(scan["filters_applied_by_the_source"], ["country"])
        self.assertIn("record_assembly", [step["kind"] for step in steps])
        self.assertEqual(world.queries, [])

    def test_a_join_is_explained_with_its_key_and_its_driver(self) -> None:
        result = call("explain", {"query": {
            "from": {"dataset": "customer"}, "traverse": [{"relationship": "tickets", "as": "t", "where": where("status", "eq", "open")}]}})
        join = result.structured_content["steps"][-1]
        self.assertEqual(join["kind"], "hash_join")
        self.assertIn("on customer.id = ticket.customer_id", join["detail"])
        self.assertIn("driver=right", result.structured_content["optimizer"])

    def test_an_invalid_query_gives_the_same_error_as_running_it(self) -> None:
        query = {"query": {"from": {"dataset": "customer"}, "select": ["nope"]}}
        self.assertEqual(text(call("explain", query)).replace("explain", "query"), text(call("query", query)))


class CommandTests(unittest.TestCase):
    def test_the_mcp_command_serves_the_opened_catalog_with_its_time_limit(self) -> None:
        db = open_db()
        with mock.patch("yodb.mcp_server.serve") as serve:
            status, out, err = run(["mcp", "catalog/", "--timeout", "5"], db=db)
        self.assertEqual(status, 0)
        self.assertEqual(out, "", "standard output belongs to the protocol")
        self.assertIn("serving catalog shop v1", err)
        serve.assert_called_once_with(db, timeout_seconds=5.0)

    def test_the_time_limit_defaults_to_a_minute(self) -> None:
        self.assertEqual(cli._parser().parse_args(["mcp", "catalog/"]).timeout, 60.0)

    def test_without_the_mcp_package_the_command_says_how_to_install_it(self) -> None:
        with mock.patch.dict("sys.modules", {"mcp.server.mcpserver": None}):
            status, out, err = run(["mcp", "catalog/"], db=open_db())
        self.assertEqual(status, 2)
        self.assertEqual(out, "")
        self.assertIn('pip install "yodb[mcp]"', err)

    def test_a_catalog_that_does_not_open_fails_before_serving_and_prints_nothing_to_standard_output(self) -> None:
        with mock.patch("yodb.mcp_server.serve") as serve:
            status, out, err = run(["mcp", "/nonexistent-catalog", "--connections", "/nonexistent.yaml"])
        self.assertEqual(status, 2)
        self.assertEqual(out, "")
        serve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
