"""Strict parsing of JSON/YAML-compatible input into logical query objects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import ErrorCode
from .models import (
    AllExpression,
    AnyExpression,
    ComparisonOperator,
    DatasetReference,
    FilterExpression,
    NotExpression,
    OrderTerm,
    PageRequest,
    Predicate,
    QueryConstraints,
    QueryRequest,
    SortDirection,
)
from .registry import DEFAULT_TERMS
from .extensions import TermRegistry
from .shape import fail as _fail
from .shape import list_of as _list
from .shape import mapping as _mapping
from .shape import non_empty_string as _non_empty_string
from .shape import reject_unknown as _reject_unknown


# A filter is a tree; untrusted input must not be able to make walking it unbounded.
MAXIMUM_EXPRESSION_DEPTH = 32
MAXIMUM_EXPRESSION_TERMS = 1_000

_QUERY_KEYS = frozenset({"from", "select", "where", "order_by", "page", "constraints"})
_FROM_KEYS = frozenset({"dataset", "as"})
_PREDICATE_KEYS = frozenset({"field", "op", "value"})
_ORDER_KEYS = frozenset({"field", "direction"})
_PAGE_KEYS = frozenset({"first", "after"})
_CONSTRAINT_KEYS = frozenset(
    {"maximum_results", "maximum_latency_ms", "maximum_cost", "minimum_quality", "allow_partial_results"}
)


def parse_query(
    raw: Mapping[str, Any],
    terms: TermRegistry = DEFAULT_TERMS,
    *,
    maximum_depth: int = MAXIMUM_EXPRESSION_DEPTH,
    maximum_terms: int = MAXIMUM_EXPRESSION_TERMS,
) -> QueryRequest:
    """Parse one logical query without accessing the catalog or a source.

    A filter may nest ``maximum_depth`` levels and contain ``maximum_terms`` nodes; deeper or
    larger input is refused before anything recurses over it.
    """

    root = _mapping(raw, "query")
    if "traverse" in root:
        _fail(
            ErrorCode.QUERY_FEATURE_NOT_SUPPORTED,
            "Traversal is reserved for a later query-model increment.",
        )
    for key in terms.keys():
        if key in root:
            _fail(
                ErrorCode.QUERY_FEATURE_NOT_SUPPORTED,
                f"'{key}' is a filter term: use {{'{key}': {{...}}}} inside 'where'.",
                key,
            )
    _reject_unknown(root, _QUERY_KEYS, "query")
    if "from" not in root:
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "A query requires 'from'.", "from")

    return QueryRequest(
        root=_parse_from(root["from"]),
        select=_parse_select(root.get("select")),
        where=_parse_expression(root["where"], "where", terms, _Budget(maximum_depth, maximum_terms)) if "where" in root else None,
        order_by=_parse_order_by(root.get("order_by")),
        page=_parse_page(root["page"]) if "page" in root else None,
        constraints=_parse_constraints(root["constraints"]) if "constraints" in root else None,
    )


def _parse_from(raw: Any) -> DatasetReference:
    value = _mapping(raw, "from")
    _reject_unknown(value, _FROM_KEYS, "from")
    dataset = _non_empty_string(value.get("dataset"), "from.dataset")
    scope = _non_empty_string(value.get("as", dataset), "from.as")
    return DatasetReference(dataset=dataset, scope=scope)


def _parse_select(raw: Any) -> tuple[str, ...] | None:
    if raw is None:
        return None
    values = _list(raw, "select")
    fields = tuple(_non_empty_string(value, f"select[{index}]") for index, value in enumerate(values))
    if len(set(fields)) != len(fields):
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "'select' must not contain duplicate fields.", "select")
    return fields


class _Budget:
    """How much more filter the parser will read."""

    def __init__(self, maximum_depth: int, maximum_terms: int) -> None:
        self.maximum_depth = maximum_depth
        self.remaining_terms = maximum_terms
        self.maximum_terms = maximum_terms

    def enter(self, depth: int, location: str) -> None:
        if depth > self.maximum_depth:
            _fail(ErrorCode.QUERY_LIMIT_INVALID, f"A filter may nest at most {self.maximum_depth} levels deep.", location)
        self.remaining_terms -= 1
        if self.remaining_terms < 0:
            _fail(ErrorCode.QUERY_LIMIT_INVALID, f"A filter may contain at most {self.maximum_terms} terms.", location)


def _parse_expression(raw: Any, location: str, terms: TermRegistry, budget: _Budget, depth: int = 1) -> FilterExpression:
    budget.enter(depth, location)
    value = _mapping(raw, location)
    keys = set(value)
    for extension in terms:
        if extension.key in keys:
            if len(keys) != 1:
                _fail(
                    ErrorCode.QUERY_EXPRESSION_INVALID,
                    f"A {extension.noun} term must contain only '{extension.key}'.",
                    location,
                )
            return extension.parse(value[extension.key], f"{location}.{extension.key}")
    boolean_keys = keys & {"all", "any", "not"}
    if boolean_keys:
        if len(keys) != 1 or len(boolean_keys) != 1:
            _fail(
                ErrorCode.QUERY_EXPRESSION_INVALID,
                "A Boolean expression must contain exactly one of 'all', 'any', or 'not'.",
                location,
            )
        kind = next(iter(boolean_keys))
        if kind == "not":
            return NotExpression(_parse_expression(value["not"], f"{location}.not", terms, budget, depth + 1))
        children = _list(value[kind], f"{location}.{kind}")
        if not children:
            _fail(
                ErrorCode.QUERY_EXPRESSION_INVALID,
                f"'{kind}' must contain at least one expression.",
                f"{location}.{kind}",
            )
        parsed = tuple(
            _parse_expression(child, f"{location}.{kind}[{index}]", terms, budget, depth + 1)
            for index, child in enumerate(children)
        )
        return AllExpression(parsed) if kind == "all" else AnyExpression(parsed)

    _reject_unknown(value, _PREDICATE_KEYS, location)
    field = _non_empty_string(value.get("field"), f"{location}.field")
    operator_value = _non_empty_string(value.get("op"), f"{location}.op")
    try:
        operator = ComparisonOperator(operator_value)
    except ValueError:
        _fail(ErrorCode.QUERY_OPERATOR_NOT_SUPPORTED, f"Unknown filter operator '{operator_value}'.", f"{location}.op")
    return Predicate(
        field=field,
        operator=operator,
        value=value.get("value"),
        value_supplied="value" in value,
    )


def _parse_order_by(raw: Any) -> tuple[OrderTerm, ...]:
    if raw is None:
        return ()
    values = _list(raw, "order_by")
    terms: list[OrderTerm] = []
    seen: set[str] = set()
    for index, raw_term in enumerate(values):
        location = f"order_by[{index}]"
        term = _mapping(raw_term, location)
        _reject_unknown(term, _ORDER_KEYS, location)
        field = _non_empty_string(term.get("field"), f"{location}.field")
        if field in seen:
            _fail(ErrorCode.QUERY_SHAPE_INVALID, "'order_by' must not repeat a field.", f"{location}.field")
        seen.add(field)
        direction_value = _non_empty_string(term.get("direction"), f"{location}.direction")
        try:
            direction = SortDirection(direction_value)
        except ValueError:
            _fail(ErrorCode.QUERY_SHAPE_INVALID, "Order direction must be 'asc' or 'desc'.", f"{location}.direction")
        terms.append(OrderTerm(field=field, direction=direction))
    return tuple(terms)


def _parse_page(raw: Any) -> PageRequest:
    value = _mapping(raw, "page")
    _reject_unknown(value, _PAGE_KEYS, "page")
    first = value.get("first")
    after = value.get("after")
    if first is not None and (type(first) is not int or first <= 0):
        _fail(ErrorCode.QUERY_LIMIT_INVALID, "'page.first' must be a positive integer.", "page.first")
    if after is not None and (not isinstance(after, str) or not after.strip()):
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "'page.after' must be a non-empty cursor string.", "page.after")
    return PageRequest(first=first, after=after)


def _parse_constraints(raw: Any) -> QueryConstraints:
    value = _mapping(raw, "constraints")
    _reject_unknown(value, _CONSTRAINT_KEYS, "constraints")
    for name in ("maximum_results", "maximum_latency_ms"):
        candidate = value.get(name)
        if candidate is not None and (type(candidate) is not int or candidate <= 0):
            _fail(ErrorCode.QUERY_LIMIT_INVALID, f"'constraints.{name}' must be a positive integer.", f"constraints.{name}")
    for name in ("maximum_cost", "minimum_quality"):
        candidate = value.get(name)
        if candidate is not None and (
            type(candidate) not in (int, float) or isinstance(candidate, bool) or candidate <= 0
        ):
            _fail(ErrorCode.QUERY_LIMIT_INVALID, f"'constraints.{name}' must be a positive number.", f"constraints.{name}")
    partial = value.get("allow_partial_results")
    if partial is not None and type(partial) is not bool:
        _fail(
            ErrorCode.QUERY_SHAPE_INVALID,
            "'constraints.allow_partial_results' must be a boolean.",
            "constraints.allow_partial_results",
        )
    return QueryConstraints(
        maximum_results=value.get("maximum_results"),
        maximum_latency_ms=value.get("maximum_latency_ms"),
        maximum_cost=value.get("maximum_cost"),
        minimum_quality=value.get("minimum_quality"),
        allow_partial_results=partial,
    )
