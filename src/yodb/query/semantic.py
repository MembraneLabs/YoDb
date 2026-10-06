"""The semantic condition as a registered query term.

``{"semantic": {"field": ..., "proposition": ...}}`` asks whether the proposition
is true of a record's text.  It can only be answered cheaply as a *conjunct*
(candidates are narrowed by the other terms, then verified), so V0.1 rejects it
under ``any``/``not``, allows one per query, and requires a public text field the
catalog marks ``semantic_eligible``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..catalog import LogicalType
from ..errors import ErrorCode
from .extensions import TermExtension, strip_terms, terms_of_type
from .fields import bind_public_field
from .models import BoundDataset, BoundExtensionTerm, BoundField, BoundFilterExpression, ExtensionTerm, FieldUse
from .shape import fail, mapping, non_empty_string, reject_unknown

_KEYS = frozenset({"field", "proposition"})
MAXIMUM_PROPOSITION_CHARS = 1_000     # policy.limits["semantic.proposition_chars"] overrides


@dataclass(frozen=True)
class SemanticPredicate(ExtensionTerm):
    """Is ``proposition`` true of the record's text ``field``? (before binding)"""

    field: str
    proposition: str


@dataclass(frozen=True)
class BoundSemanticPredicate(BoundExtensionTerm):
    """A validated semantic condition on a public, semantic-eligible text field.

    It asks whether the proposition is *true of the record*.  Vector similarity
    is never part of its meaning; it is only a possible candidate-generation
    technique chosen below the logical boundary.
    """

    field: BoundField
    proposition: str


def _parse(body: Any, location: str) -> SemanticPredicate:
    body = mapping(body, location)
    reject_unknown(body, _KEYS, location)
    return SemanticPredicate(
        field=non_empty_string(body.get("field"), f"{location}.field"),
        proposition=non_empty_string(body.get("proposition"), f"{location}.proposition"),
    )


def _bind(term: SemanticPredicate, root: BoundDataset, location: str, policy: Any) -> BoundSemanticPredicate:
    field = bind_public_field(root, term.field, f"{location}.semantic.field")
    if field.spec.type not in {LogicalType.STRING, LogicalType.TEXT}:
        fail(
            ErrorCode.QUERY_OPERATOR_NOT_SUPPORTED,
            f"Field '{field.name}' of type '{field.spec.type.value}' cannot take a semantic condition.",
            f"{location}.semantic.field",
        )
    if not field.spec.semantic_eligible:
        fail(
            ErrorCode.QUERY_OPERATOR_NOT_SUPPORTED,
            f"Field '{field.name}' is not marked semantic_eligible in the catalog.",
            f"{location}.semantic.field",
        )
    proposition = term.proposition.strip()
    if not proposition:
        fail(ErrorCode.QUERY_VALUE_TYPE_INVALID, "A proposition must not be blank.", f"{location}.semantic.proposition")
    limit = policy.limit("semantic.proposition_chars", MAXIMUM_PROPOSITION_CHARS)
    if len(proposition) > limit:
        fail(
            ErrorCode.QUERY_LIMIT_INVALID,
            f"A proposition may not exceed {limit} characters.",
            f"{location}.semantic.proposition",
        )
    return BoundSemanticPredicate(field=field, proposition=proposition)


def _vector_stores(term: BoundSemanticPredicate, catalog, dataset: str) -> tuple[str, ...]:
    """Sources that hold only the vectors of the term's field (the text lives elsewhere)."""

    return tuple(
        name
        for name, source in catalog.sources.items()
        if (representation := source.datasets.get(dataset)) is not None
        and term.field.name in representation.embeddings
        and term.field.name not in representation.fields
    )


SEMANTIC_TERM = TermExtension(
    key="semantic",
    parsed_type=SemanticPredicate,
    bound_type=BoundSemanticPredicate,
    parse=_parse,
    bind=_bind,
    describe=lambda term: {"semantic": {"field": term.field.name, "proposition": term.proposition}},
    uses=lambda term: ((term.field, FieldUse.EXTENSION),),
    noun="semantic condition",
    extra_sources=_vector_stores,
    uses_minimum_quality=True,
)


def semantic_predicates(expression: BoundFilterExpression | None) -> tuple[BoundSemanticPredicate, ...]:
    """Every semantic condition in ``expression``, in document order."""

    return terms_of_type(expression, BoundSemanticPredicate)  # type: ignore[return-value]


def without_semantic_terms(expression: BoundFilterExpression | None) -> BoundFilterExpression | None:
    """The filter with its semantic conjuncts removed."""

    return strip_terms(expression, BoundSemanticPredicate)
