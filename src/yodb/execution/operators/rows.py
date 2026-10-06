"""Row semantics YoDb applies itself: SQL-style filtering and PostgreSQL-style ordering."""

from __future__ import annotations

from typing import Any

from ...query.models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    ComparisonOperator,
    SortDirection,
)
from ..contracts import LogicalRow


def matches(expression: BoundFilterExpression | None, row: LogicalRow) -> bool:
    """Evaluate SQL-style three-valued logic; only TRUE passes a WHERE clause."""

    return truth(expression, row) is True


def truth(expression: BoundFilterExpression | None, row: LogicalRow) -> bool | None:
    if expression is None:
        return True
    if isinstance(expression, BoundPredicate):
        return predicate_truth(expression, row.get(expression.field.name))
    if isinstance(expression, BoundAllExpression):
        return _and(truth(item, row) for item in expression.expressions)
    if isinstance(expression, BoundAnyExpression):
        return _or(truth(item, row) for item in expression.expressions)
    if isinstance(expression, BoundNotExpression):
        value = truth(expression.expression, row)
        return None if value is None else not value
    raise AssertionError(f"Unknown filter expression: {expression!r}")


def predicate_truth(predicate: BoundPredicate, actual: object | None) -> bool | None:
    op = predicate.operator
    if op is ComparisonOperator.IS_NULL:
        return actual is None
    if op is ComparisonOperator.IS_NOT_NULL:
        return actual is not None
    if actual is None:
        return None
    expected = predicate.value
    if op is ComparisonOperator.EQ:
        return actual == expected
    if op is ComparisonOperator.NE:
        return actual != expected
    if op is ComparisonOperator.IN:
        return actual in expected  # type: ignore[operator]
    if op is ComparisonOperator.NOT_IN:
        values = expected  # type: ignore[assignment]
        return None if any(value is None for value in values) else actual not in values
    if op is ComparisonOperator.CONTAINS:
        return str(expected) in str(actual)
    if op is ComparisonOperator.STARTS_WITH:
        return str(actual).startswith(str(expected))
    try:
        if op is ComparisonOperator.GT:
            return actual > expected  # type: ignore[operator]
        if op is ComparisonOperator.GTE:
            return actual >= expected  # type: ignore[operator]
        if op is ComparisonOperator.LT:
            return actual < expected  # type: ignore[operator]
        if op is ComparisonOperator.LTE:
            return actual <= expected  # type: ignore[operator]
    except TypeError:
        return False
    raise AssertionError(f"Unsupported predicate operator: {op!r}")


def _and(values: object) -> bool | None:
    unknown = False
    for value in values:  # type: ignore[union-attr]
        if value is False:
            return False
        unknown = unknown or value is None
    return None if unknown else True


def _or(values: object) -> bool | None:
    unknown = False
    for value in values:  # type: ignore[union-attr]
        if value is True:
            return True
        unknown = unknown or value is None
    return None if unknown else False


def compare_rows(left: LogicalRow, right: LogicalRow, order_by: tuple[Any, ...]) -> int:
    """Order two rows; NULLs sort like PostgreSQL's default (last for ASC, first for DESC)."""

    for term in order_by:
        first, second = left.get(term.field.name), right.get(term.field.name)
        if first is None and second is None:
            continue
        if first is None:
            return 1 if term.direction is SortDirection.ASC else -1
        if second is None:
            return -1 if term.direction is SortDirection.ASC else 1
        if first == second:
            continue
        try:
            comparison = -1 if first < second else 1
        except TypeError:
            comparison = -1 if repr(first) < repr(second) else 1
        return comparison if term.direction is SortDirection.ASC else -comparison
    return 0
