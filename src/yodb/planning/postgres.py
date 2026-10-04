"""PostgreSQL's declared capabilities for the planner.

Everything here is a promise the PostgreSQL compiler keeps exactly.  Text
``contains``/``starts_with`` are deliberately not declared: their case and
collation semantics are not pinned down yet, so they run in YoDb.
"""

from __future__ import annotations

from ..catalog import LogicalType, SourceKind, VectorMetric
from ..query.models import ComparisonOperator
from .adapter import CapabilityPlanningAdapter
from .capabilities import (
    BooleanOperator,
    KeyLookupCapability,
    SourceCapabilities,
    TextOrdering,
    VectorSearchCapability,
)

_SCALAR_TYPES = frozenset(
    {
        LogicalType.ID,
        LogicalType.STRING,
        LogicalType.TEXT,
        LogicalType.INT,
        LogicalType.FLOAT,
        LogicalType.BOOL,
        LogicalType.TIMESTAMP,
        LogicalType.UUID,
    }
)

POSTGRES_CAPABILITIES = SourceCapabilities(
    source_kind=SourceKind.POSTGRES,
    filter_operators=frozenset(
        {
            ComparisonOperator.EQ,
            ComparisonOperator.NE,
            ComparisonOperator.IN,
            ComparisonOperator.NOT_IN,
            ComparisonOperator.IS_NULL,
            ComparisonOperator.IS_NOT_NULL,
            ComparisonOperator.GT,
            ComparisonOperator.GTE,
            ComparisonOperator.LT,
            ComparisonOperator.LTE,
        }
    ),
    filterable_types=_SCALAR_TYPES,
    boolean_operators=frozenset(BooleanOperator),
    supports_order=True,
    supports_limit=True,
    orderable_types=_SCALAR_TYPES,
    text_ordering=TextOrdering.SOURCE_DEFINED,  # the database's collation
    maximum_rows=None,
    key_lookup=KeyLookupCapability(maximum_keys=5_000),  # one IN list, under the driver's parameter limit
    vector_search=VectorSearchCapability(  # requires the pgvector extension on the source
        metrics=frozenset(VectorMetric),
        combines_with_filters=True,  # WHERE ... ORDER BY embedding <=> $v LIMIT K
        maximum_shortlist=None,
    ),
)


class PostgresPlanningAdapter(CapabilityPlanningAdapter):
    def __init__(self) -> None:
        super().__init__(POSTGRES_CAPABILITIES)
