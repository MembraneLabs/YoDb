"""Physical, backend-neutral plans produced from one resolved YoDb query."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from hashlib import sha256
import json
from typing import ClassVar, Protocol, TypeAlias, runtime_checkable

from ..catalog import SourceKind, VectorMetric
from ..operators import OperatorKind
from ..query.models import BoundFilterExpression, BoundOrderTerm, BoundQuery
from ..query.resolution import ResolvedField, SingleSourceQueryBinding, SourceResolvedQuery
from .capabilities import SourceCapabilities
from .expressions import filter_fields, filter_shape


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
    """Vector-search capability of a source, separate from source-native compilation."""

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


class PhysicalNode:
    """Base of every physical plan node.

    A node names the operator it implements and can describe itself, so
    explaining, fingerprinting and rewriting a plan never need to know the
    concrete node types.  A new operator adds a node class; nothing else here
    changes.
    """

    operator: ClassVar[OperatorKind]

    def inputs(self) -> tuple["PhysicalNode", ...]:
        """The nodes whose output this node consumes (leaves have none)."""

        return ()

    def with_inputs(self, inputs: tuple["PhysicalNode", ...]) -> "PhysicalNode":
        """A copy reading from ``inputs`` instead (same count as :meth:`inputs`)."""

        if inputs:
            raise ValueError(f"{type(self).__name__} has no inputs")
        return self

    def shape(self) -> dict[str, object]:
        """A value-free structural description of this node and its inputs."""

        raise NotImplementedError

    def describe(self) -> "PlanExplanationNode":
        """This node's own entry in a plan explanation (without its inputs)."""

        raise NotImplementedError


class UnaryNode(PhysicalNode):
    """A node with exactly one input, held in ``self.input``."""

    def inputs(self) -> tuple[PhysicalNode, ...]:
        return (self.input,)  # type: ignore[attr-defined]

    def with_inputs(self, inputs: tuple[PhysicalNode, ...]) -> PhysicalNode:
        (only,) = inputs
        return replace(self, input=only)  # type: ignore[type-var]


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
    # Row guard for the plain scan the executor falls back to when the ranked
    # search would be unsafe (see ``RecordAssembly.schedule``).
    fallback_maximum_rows: int | None = None


@dataclass(frozen=True)
class RemoteScan(PhysicalNode):
    """One validated, compilable source-local scan fragment."""

    operator: ClassVar[OperatorKind] = OperatorKind.SCAN

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

    def shape(self) -> dict[str, object]:
        return {
            "kind": "remote_scan",
            "source": self.source.source_name,
            "fields": [field.field.name for field in self.projection],
            "filter": filter_shape(self.pushed_filter),
            "order": [(term.field.name, term.direction.value) for term in self.order_by],
            "limit": self.limit,
            "maximum_rows": self.maximum_rows,
            "vector": None if self.vector_search is None else [self.vector_search.metric.value, self.vector_search.shortlist_size],
        }

    def describe(self) -> "PlanExplanationNode":
        return PlanExplanationNode(
            kind="remote_scan",
            location=self.source.source_name,
            fields=tuple(field.field.name for field in self.projection),
            pushed_filter_fields=filter_fields(self.pushed_filter),
            ordering=tuple(f"{term.field.name} {term.direction.value}" for term in self.order_by),
            limit=self.limit,
            detail=()
            if self.vector_search is None
            else (f"vector_search={self.vector_search.metric.value} top {self.vector_search.shortlist_size}",),
        )


class StepRole(str, Enum):
    ANCHOR = "anchor"        # identity source: its rows are the result records
    REQUIRED = "required"    # contributor with a pushed filter: an inner-join restriction
    OPTIONAL = "optional"    # contributor that only enriches
    SHORTLIST = "shortlist"  # vector store: ranks IDs, returns the nearest K; carries no fields


@dataclass(frozen=True)
class AssemblyStep:
    """Read one source; ``restrict`` asks for the IDs learned so far to narrow it.

    The executor still checks at run time that the learned ID set fits the
    source's lookup limit and falls back to a plain (guarded) scan when it
    does not, so a plan is never wrong because an estimate was.
    """

    source_name: str
    role: StepRole
    restrict: bool = True


def default_schedule(
    anchor: "RemoteScan", contributors: tuple["RemoteScan", ...], required: tuple[str, ...]
) -> tuple[AssemblyStep, ...]:
    """The fixed rule: required contributors, the vector shortlist (if any), the anchor, then enrichers."""

    ranked = tuple(c for c in contributors if c.vector_search is not None)
    plain = tuple(c for c in contributors if c.vector_search is None)
    required_steps = tuple(
        AssemblyStep(c.source.source_name, StepRole.REQUIRED) for c in plain if c.source.source_name in required
    )
    shortlist_steps = tuple(AssemblyStep(c.source.source_name, StepRole.SHORTLIST) for c in ranked)
    optional_steps = tuple(
        AssemblyStep(c.source.source_name, StepRole.OPTIONAL) for c in plain if c.source.source_name not in required
    )
    return (*required_steps, *shortlist_steps, AssemblyStep(anchor.source.source_name, StepRole.ANCHOR), *optional_steps)


@dataclass(frozen=True)
class RecordAssembly(PhysicalNode):
    """Left-enrich root records with contributor fields using logical identity."""

    operator: ClassVar[OperatorKind] = OperatorKind.COMBINE

    anchor: RemoteScan
    contributors: tuple[RemoteScan, ...]
    required_contributor_matches: tuple[str, ...]
    properties: PlanProperties
    # Largest logical-ID set the executor may transfer between sources to
    # restrict a later scan; ``None`` disables transfer.
    maximum_transfer_keys: int | None = None
    # How many batches of ``maximum_transfer_keys`` an ID set may be sent in (1: never batched).
    maximum_key_batches: int = 1
    # The order sources are read in and which reads are restricted by learned
    # IDs.  Always complete: one step per source.
    schedule: tuple[AssemblyStep, ...] = ()

    def inputs(self) -> tuple[PhysicalNode, ...]:
        return (self.anchor, *self.contributors)

    def with_inputs(self, inputs: tuple[PhysicalNode, ...]) -> PhysicalNode:
        anchor, *contributors = inputs
        return replace(self, anchor=anchor, contributors=tuple(contributors))

    def shape(self) -> dict[str, object]:
        return {
            "kind": "record_assembly",
            "anchor": self.anchor.shape(),
            "contributors": [item.shape() for item in self.contributors],
            "required_contributor_matches": list(self.required_contributor_matches),
            "maximum_transfer_keys": self.maximum_transfer_keys,
            "maximum_key_batches": self.maximum_key_batches,
            "schedule": [[step.source_name, step.role.value, step.restrict] for step in self.schedule],
        }

    def describe(self) -> "PlanExplanationNode":
        order = " -> ".join(f"{step.role.value}:{step.source_name}{'*' if step.restrict else ''}" for step in self.schedule)
        return PlanExplanationNode(
            "record_assembly",
            "coordinator",
            tuple(field.field.name for field in self.properties.output_fields),
            key_transfer_max_keys=self.maximum_transfer_keys,
            detail=(f"schedule: {order}",),
        )


@dataclass(frozen=True)
class CoordinatorFilter(UnaryNode):
    """Defensive complete Boolean evaluation after source work/assembly."""

    operator: ClassVar[OperatorKind] = OperatorKind.FILTER

    input: "PhysicalPlan"
    expression: BoundFilterExpression | None
    properties: PlanProperties

    def shape(self) -> dict[str, object]:
        return {"kind": "coordinator_filter", "input": self.input.shape(), "filter": filter_shape(self.expression)}

    def describe(self) -> "PlanExplanationNode":
        return PlanExplanationNode(
            "coordinator_filter",
            "coordinator",
            tuple(field.field.name for field in self.properties.output_fields),
            residual_filter=self.expression is not None,
        )


@dataclass(frozen=True)
class CoordinatorSortPage(UnaryNode):
    """Global deterministic ordering and page placement at the coordinator (the order and limit operators)."""

    operator: ClassVar[OperatorKind] = OperatorKind.ORDER

    input: "PhysicalPlan"
    order_by: tuple[BoundOrderTerm, ...]
    first: int | None
    after: str | None
    properties: PlanProperties
    # Why the coordinator, not a source, orders (e.g. collation differences).
    notes: tuple[str, ...] = ()

    def shape(self) -> dict[str, object]:
        return {
            "kind": "coordinator_sort_page",
            "input": self.input.shape(),
            "order": [(term.field.name, term.direction.value) for term in self.order_by],
            "first": self.first,
        }

    def describe(self) -> "PlanExplanationNode":
        return PlanExplanationNode(
            "coordinator_sort_page",
            "coordinator",
            tuple(field.field.name for field in self.properties.output_fields),
            ordering=tuple(f"{term.field.name} {term.direction.value}" for term in self.order_by),
            limit=self.first,
            detail=tuple(f"note: {note}" for note in self.notes),
        )


@dataclass(frozen=True)
class ResultProject(UnaryNode):
    """Remove helper fields and expose only requested public result fields."""

    operator: ClassVar[OperatorKind] = OperatorKind.PROJECT

    input: "PhysicalPlan"
    projection: tuple[ResolvedField, ...]
    properties: PlanProperties

    def shape(self) -> dict[str, object]:
        return {"kind": "result_project", "input": self.input.shape(), "fields": [field.field.name for field in self.projection]}

    def describe(self) -> "PlanExplanationNode":
        return PlanExplanationNode(
            "result_project", "coordinator", tuple(field.field.name for field in self.projection)
        )


PhysicalPlan: TypeAlias = PhysicalNode


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
    # How the plan was chosen: cost-based with its estimates, or the fixed rules and why.
    optimizer: tuple[str, ...] = ()


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


def explain_plan(plan: PhysicalNode) -> tuple["PlanExplanationNode", ...]:
    """Every node's description, inputs before the node that consumes them."""

    return (*(entry for child in plan.inputs() for entry in explain_plan(child)), plan.describe())


def plan_fingerprint(plan: PhysicalNode) -> str:
    """A digest of the plan's structure (never of query values or secrets)."""

    encoded = json.dumps(plan.shape(), sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def transform_plan(plan: PhysicalNode, rewrite: Callable[[PhysicalNode], PhysicalNode]) -> PhysicalNode:
    """Rebuild the plan bottom-up, applying ``rewrite`` to every node."""

    inputs = tuple(transform_plan(child, rewrite) for child in plan.inputs())
    return rewrite(plan.with_inputs(inputs) if inputs else plan)


def properties_from(properties: "PlanProperties", **changes: object) -> "PlanProperties":
    """Copy plan properties with some fields changed."""

    return replace(properties, **changes)
