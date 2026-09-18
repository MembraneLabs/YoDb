"""Backend-neutral physical planning contracts for federated YoDb queries.

These objects are private execution descriptions.  They contain validated
source bindings only; callers continue to use the logical query model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias, runtime_checkable

from ..query.models import BoundFilterExpression, BoundOrderTerm, ComparisonOperator
from ..query.resolution import LogicalIdLink, ResolvedField, SingleSourceQueryBinding, SourceResolvedQuery


@dataclass(frozen=True)
class SourceCapabilities:
    """Semantics a source adapter promises for a physical source operation.

    Capabilities never expose source table names, index names, or native query
    syntax.  They establish only whether a logical operation can be delegated
    without changing its meaning.
    """

    predicate_operators: frozenset[ComparisonOperator]
    supports_projection_pushdown: bool
    supports_ordered_limit: bool
    supports_key_lookup: bool
    maximum_keys_per_lookup: int | None = None
    supports_native_relation: bool = False
    supports_bounded_traversal: bool = False
    supports_semantic_search: bool = False


@runtime_checkable
class SourceCapabilityProvider(Protocol):
    """Return source-kind-specific capabilities for a resolved source binding."""

    def capabilities_for(self, source: SingleSourceQueryBinding) -> SourceCapabilities: ...


@dataclass(frozen=True)
class SourceScanPlan:
    """One source-local request with only approved fields and operations."""

    source: SingleSourceQueryBinding
    projection: tuple[ResolvedField, ...]
    pushed_where: BoundFilterExpression | None
    order_by: tuple[BoundOrderTerm, ...] = ()
    limit: int | None = None


@dataclass(frozen=True)
class KeySetPlan:
    """Materialize a bounded, deduplicated logical-ID set from a scan."""

    input: SourceScanPlan
    logical_id: ResolvedField
    maximum_distinct_keys: int


@dataclass(frozen=True)
class KeyTransferPlan:
    """Use IDs from one source as bounded lookup inputs in another source."""

    keys: KeySetPlan
    destination: SourceScanPlan
    link: LogicalIdLink
    maximum_keys_per_batch: int


FederatedContributor: TypeAlias = SourceScanPlan | KeyTransferPlan


@dataclass(frozen=True)
class SingleSourcePlan:
    """A query whose required logical fields can be completed by one source."""

    scan: SourceScanPlan
    residual_where: BoundFilterExpression | None = None


@dataclass(frozen=True)
class InMemoryAssemblyPlan:
    """Anchor records, enrich them by declared logical ID, then finish locally."""

    anchor: SourceScanPlan
    contributors: tuple[FederatedContributor, ...]
    query: SourceResolvedQuery
    residual_where: BoundFilterExpression | None
    maximum_rows_per_source: int


PhysicalQueryPlan: TypeAlias = SingleSourcePlan | InMemoryAssemblyPlan


@dataclass(frozen=True)
class CandidatePlan:
    """A semantically valid candidate with a stable, human-readable strategy."""

    strategy: str
    plan: PhysicalQueryPlan

