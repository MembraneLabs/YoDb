"""Replace a ``{"rule": name}`` term with the filter the rule stands for.

The result is made only of ordinary bound filters, so the planner cannot tell a filter that came
from a rule from one the caller typed: pushdown, row guards and ``explain`` work unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from difflib import get_close_matches
import json

from ..catalog import Catalog, RuleSpec
from ..errors import ErrorCode
from ..query.extensions import TermRegistry
from ..query.models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundDataset,
    BoundFilterExpression,
    BoundNotExpression,
    AllExpression,
    AnyExpression,
    FilterExpression,
    NotExpression,
    RuleReference,
    RuleUse,
)
from ..query.parser import MAXIMUM_EXPRESSION_DEPTH, MAXIMUM_EXPRESSION_TERMS, parse_filter
from ..query.shape import fail

# A rule can build on other rules this many levels deep, and each condition is held to a smaller
# filter than a query's own, so a chain of rules cannot grow past what a query may be.
MAXIMUM_RULE_DEPTH = 8
MAXIMUM_CONDITION_DEPTH = 8
MAXIMUM_CONDITION_TERMS = 200

# A condition holds no extension terms (a semantic condition is refused inside a rule).
CONDITION_TERMS = TermRegistry()

Bind = Callable[[FilterExpression, str], BoundFilterExpression]


class RuleExpansion:
    """The rules of one catalog, and the record of which ones one query used."""

    def __init__(self, catalog: Catalog) -> None:
        self._rules: dict[str, tuple[str, RuleSpec]] = {
            rule.name: (dataset, rule) for dataset, rules in catalog.rules.items() for rule in rules
        }
        self._stack: list[str] = []
        self._uses: dict[str, RuleUse] = {}
        self._expanded_terms = 0

    @property
    def uses(self) -> tuple[RuleUse, ...]:
        return tuple(self._uses.values())

    def begin(self, name: str) -> None:
        """Start inside a rule (the catalog checking it), so a reference back to it is a loop."""

        self._stack.append(name)

    def expand(self, reference: RuleReference, root: BoundDataset, location: str, bind: Bind) -> BoundFilterExpression:
        from_query = not self._stack
        entry = self._rules.get(reference.name)
        # A hidden rule cannot be named in a query, and says so exactly as an unknown one does.
        if entry is None or (from_query and not entry[1].visible):
            fail(ErrorCode.RULE_NOT_FOUND, self._unknown_message(reference.name, root.name), location)
        dataset, rule = entry
        if dataset != root.name:
            fail(
                ErrorCode.RULE_NOT_APPLICABLE,
                f"Rule '{rule.name}' belongs to dataset '{dataset}' and cannot be used on '{root.name}'.",
                location,
            )
        if reference.name in self._stack:
            fail(
                ErrorCode.QUERY_EXPRESSION_INVALID,
                "Rules refer to each other in a loop: " + " -> ".join([*self._stack, reference.name]) + ".",
                location,
            )
        if len(self._stack) >= MAXIMUM_RULE_DEPTH:
            fail(ErrorCode.QUERY_LIMIT_INVALID, f"Rules may build on each other at most {MAXIMUM_RULE_DEPTH} levels deep.", location)

        condition = parse_filter(
            rule.condition,
            f"rules.{dataset}.{rule.name}.condition",
            CONDITION_TERMS,
            maximum_depth=MAXIMUM_CONDITION_DEPTH,
            maximum_terms=MAXIMUM_CONDITION_TERMS,
        )
        self._expanded_terms += _count_terms(condition)
        if self._expanded_terms > MAXIMUM_EXPRESSION_TERMS:
            fail(ErrorCode.QUERY_LIMIT_INVALID, f"The rules in this query expand to more than {MAXIMUM_EXPRESSION_TERMS} terms.", location)

        previous = self._uses.get(rule.name)
        self._uses[rule.name] = RuleUse(
            name=rule.name,
            dataset=dataset,
            definition=rule.definition,
            visible=rule.visible,
            condition=json.dumps(rule.condition, separators=(",", ":"), ensure_ascii=False, default=str),
            named_in_query=from_query or (previous is not None and previous.named_in_query),
        )
        self._stack.append(rule.name)
        try:
            return bind(condition, location)
        finally:
            self._stack.pop()

    def _unknown_message(self, name: str, dataset: str) -> str:
        offered = [rule_name for rule_name, (owner, rule) in self._rules.items() if owner == dataset and rule.visible]
        near = get_close_matches(name, offered, n=3)
        hint = " Did you mean " + " or ".join(f"'{n}'" for n in near) + "?" if near else ""
        return f"Unknown rule '{name}' on dataset '{dataset}'.{hint}"


def _count_terms(expression: FilterExpression) -> int:
    total, pending = 0, [expression]
    while pending:
        node = pending.pop()
        total += 1
        if isinstance(node, (AllExpression, AnyExpression)):
            pending.extend(node.expressions)
        elif isinstance(node, NotExpression):
            pending.append(node.expression)
    return total


def expression_size(expression: BoundFilterExpression | None) -> tuple[int, int]:
    """The number of terms in a bound filter and how deep it nests, without recursing."""

    if expression is None:
        return 0, 0
    terms, depth, pending = 0, 0, [(expression, 1)]
    while pending:
        node, level = pending.pop()
        terms += 1
        depth = max(depth, level)
        if isinstance(node, (BoundAllExpression, BoundAnyExpression)):
            pending.extend((child, level + 1) for child in node.expressions)
        elif isinstance(node, BoundNotExpression):
            pending.append((node.expression, level + 1))
    return terms, depth


def check_expanded_size(expression: BoundFilterExpression | None, location: str = "where") -> None:
    """Hold an expanded filter to the bounds a typed one is held to."""

    terms, depth = expression_size(expression)
    if terms > MAXIMUM_EXPRESSION_TERMS:
        fail(ErrorCode.QUERY_LIMIT_INVALID, f"With its rules expanded, the filter has {terms} terms; at most {MAXIMUM_EXPRESSION_TERMS} are allowed.", location)
    if depth > MAXIMUM_EXPRESSION_DEPTH:
        fail(ErrorCode.QUERY_LIMIT_INVALID, f"With its rules expanded, the filter nests {depth} levels; at most {MAXIMUM_EXPRESSION_DEPTH} are allowed.", location)
