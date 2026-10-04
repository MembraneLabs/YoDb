"""Shared test fixtures: capabilities."""

from __future__ import annotations

from yodb.catalog import LogicalType, SourceKind
from yodb.planning import BooleanOperator, SourceCapabilities, TextOrdering
from yodb.query import ComparisonOperator


STRINGS = frozenset({LogicalType.STRING, LogicalType.ID})


KIND = SourceKind.NEO4J  # stand-in kind for a second, different database


LIMITED = SourceCapabilities(
    source_kind=KIND,
    filter_operators=frozenset({ComparisonOperator.EQ, ComparisonOperator.IN}),
    filterable_types=STRINGS,
    boolean_operators=frozenset({BooleanOperator.ALL}),
    supports_order=False,
    supports_limit=False,
    orderable_types=frozenset(),
    text_ordering=TextOrdering.CODE_POINT,
    maximum_rows=50,
)
