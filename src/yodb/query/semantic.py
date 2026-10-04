"""Rules for where a semantic condition may appear in a logical filter.

A semantic condition asks whether a proposition is true of a record.  It can
be answered cheaply only as a *conjunct*: candidates are narrowed by the other
terms and by retrieval, then verified.  Under ``any`` or ``not`` the set of
records that satisfy the proposition would have to be known in full, so V0.1
rejects those positions instead of silently approximating them.
"""

from __future__ import annotations

from ..errors import ErrorCode, ErrorDetail, QueryError
from .models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    BoundSemanticPredicate,
)


def semantic_predicates(expression: BoundFilterExpression | None) -> tuple[BoundSemanticPredicate, ...]:
    """Every semantic condition in ``expression``, in document order."""

    if expression is None or isinstance(expression, BoundPredicate):
        return ()
    if isinstance(expression, BoundSemanticPredicate):
        return (expression,)
    if isinstance(expression, (BoundAllExpression, BoundAnyExpression)):
        return tuple(found for child in expression.expressions for found in semantic_predicates(child))
    if isinstance(expression, BoundNotExpression):
        return semantic_predicates(expression.expression)
    raise AssertionError(f"Unknown bound expression: {expression!r}")


def validate_semantic_placement(
    expression: BoundFilterExpression | None,
    *,
    maximum_semantic_filters: int,
) -> None:
    """Reject a semantic condition outside conjunctive position or over budget."""

    found = semantic_predicates(expression)
    if len(found) > maximum_semantic_filters:
        _fail(
            ErrorCode.QUERY_LIMIT_INVALID,
            f"A query may contain at most {maximum_semantic_filters} semantic condition(s).",
            "where",
        )
    _check_conjunctive(expression, conjunctive=True, location="where")


def _check_conjunctive(expression: BoundFilterExpression | None, *, conjunctive: bool, location: str) -> None:
    if expression is None or isinstance(expression, BoundPredicate):
        return
    if isinstance(expression, BoundSemanticPredicate):
        if not conjunctive:
            _fail(
                ErrorCode.QUERY_EXPRESSION_INVALID,
                "A semantic condition cannot appear under 'any' or 'not'; place it in the top-level 'where' or 'all'.",
                location,
            )
        return
    if isinstance(expression, BoundAllExpression):
        for index, child in enumerate(expression.expressions):
            _check_conjunctive(child, conjunctive=conjunctive, location=f"{location}.all[{index}]")
    elif isinstance(expression, BoundAnyExpression):
        for index, child in enumerate(expression.expressions):
            _check_conjunctive(child, conjunctive=False, location=f"{location}.any[{index}]")
    elif isinstance(expression, BoundNotExpression):
        _check_conjunctive(expression.expression, conjunctive=False, location=f"{location}.not")
    else:
        raise AssertionError(f"Unknown bound expression: {expression!r}")


def _fail(code: ErrorCode, message: str, location: str) -> None:
    raise QueryError(ErrorDetail(code=code, message=message, retryable=False, location=location))
