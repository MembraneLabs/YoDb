"""What a source adapter declares about itself so the planner can plan for it.

A new database is added by writing an adapter (planning + compiler + executor)
and filling in one :class:`SourceCapabilities`.  The planner and executor read
only this description; they never branch on a database's name.  Every field is a
*promise* the adapter's compiler must keep exactly: if a source declares
``filter_operators={EQ}``, the planner will push an ``EQ`` and keep everything
else in YoDb.  Declaring less is always safe; declaring more than the compiler
can preserve is a bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..catalog import LogicalType, SourceKind, VectorMetric
from ..query.models import ComparisonOperator


class BooleanOperator(str, Enum):
    ALL = "all"
    ANY = "any"
    NOT = "not"


class TextOrdering(str, Enum):
    """How the source orders text relative to YoDb's own comparison.

    ``CODE_POINT`` matches the coordinator's ordering exactly.  ``SOURCE_DEFINED``
    means the source applies its own collation, so a single-source query trusts
    the source's order, while an ordering YoDb must apply across several sources
    uses code-point order and may differ for mixed-case text.
    """

    CODE_POINT = "code_point"
    SOURCE_DEFINED = "source_defined"


@dataclass(frozen=True)
class KeyLookupCapability:
    """The source can restrict a scan to a set of logical IDs."""

    maximum_keys: int

    def __post_init__(self) -> None:
        if self.maximum_keys < 1:
            raise ValueError("maximum_keys must be positive")


@dataclass(frozen=True)
class VectorSearchCapability:
    """The source can return the K rows nearest to a query vector.

    ``combines_with_filters``: the ranking is computed *among rows that satisfy
    the scan's other filters and key restriction*.  If False, the source can only
    rank its whole collection, so the planner uses a shortlist only for a query
    with no other filter.
    """

    metrics: frozenset[VectorMetric]
    combines_with_filters: bool
    maximum_shortlist: int | None = None

    def __post_init__(self) -> None:
        if not self.metrics:
            raise ValueError("a vector search capability needs at least one metric")
        if self.maximum_shortlist is not None and self.maximum_shortlist < 1:
            raise ValueError("maximum_shortlist must be positive")


@dataclass(frozen=True)
class SourceCapabilities:
    source_kind: SourceKind
    # Filtering the source can enforce exactly.
    filter_operators: frozenset[ComparisonOperator]
    filterable_types: frozenset[LogicalType]
    boolean_operators: frozenset[BooleanOperator]
    # Ordering and paging the source can enforce exactly.
    supports_order: bool
    supports_limit: bool
    orderable_types: frozenset[LogicalType]
    text_ordering: TextOrdering
    # Hard cap on rows one scan may return (None: no source-imposed cap).
    maximum_rows: int | None = None
    key_lookup: KeyLookupCapability | None = None
    vector_search: VectorSearchCapability | None = None

    def __post_init__(self) -> None:
        if self.maximum_rows is not None and self.maximum_rows < 1:
            raise ValueError("maximum_rows must be positive")
        if self.supports_limit and not self.supports_order:
            raise ValueError("a source that pushes a limit must also push the order that defines it")
