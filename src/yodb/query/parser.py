"""Strict parsing of JSON/YAML-compatible input into logical query objects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import ErrorCode, ErrorDetail, QueryError
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


_QUERY_KEYS = frozenset({"from", "select", "where", "order_by", "page", "constraints"})
_FROM_KEYS = frozenset({"dataset", "as"})
_PREDICATE_KEYS = frozenset({"field", "op", "value"})
_ORDER_KEYS = frozenset({"field", "direction"})
_PAGE_KEYS = frozenset({"first", "after"})
_CONSTRAINT_KEYS = frozenset(
    {"maximum_results", "maximum_latency_ms", "maximum_cost", "minimum_quality", "allow_partial_results"}
)


def parse_query(raw: Mapping[str, Any]) -> QueryRequest:
    """Parse one logical query without accessing the catalog or a source."""

    root = _mapping(raw, "query")
    if "traverse" in root or "semantic" in root:
        _fail(
            ErrorCode.QUERY_FEATURE_NOT_SUPPORTED,
            "Traversal and semantic search are reserved for a later query-model increment.",
        )
    _reject_unknown(root, _QUERY_KEYS, "query")
    if "from" not in root:
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "A query requires 'from'.", "from")

    return QueryRequest(
        root=_parse_from(root["from"]),
        select=_parse_select(root.get("select")),
        where=_parse_expression(root["where"], "where") if "where" in root else None,
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


def _parse_expression(raw: Any, location: str) -> FilterExpression:
    value = _mapping(raw, location)
    keys = set(value)
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
            return NotExpression(_parse_expression(value["not"], f"{location}.not"))
        children = _list(value[kind], f"{location}.{kind}")
        if not children:
            _fail(
                ErrorCode.QUERY_EXPRESSION_INVALID,
                f"'{kind}' must contain at least one expression.",
                f"{location}.{kind}",
            )
        parsed = tuple(
            _parse_expression(child, f"{location}.{kind}[{index}]")
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


def _mapping(raw: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "Expected an object.", location)
    if not all(isinstance(key, str) for key in raw):
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "Object keys must be strings.", location)
    return raw


def _list(raw: Any, location: str) -> list[Any]:
    if not isinstance(raw, list):
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "Expected an array.", location)
    return raw


def _non_empty_string(raw: Any, location: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "Expected a non-empty string.", location)
    return raw


def _reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], location: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(
            ErrorCode.QUERY_SHAPE_INVALID,
            f"Unknown field(s): {', '.join(unknown)}.",
            location,
        )


def _fail(code: ErrorCode, message: str, location: str | None = None) -> None:
    raise QueryError(ErrorDetail(code=code, message=message, retryable=False, location=location))
