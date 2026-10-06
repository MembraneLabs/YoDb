"""Helpers over bound filter expressions shared by plan nodes and planning operators."""

from __future__ import annotations

from ..query.models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundExtensionTerm,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    ComparisonOperator,
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


def top_level_conjuncts(expression: BoundFilterExpression | None) -> tuple[BoundFilterExpression, ...]:
    """The independent conditions the filter ANDs together: its leaves and any ``any``/``not`` subtrees."""

    if expression is None:
        return ()
    if isinstance(expression, BoundAllExpression):
        return tuple(part for child in expression.expressions for part in top_level_conjuncts(child))
    return (expression,)


def holds_when_every_field_is_null(expression: BoundFilterExpression) -> bool:
    """Whether the filter is TRUE for a record whose fields are all NULL (SQL three-valued logic).

    A source that has no row for a record contributes only NULLs.  A condition on that source
    may be pushed down as a required match only if it can never be TRUE of such a record: then
    "the source returned no row" and "the condition is not true" are the same thing.
    """

    return _on_nulls(expression) is True


def _on_nulls(expression: BoundFilterExpression) -> bool | None:
    if isinstance(expression, BoundPredicate):
        if expression.operator is ComparisonOperator.IS_NULL:
            return True
        if expression.operator is ComparisonOperator.IS_NOT_NULL:
            return False
        return None                                   # any other comparison with NULL is unknown
    if isinstance(expression, BoundAllExpression):
        parts = [_on_nulls(child) for child in expression.expressions]
        return False if False in parts else (None if None in parts else True)
    if isinstance(expression, BoundAnyExpression):
        parts = [_on_nulls(child) for child in expression.expressions]
        return True if True in parts else (None if None in parts else False)
    if isinstance(expression, BoundNotExpression):
        inner = _on_nulls(expression.expression)
        return None if inner is None else not inner
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
