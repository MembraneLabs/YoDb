"""Filter terms contributed by extension operators (e.g. a semantic condition).

A plain predicate is ``{field, op, value}``.  An extension adds a *new kind of term*
to the ``where`` tree (e.g. ``{"semantic": {...}}``).  Everything the query layer
needs to know about such a term is declared once, in a :class:`TermExtension`:

* how to **parse** its raw body and **bind** it to the catalog;
* how it appears in the **query fingerprint**;
* which **fields it reads** (so source resolution can include them);
* **placement** rules (only as a conjunct? how many per query?).

The parser, validator and resolver consult the registry, so adding a term never
means editing them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import ErrorCode
from .models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundDataset,
    BoundExtensionTerm,
    BoundField,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    ExtensionTerm,
    FieldUse,
)
from .shape import fail


@dataclass(frozen=True)
class TermExtension:
    key: str                                        # the JSON key that introduces the term
    parsed_type: type
    bound_type: type
    parse: Callable[[Mapping[str, Any], str], ExtensionTerm]
    bind: Callable[[ExtensionTerm, BoundDataset, str, Any], BoundExtensionTerm]   # (term, root, location, policy)
    describe: Callable[[BoundExtensionTerm], object]                              # fingerprint payload
    uses: Callable[[BoundExtensionTerm], tuple[tuple[BoundField, FieldUse], ...]]
    maximum: int = 1                                # terms allowed per query (policy.limits[key] overrides)
    noun: str = "term"                              # for messages: "semantic condition"
    conjunctive_only: bool = True                   # may only be ANDed, never under any/not
    # Sources (beyond the ones owning the fields it reads) the term needs, resolved with the
    # dataset identity only: (term, catalog, dataset name) -> source names.  The planner's
    # extension operator receives them as ``SourceResolvedQuery.extension_sources``.
    extra_sources: Callable[[BoundExtensionTerm, Any, str], tuple[str, ...]] = lambda term, catalog, dataset: ()
    uses_minimum_quality: bool = False              # whether ``constraints.minimum_quality`` applies to it


class TermRegistry:
    """The extension terms a query may contain."""

    def __init__(self, extensions: Iterable[TermExtension] = ()) -> None:
        self._by_key: dict[str, TermExtension] = {}
        for extension in extensions:
            if extension.key in self._by_key:
                raise ValueError(f"duplicate term extension '{extension.key}'")
            self._by_key[extension.key] = extension

    def __iter__(self):
        return iter(self._by_key.values())

    def keys(self) -> tuple[str, ...]:
        return tuple(self._by_key)

    def by_key(self, key: str) -> TermExtension | None:
        return self._by_key.get(key)

    def for_parsed(self, term: ExtensionTerm) -> TermExtension:
        return self._find(lambda e: isinstance(term, e.parsed_type), term)

    def for_bound(self, term: BoundExtensionTerm) -> TermExtension:
        return self._find(lambda e: isinstance(term, e.bound_type), term)

    def _find(self, matches: Callable[[TermExtension], bool], term: object) -> TermExtension:
        for extension in self._by_key.values():
            if matches(extension):
                return extension
        raise AssertionError(f"No term extension is registered for {type(term).__name__}")


def extension_terms(expression: BoundFilterExpression | None) -> tuple[BoundExtensionTerm, ...]:
    """Every extension term in ``expression``, in document order."""

    if expression is None or isinstance(expression, BoundPredicate):
        return ()
    if isinstance(expression, BoundExtensionTerm):
        return (expression,)
    if isinstance(expression, (BoundAllExpression, BoundAnyExpression)):
        return tuple(term for child in expression.expressions for term in extension_terms(child))
    if isinstance(expression, BoundNotExpression):
        return extension_terms(expression.expression)
    raise AssertionError(f"Unknown bound expression: {expression!r}")


def terms_of_type(expression: BoundFilterExpression | None, bound_type: type) -> tuple[BoundExtensionTerm, ...]:
    return tuple(term for term in extension_terms(expression) if isinstance(term, bound_type))


def strip_terms(expression: BoundFilterExpression | None, bound_type: type) -> BoundFilterExpression | None:
    """The filter without its conjunct terms of ``bound_type`` (placement was validated)."""

    if expression is None or isinstance(expression, BoundPredicate):
        return expression
    if isinstance(expression, bound_type):
        return None
    if isinstance(expression, BoundAllExpression):
        kept = tuple(item for item in (strip_terms(child, bound_type) for child in expression.expressions) if item is not None)
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else BoundAllExpression(kept)
    if isinstance(expression, BoundExtensionTerm):
        return expression                       # a different extension's term stays
    raise AssertionError(f"A claimed term cannot sit under {type(expression).__name__}")


def validate_placement(expression: BoundFilterExpression | None, registry: TermRegistry, policy: Any) -> None:
    """Reject an extension term over budget or outside the positions its extension allows."""

    for extension in registry:
        found = terms_of_type(expression, extension.bound_type)
        allowed = policy.limit(extension.key, extension.maximum)
        if len(found) > allowed:
            fail(
                ErrorCode.QUERY_LIMIT_INVALID,
                f"A query may contain at most {allowed} {extension.noun}(s).",
                "where",
            )
    _check_conjunctive(expression, registry, conjunctive=True, location="where")


def _check_conjunctive(expression: BoundFilterExpression | None, registry: TermRegistry, *, conjunctive: bool, location: str) -> None:
    if expression is None or isinstance(expression, BoundPredicate):
        return
    if isinstance(expression, BoundExtensionTerm):
        extension = registry.for_bound(expression)
        if extension.conjunctive_only and not conjunctive:
            fail(
                ErrorCode.QUERY_EXPRESSION_INVALID,
                f"A {extension.noun} cannot appear under 'any' or 'not'; place it in the top-level 'where' or 'all'.",
                location,
            )
        return
    if isinstance(expression, BoundAllExpression):
        for index, child in enumerate(expression.expressions):
            _check_conjunctive(child, registry, conjunctive=conjunctive, location=f"{location}.all[{index}]")
    elif isinstance(expression, BoundAnyExpression):
        for index, child in enumerate(expression.expressions):
            _check_conjunctive(child, registry, conjunctive=False, location=f"{location}.any[{index}]")
    elif isinstance(expression, BoundNotExpression):
        _check_conjunctive(expression.expression, registry, conjunctive=False, location=f"{location}.not")
    else:
        raise AssertionError(f"Unknown bound expression: {expression!r}")
