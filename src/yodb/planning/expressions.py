"""Helpers over bound filter expressions shared by plan nodes and planning operators."""

from __future__ import annotations

from ..query.models import (
    BoundAllExpression,
    BoundExtensionTerm,
    BoundFilterExpression,
    BoundPredicate,
)


def conjunctive_predicates(expression: BoundFilterExpression | None) -> tuple[BoundPredicate, ...] | None:
    """The leaves of a pure ``all``-of-predicates filter, or None if it is anything else."""

    if expression is None:
        return ()
    if isinstance(expression, BoundPredicate):
        return (expression,)
    if isinstance(expression, BoundAllExpression):
        children = tuple(conjunctive_predicates(child) for child in expression.expressions)
        if any(child is None for child in children):
            return None
        return tuple(predicate for child in children for predicate in child or ())
    return None


def filter_shape(expression: BoundFilterExpression | None) -> object:
    """A value-free structural description of a filter (for plan fingerprints)."""

    if expression is None:
        return None
    if isinstance(expression, BoundPredicate):
        return {"field": expression.field.name, "operator": expression.operator.value}
    if isinstance(expression, BoundAllExpression):
        return {"all": [filter_shape(child) for child in expression.expressions]}
    if isinstance(expression, BoundExtensionTerm):
        return {"extension": type(expression).__name__}
    name = type(expression).__name__.replace("Bound", "").replace("Expression", "").lower()
    child = getattr(expression, "expression", None)
    children = getattr(expression, "expressions", None)
    return {name: filter_shape(child) if child is not None else [filter_shape(item) for item in children]}


def filter_fields(expression: BoundFilterExpression | None) -> tuple[str, ...]:
    """The logical field names a filter mentions, in order."""

    if expression is None:
        return ()
    if isinstance(expression, BoundPredicate):
        return (expression.field.name,)
    if isinstance(expression, BoundAllExpression):
        return tuple(name for child in expression.expressions for name in filter_fields(child))
    if isinstance(expression, BoundExtensionTerm):
        return ()
    child = getattr(expression, "expression", None)
    children = getattr(expression, "expressions", None)
    return filter_fields(child) if child is not None else tuple(name for item in children for name in filter_fields(item))
