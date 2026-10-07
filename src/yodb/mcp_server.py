"""An MCP server over one open catalog, so an AI agent can discover and query it.

    yodb mcp CATALOG                      serve the catalog over standard input/output

Three read-only tools:

    describe_catalog      the datasets, their public fields and the relationships between them
    query                 run one logical query and return its rows
    explain               the plan for a query, without running it

The agent sees what a caller of the query model sees and nothing more: logical names, public fields,
and errors that carry a code and a location but never SQL, credentials or physical names.  Needs the
``mcp`` package (``pip install "yodb[mcp]"``); it is imported only when a server is built.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from .catalog import Catalog, RelationshipSpec, Visibility
from .cli import _jsonable
from .client import YoDb
from .errors import ErrorCode, YoDbError
from .query import QueryValidationPolicy

SERVER_NAME = "yodb"

_PAGE = QueryValidationPolicy()
_GUARDS = (ErrorCode.QUERY_ROW_LIMIT_EXCEEDED, ErrorCode.QUERY_COORDINATOR_LIMIT_EXCEEDED)

INSTRUCTIONS = """\
YoDb answers read-only questions about business datasets whose data may live in several databases.
Call describe_catalog first: it lists the datasets, fields and relationships you may name. Then call
query with a structured query (not SQL). Use explain to see how a query would run without running it.
An error names a code and the part of the query that is wrong; fix that part and call again."""

QUERY_LANGUAGE = """\
A query is a JSON object. Only names returned by describe_catalog are accepted; SQL is not.

{"from": {"dataset": "customer"},                      required
 "select": ["name", "country"],                        optional; default every public field; "id" is always returned
 "where": <condition>,                                 optional
 "traverse": [<step>],                                 optional; a join to one other dataset
 "order_by": [{"field": "name", "direction": "asc"}],  optional; "direction" is required: "asc" or "desc"; ties break by "id"
 "page": {"first": 25}}                                optional; default 100, at most 500

<condition> is one of:
  {"field": "status", "op": "eq", "value": "open"}
  {"all": [<condition>, ...]}    {"any": [<condition>, ...]}    {"not": <condition>}

Operators by field type:
  every type except json and bytes:  eq, ne, in, not_in (value is a list), is_null, is_not_null (no value)
  int, float, timestamp:             gt, gte, lt, lte
  string, text:                      contains, starts_with (case-sensitive)
Values are typed: an int field takes 3, not "3"; a bool takes true; a timestamp takes RFC 3339 with an
offset ("2026-01-01T00:00:00Z"). Test for a missing value with is_null, never with eq null.
A field may be null. As in SQL, ne, not_in and "not" do not match a null: add {"any": [..., is_null]} to
include them. Nulls sort last ascending and first descending. Text matching is exact and case-sensitive,
so check a field's example values, or look at a few rows, before filtering on a value you guessed.

<step> joins the "from" dataset to another over a relationship from describe_catalog:
  {"relationship": "customer_has_ticket",   required
   "as": "ticket",                          prefix of the joined fields in the rows; default the relationship name
   "direction": "forward",                  "forward" starts at the relationship's "from" dataset; "reverse" starts
                                            at its "to" dataset and needs "reversible": true
   "select": ["subject"],                   fields of the joined dataset; "<as>.id" is always returned
   "where": <condition>,                    a filter on the joined dataset, with its own field names
   "optional": false}                       true keeps rows with no match (a left join), their joined fields null
Rows are flat, one per matching pair: {"id": ..., "name": ..., "ticket.id": ..., "ticket.subject": ...}.
order_by may name a joined field as "ticket.priority"; the top-level "select" and "where" take only fields
of the "from" dataset, and joined fields are filtered in the step's own "where". That "where" decides
which joined rows can match, before matching: it cannot find rows that have no match. For those, use
"optional": true with no step "where" and keep the rows whose "<as>.id" is null.
Only a relationship with "traversable": true can be used.

Not available: aggregation (count, sum, group by), distinct, more than one traverse step, a next page
(page.after), and comparing two fields with each other@NO_SEMANTIC@. To count, fetch the rows and count them.
A query that would read more than about 10,000 rows from one source, or join more than 50,000, is refused
with query_row_limit_exceeded or query_coordinator_limit_exceeded, whatever page.first is: add a filter
on a field with few matching rows (an eq or in, or a range). contains and starts_with are applied after
the rows are read, and can keep other filters from narrowing the read: when a query is refused, call
explain and check which filters appear under "filters_applied_by_the_source"."""

SEMANTIC_LANGUAGE = """A semantic condition asks whether a statement is true of a record's text, judged by a model:
  {"semantic": {"field": "body", "proposition": "the customer is asking when their card will arrive"}}
The field must be marked "semantic": true in describe_catalog. Put it at the top of "where" or inside "all"
(never under "any" or "not"); one per query; it also works in a traverse step's "where". The proposition
is a statement of at most 1,000 characters. Add ordinary conditions beside it: the row limits above apply
to the records it has to judge. The answer's "semantic" entry says how it ran. "exact": false means the
records were shortlisted by similarity first: every row returned is a true match, but matches may be
missing, more so with a small page.first, so do not present such rows as the complete or the top-N answer."""


def query_language(*, semantic: bool = False) -> str:
    """The guide an agent reads in the query tool's description."""

    if semantic:
        return QUERY_LANGUAGE.replace("@NO_SEMANTIC@", "") + "\n\n" + SEMANTIC_LANGUAGE
    return QUERY_LANGUAGE.replace("@NO_SEMANTIC@", ", and semantic conditions (none is configured on this server)")


def describe_catalog(catalog: Catalog, *, semantic: bool = False) -> dict[str, Any]:
    """What a query may name: datasets with their public fields, and the relationships between them.

    A field is marked ``semantic`` only when ``semantic`` says a semantic condition can be answered.
    """

    datasets = {}
    for name, dataset in catalog.datasets.items():
        fields = {}
        for field_name, spec in dataset.fields.items():
            if spec.visibility is not Visibility.PUBLIC:
                continue
            field: dict[str, Any] = {"type": spec.type.value, "description": spec.description}
            if spec.aliases:
                field["aliases"] = list(spec.aliases)
            if spec.example_values:
                field["example_values"] = _jsonable(list(spec.example_values))
            if spec.semantic_eligible and semantic:
                field["semantic"] = True
            fields[field_name] = field
        datasets[name] = {
            "description": dataset.description,
            **({"aliases": list(dataset.aliases)} if dataset.aliases else {}),
            "fields": fields,
        }
    return {
        "catalog": {"name": catalog.metadata.name, "version": catalog.metadata.version},
        "datasets": datasets,
        "relationships": {
            name: _describe_relationship(catalog, relationship) for name, relationship in catalog.relationships.items()
        },
    }


def _describe_relationship(catalog: Catalog, relationship: RelationshipSpec) -> dict[str, Any]:
    described: dict[str, Any] = {
        "from": relationship.from_dataset,
        "to": relationship.to_dataset,
        "description": relationship.description,
        **({"aliases": list(relationship.aliases)} if relationship.aliases else {}),
        "cardinality": relationship.cardinality.value,
        "reversible": relationship.direction == "bi",
    }
    implementation = relationship.implementations[0]
    left, right = implementation.from_endpoint.field, implementation.to_endpoint.field
    if implementation.edge_type is not None:
        described["traversable"] = False
        described["why_not"] = "It is stored as graph edges, which cannot be traversed yet."
        return described
    described["on"] = f"{relationship.from_dataset}.{left} = {relationship.to_dataset}.{right}"
    public = all(
        _is_public(catalog, dataset, field)
        for dataset, field in ((relationship.from_dataset, left), (relationship.to_dataset, right))
    )
    described["traversable"] = public
    if not public:
        del described["on"]
        described["why_not"] = "It joins on a field that is not public."
    return described


def _is_public(catalog: Catalog, dataset: str, field: str) -> bool:
    spec = catalog.datasets[dataset].fields.get(field)
    return spec is not None and spec.visibility is Visibility.PUBLIC


def build_server(db: YoDb, *, timeout_seconds: float | None = 60.0):
    """An MCP server whose tools read ``db``.  ``timeout_seconds`` bounds each query as a whole."""

    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    semantic = db.semantic_enabled
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, log_level="WARNING")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def guarded(call):
        try:
            return call()
        except YoDbError as error:
            raise ToolError(error_text(error)) from error

    @server.tool(
        name="describe_catalog",
        title="Describe the catalog",
        description="List what can be queried: every dataset with its public fields (type, description, and "
        "example values where the catalog gives them), and the relationships that a query may traverse to join "
        'two datasets ("reversible" ones in either direction). Call this before writing a query. It reads no data.',
        annotations=read_only,
    )
    def describe_catalog_tool() -> dict[str, Any]:
        return guarded(lambda: describe_catalog(db.catalog, semantic=semantic))

    @server.tool(
        name="query",
        title="Run a query",
        description="Run one read-only query and return its rows as "
        '{"rows": [...], "row_count": n}. A "note" is added when the page is full and more rows may match.\n\n'
        + query_language(semantic=semantic),
        annotations=read_only,
    )
    def query_tool(query: dict[str, Any] | str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            result = db.query(query, timeout_seconds=timeout_seconds)
            rows = [_jsonable(dict(row)) for row in result.rows]
            answer: dict[str, Any] = {"rows": rows, "row_count": len(rows)}
            first, joined = _page_size(query)
            report = result.reports.get("semantic")
            if report is not None:
                answer["semantic"] = _semantic_report(report, counts=not joined)
            if first is not None and len(rows) >= first:
                more = "narrow the filter" if first >= _PAGE.maximum_page_size else (
                    f"narrow the filter, or raise page.first (at most {_PAGE.maximum_page_size})")
                answer["note"] = f"The page is full ({len(rows)} rows), so more rows may match. There is no next page: {more}."
            return answer

        return guarded(run)

    @server.tool(
        name="explain",
        title="Explain a query",
        description="Show how a query would run, without running it: which sources are read and in what order, "
        "which filters each database applies, how a join is driven, and how the plan was chosen. Takes the "
        "same query object as the query tool, and reports the same errors for an invalid one.",
        annotations=read_only,
    )
    def explain_tool(query: dict[str, Any] | str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            explanation = db.explain(query)
            return {
                "plan_kind": explanation.plan_kind,
                "steps": [_step(node) for node in explanation.nodes],
                "optimizer": list(explanation.optimizer),
            }

        return guarded(run)

    return server


def serve(db: YoDb, *, timeout_seconds: float | None = 60.0) -> None:
    """Serve ``db`` over standard input/output until the client disconnects."""

    build_server(db, timeout_seconds=timeout_seconds).run("stdio")


def error_text(error: YoDbError) -> str:
    """One error as text an agent can act on: the code, where in the query, and what is wrong."""

    detail = error.detail
    where = f" at {detail.location}" if detail.location else ""
    source = f" (source {detail.source_name})" if detail.source_name else ""
    lines = [f"[{detail.code.value}]{where}{source}: {detail.message}"]
    for source_name, items in (detail.details.get("sources") or {}).items():
        lines.extend(f"  {source_name}: {item}" for item in items)
    if detail.code in _GUARDS:
        lines.append("  Too many rows would be read. Add a filter that few rows match (eq, in or a range); page.first does not help.")
    if detail.retryable:
        lines.append("  (retryable: the same call may succeed if repeated)")
    return "\n".join(lines)


def _semantic_report(report, *, counts: bool = True) -> dict[str, Any]:
    """How a semantic condition ran, in terms an agent can weigh: exact, or possibly missing matches.

    A join judges one batch of keys at a time and reports only its last batch, so its counts are left out.
    """

    stats = report.stats
    described: dict[str, Any] = {"plan": stats.plan.value, "exact": stats.shortlisted is None}
    if not counts:
        return described
    described |= {
        "records_considered": stats.candidates_considered,
        "records_judged": stats.verified,
        "records_that_qualified": stats.qualified,
    }
    if stats.shortlisted is not None:
        described["shortlisted_by_vector_search"] = stats.shortlisted
    return described


def _step(node) -> dict[str, Any]:
    step: dict[str, Any] = {"kind": node.kind, "at": node.location}
    if node.fields:
        step["fields"] = list(node.fields)
    if node.pushed_filter_fields:
        step["filters_applied_by_the_source"] = list(node.pushed_filter_fields)
    if node.residual_filter:
        step["filters_applied_by_yodb"] = True
    if node.ordering:
        step["order"] = list(node.ordering)
    if node.limit is not None:
        step["limit"] = node.limit
    if node.detail:
        step["detail"] = list(node.detail)
    return step


def _page_size(query: Mapping[str, Any] | str) -> tuple[int | None, bool]:
    """The page size a valid query asked for and whether it is a join, read leniently: the query has already run."""

    if isinstance(query, str):
        try:
            query = json.loads(query)
        except ValueError:
            return None, False
    if not isinstance(query, Mapping):
        return None, False
    joined = bool(query.get("traverse"))
    page = query.get("page")
    first = page.get("first") if isinstance(page, Mapping) else None
    size = first if isinstance(first, int) and not isinstance(first, bool) else _PAGE.default_page_size
    constraints = query.get("constraints")
    cap = constraints.get("maximum_results") if isinstance(constraints, Mapping) else None
    if joined and isinstance(cap, int) and not isinstance(cap, bool):      # only a join's page honours the cap
        size = min(size, cap)
    return size, joined
