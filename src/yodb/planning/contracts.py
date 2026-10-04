"""Physical, backend-neutral plans produced from one resolved YoDb query."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias, runtime_checkable

from ..catalog import SourceKind, VectorMetric
from ..query.models import BoundFilterExpression, BoundOrderTerm, BoundQuery, ComparisonOperator
from ..query.resolution import ResolvedField, SingleSourceQueryBinding, SourceResolvedQuery
from ..semantic import SemanticPlanKind
from .capabilities import SourceCapabilities


class PlanLocationKind(str, Enum):
    REMOTE = "remote"
    COORDINATOR = "coordinator"


class ResultCompleteness(str, Enum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


class ResultShape(str, Enum):
    RECORDS = "records"
    ID_SET = "id_set"
    EDGE_SET = "edge_set"


@dataclass(frozen=True)
class PlanLocation:
    kind: PlanLocationKind
    source_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind is PlanLocationKind.REMOTE and not self.source_name:
            raise ValueError("a remote plan location requires a source name")
        if self.kind is PlanLocationKind.COORDINATOR and self.source_name is not None:
            raise ValueError("a coordinator plan location cannot name a source")


@dataclass(frozen=True)
class PlanProperties:
    """Facts the planner must preserve when connecting physical nodes."""

    output_fields: tuple[ResolvedField, ...]
    logical_id: ResolvedField | None
    ids_are_unique: bool
    ordering: tuple[BoundOrderTerm, ...] | None
    location: PlanLocation
    completeness: ResultCompleteness
    result_shape: ResultShape
    catalog_fingerprint: str


@dataclass(frozen=True)
class SourceOperationRequest:
    """The logical operations the planner asks one adapter to place remotely."""

    projection: tuple[ResolvedField, ...]
    filter: BoundFilterExpression | None
    order_by: tuple[BoundOrderTerm, ...]
    limit: int | None
    complete_result: bool


@dataclass(frozen=True)
class PushdownDecision:
    """Exactly which requested operations an adapter can preserve remotely."""

    accepted_filter: BoundFilterExpression | None
    residual_filter: BoundFilterExpression | None
    accepted_projection: tuple[ResolvedField, ...]
    accepted_order: tuple[BoundOrderTerm, ...]
    accepted_limit: int | None
    limit_is_guaranteed: bool
    reasons: tuple[str, ...] = ()


@runtime_checkable
class SourcePlanningAdapter(Protocol):
    """Semantic source capability, separate from source-native compilation."""

    @property
    def source_kind(self) -> SourceKind: ...

    @property
    def capabilities(self) -> "SourceCapabilities":
        """Everything the planner may rely on this source doing exactly."""

    def plan_remote_scan(
        self,
        source: SingleSourceQueryBinding,
        requested: SourceOperationRequest,
    ) -> PushdownDecision: ...


@dataclass(frozen=True)
class VectorSearch:
    """Return the ``shortlist_size`` rows nearest to the query vector.

    The planner fixes everything except ``query_vector``; the executor fills it
    in after embedding the proposition.  The search runs under the scan's other
    filters and key restriction, never instead of them.
    """

    column: str
    metric: VectorMetric
    model: str
    dimensions: int
    shortlist_size: int
    query_vector: tuple[float, ...] | None = None


@dataclass(frozen=True)
class RemoteScan:
    """One validated, compilable source-local scan fragment."""

    source: SingleSourceQueryBinding
    projection: tuple[ResolvedField, ...]
    pushed_filter: BoundFilterExpression | None
    order_by: tuple[BoundOrderTerm, ...]
    limit: int | None
    maximum_rows: int | None
    properties: PlanProperties
    # Filled in by the executor at run time (never by the planner): a bounded
    # set of logical IDs learned from another scan, ANDed with ``pushed_filter``.
    key_filter: tuple[object, ...] | None = None
    vector_search: VectorSearch | None = None
    # Largest ID set this scan's source accepts as a restriction; None means the
    # source cannot be restricted, so the executor never tries.
    key_lookup_limit: int | None = None


@dataclass(frozen=True)
class RecordAssembly:
    """Left-enrich root records with contributor fields using logical identity."""

    anchor: RemoteScan
    contributors: tuple[RemoteScan, ...]
    required_contributor_matches: tuple[str, ...]
    properties: PlanProperties
    # Largest logical-ID set the executor may transfer between sources to
    # restrict a later scan; ``None`` disables transfer.
    maximum_transfer_keys: int | None = None


@dataclass(frozen=True)
class SemanticVerify:
    """Keep the input records for which the proposition is true.

    Candidates are ordered by ``order_by`` and verified in batches until
    ``first`` records qualify, so a page that fills early costs fewer model
    calls.  ``plan`` records whether the input was shortlisted by vector search.
    """

    input: "PhysicalPlan"
    field: ResolvedField
    proposition: str
    plan: SemanticPlanKind
    order_by: tuple[BoundOrderTerm, ...]
    first: int | None
    minimum_quality: float | None
    maximum_candidates: int
    maximum_cost: float | None
    maximum_latency_ms: int | None
    embedding_model: str | None
    embedding_dimensions: int | None
    properties: PlanProperties
    # Why this plan was chosen (e.g. why a shortlist was unavailable).
    choice_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoordinatorFilter:
    """Defensive complete Boolean evaluation after source work/assembly."""

    input: "PhysicalPlan"
    expression: BoundFilterExpression | None
    properties: PlanProperties


@dataclass(frozen=True)
class CoordinatorSortPage:
    """Global deterministic ordering and page placement at the coordinator."""

    input: "PhysicalPlan"
    order_by: tuple[BoundOrderTerm, ...]
    first: int | None
    after: str | None
    properties: PlanProperties
    # Why the coordinator, not a source, orders (e.g. collation differences).
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResultProject:
    """Remove helper fields and expose only requested public result fields."""

    input: "PhysicalPlan"
    projection: tuple[ResolvedField, ...]
    properties: PlanProperties


PhysicalPlan: TypeAlias = (
    RemoteScan | RecordAssembly | CoordinatorFilter | SemanticVerify | CoordinatorSortPage | ResultProject
)


@dataclass(frozen=True)
class PlanExplanationNode:
    kind: str
    location: str
    fields: tuple[str, ...]
    pushed_filter_fields: tuple[str, ...] = ()
    residual_filter: bool = False
    ordering: tuple[str, ...] = ()
    limit: int | None = None
    key_transfer_max_keys: int | None = None
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanExplanation:
    plan_kind: str
    catalog_fingerprint: str
    plan_fingerprint: str
    nodes: tuple[PlanExplanationNode, ...]


@dataclass(frozen=True)
class PlannedQuery:
    """One executable baseline plan tied to one exact catalog snapshot."""

    query: BoundQuery
    resolved: SourceResolvedQuery
    plan: PhysicalPlan
    catalog_fingerprint: str
    query_fingerprint: str
    plan_fingerprint: str
    explain: PlanExplanation


def remote_location(source_name: str) -> PlanLocation:
    return PlanLocation(PlanLocationKind.REMOTE, source_name)


def coordinator_location() -> PlanLocation:
    return PlanLocation(PlanLocationKind.COORDINATOR)
