"""Correctness-first in-memory assembly for federated source scan results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import ErrorCode, ErrorDetail, QueryExecutionError
from ..planning import InMemoryAssemblyPlan
from ..query.models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    ComparisonOperator,
)


def assemble(plan: InMemoryAssemblyPlan, scan_rows: Sequence[Sequence[Mapping[str, object]]]) -> tuple[Mapping[str, object], ...]:
    """Anchor records on the identity source, then filter, sort, and page globally."""

    if len(scan_rows) != len(plan.scans):
        raise AssertionError("every planned source scan must have one result set")
    for scan, rows in zip(plan.scans, scan_rows, strict=True):
        if len(rows) > plan.maximum_rows_per_source:
            raise QueryExecutionError(
                ErrorDetail(
                    code=ErrorCode.QUERY_EXECUTION_FAILED,
                    message="A federated source scan exceeded the in-memory execution row budget.",
                    retryable=False,
                    source_name=scan.source.source_name,
                )
            )

    anchor_rows = scan_rows[0]
    records: dict[object, dict[str, object]] = {}
    for row in anchor_rows:
        identifier = _id(row, plan.scans[0].source.source_name)
        if identifier in records:
            raise _bad_identity(plan.scans[0].source.source_name)
        records[identifier] = dict(row)

    for scan, rows in zip(plan.scans[1:], scan_rows[1:], strict=True):
        seen: set[object] = set()
        for row in rows:
            identifier = _id(row, scan.source.source_name)
            if identifier in seen:
                raise _bad_identity(scan.source.source_name)
            seen.add(identifier)
            target = records.get(identifier)
            if target is not None:
                target.update(row)

    projected = [record for record in records.values() if _matches(plan.query.where, record)]
    _sort(projected, plan.query.order_by)
    limit = plan.query.page.first
    assert limit is not None
    if plan.query.constraints.maximum_results is not None:
        limit = min(limit, plan.query.constraints.maximum_results)
    selected = tuple(field.name for field in plan.query.select)
    return tuple({name: row.get(name) for name in selected} for row in projected[:limit])


def _id(row: Mapping[str, object], source_name: str) -> object:
    identifier = row.get("id")
    if identifier is None:
        raise _bad_identity(source_name)
    try:
        hash(identifier)
    except TypeError as error:
        raise _bad_identity(source_name) from error
    return identifier


def _bad_identity(source_name: str) -> QueryExecutionError:
    return QueryExecutionError(
        ErrorDetail(
            code=ErrorCode.QUERY_EXECUTION_FAILED,
            message="A federated source returned an invalid or duplicate logical ID.",
            retryable=False,
            source_name=source_name,
        )
    )


def _matches(expression: BoundFilterExpression | None, row: Mapping[str, object]) -> bool:
    return _truth(expression, row) is True


def _truth(expression: BoundFilterExpression | None, row: Mapping[str, object]) -> bool | None:
    if expression is None:
        return True
    if isinstance(expression, BoundPredicate):
        return _predicate(expression, row.get(expression.field.name))
    if isinstance(expression, BoundAllExpression):
        values = tuple(_truth(child, row) for child in expression.expressions)
        return False if False in values else None if None in values else True
    if isinstance(expression, BoundAnyExpression):
        values = tuple(_truth(child, row) for child in expression.expressions)
        return True if True in values else None if None in values else False
    if isinstance(expression, BoundNotExpression):
        value = _truth(expression.expression, row)
        return None if value is None else not value
    raise AssertionError(f"unknown expression {expression!r}")


def _predicate(predicate: BoundPredicate, actual: object) -> bool | None:
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
        return actual in expected
    if op is ComparisonOperator.NOT_IN:
        return actual not in expected
    if op is ComparisonOperator.GT:
        return actual > expected
    if op is ComparisonOperator.GTE:
        return actual >= expected
    if op is ComparisonOperator.LT:
        return actual < expected
    if op is ComparisonOperator.LTE:
        return actual <= expected
    if op is ComparisonOperator.CONTAINS:
        return str(expected) in str(actual)
    if op is ComparisonOperator.STARTS_WITH:
        return str(actual).startswith(str(expected))
    raise AssertionError(f"unknown operator {op!r}")


def _sort(rows: list[dict[str, object]], terms: Sequence[Any]) -> None:
    for term in reversed(terms):
        non_null = [row for row in rows if row.get(term.field.name) is not None]
        nulls = [row for row in rows if row.get(term.field.name) is None]
        non_null.sort(key=lambda row: row[term.field.name], reverse=term.direction.value == "desc")
        rows[:] = (nulls + non_null) if term.direction.value == "desc" else (non_null + nulls)
