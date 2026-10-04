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

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ..catalog import LogicalType, SourceKind, VectorMetric
from ..operators import OPERATORS, OperatorKind
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


class SupportLevel(str, Enum):
    SOURCE = "source"            # the database runs this operator itself
    COORDINATOR = "coordinator"  # YoDb runs it (possibly using abilities the source offers)
    UNAVAILABLE = "unavailable"  # nothing can run it yet


@dataclass(frozen=True)
class OperatorSupport:
    """How one operator can run against a source, and the strategies that makes legal."""

    kind: OperatorKind
    level: SupportLevel
    strategies: tuple[str, ...]
    detail: str


SupportView = Callable[[SourceCapabilities], OperatorSupport]
_VIEWS: dict[OperatorKind, SupportView] = {}


def support_view(kind: OperatorKind) -> Callable[[SupportView], SupportView]:
    """Register how a source's declaration maps onto one operator.

    The operator owns its own view (next to its planning code), so adding an
    operator never means editing this module.
    """

    def register(view: SupportView) -> SupportView:
        _VIEWS[kind] = view
        return view

    return register


def operator_support(capabilities: SourceCapabilities) -> tuple[OperatorSupport, ...]:
    """What a source's declaration means for every operator in the catalog.

    Derived from the declaration only, so it cannot drift from what the planner
    does: the planner reads the same fields.
    """

    result: list[OperatorSupport] = []
    for spec in OPERATORS:
        view = _VIEWS.get(spec.kind)
        result.append(
            view(capabilities)
            if view is not None
            else OperatorSupport(spec.kind, SupportLevel.UNAVAILABLE, (), "not implemented yet")
        )
    return tuple(result)


def _names(items) -> str:
    return ", ".join(sorted(item.value for item in items))


def _source(kind, strategies, detail):
    return OperatorSupport(kind, SupportLevel.SOURCE, strategies, detail)


def _coordinator(kind, strategies, detail):
    return OperatorSupport(kind, SupportLevel.COORDINATOR, strategies, detail)


def _unavailable(kind, detail):
    return OperatorSupport(kind, SupportLevel.UNAVAILABLE, (), detail)


@support_view(OperatorKind.SCAN)
def _scan_view(caps: SourceCapabilities) -> OperatorSupport:
    return _source(OperatorKind.SCAN, ("plain",), "always")


@support_view(OperatorKind.FILTER)
def _filter_view(caps: SourceCapabilities) -> OperatorSupport:
    if not caps.filter_operators:
        return _coordinator(OperatorKind.FILTER, ("run_in_yodb",), "the source declares no filter operators")
    detail = (
        f"operators: {_names(caps.filter_operators)}; combinators: {_names(caps.boolean_operators) or 'none'}; "
        f"types: {_names(caps.filterable_types)}"
    )
    return _source(OperatorKind.FILTER, ("push_to_source", "run_in_yodb"), detail)


@support_view(OperatorKind.PROJECT)
def _project_view(caps: SourceCapabilities) -> OperatorSupport:
    return _source(OperatorKind.PROJECT, ("push_columns",), "reads only the needed columns")


@support_view(OperatorKind.ORDER)
def _order_view(caps: SourceCapabilities) -> OperatorSupport:
    if caps.supports_order:
        return _source(OperatorKind.ORDER, ("push_to_source", "run_in_yodb"), f"text ordering: {caps.text_ordering.value}")
    return _coordinator(OperatorKind.ORDER, ("run_in_yodb",), "the source cannot order")


@support_view(OperatorKind.LIMIT)
def _limit_view(caps: SourceCapabilities) -> OperatorSupport:
    if caps.supports_limit:
        return _source(OperatorKind.LIMIT, ("push_to_source", "run_in_yodb"), "with the order that defines it")
    return _coordinator(OperatorKind.LIMIT, ("run_in_yodb",), "the source cannot limit")


@support_view(OperatorKind.KEY_LOOKUP)
def _key_lookup_view(caps: SourceCapabilities) -> OperatorSupport:
    if caps.key_lookup is None:
        return _unavailable(OperatorKind.KEY_LOOKUP, "reads are never restricted by IDs")
    return _source(OperatorKind.KEY_LOOKUP, ("in_list",), f"up to {caps.key_lookup.maximum_keys} IDs per read")


@support_view(OperatorKind.VECTOR_SEARCH)
def _vector_search_view(caps: SourceCapabilities) -> OperatorSupport:
    v = caps.vector_search
    if v is None:
        return _unavailable(OperatorKind.VECTOR_SEARCH, "no vector search")
    detail = f"metrics: {_names(v.metrics)}; with filters: {'yes' if v.combines_with_filters else 'no'}"
    if v.maximum_shortlist:
        detail += f"; shortlist up to {v.maximum_shortlist}"
    return _source(OperatorKind.VECTOR_SEARCH, ("ranked_read",), detail)


@support_view(OperatorKind.COMBINE)
def _combine_view(caps: SourceCapabilities) -> OperatorSupport:
    strategies = ("read_order", "restrict_by_ids") if caps.key_lookup is not None else ("read_order",)
    return _coordinator(OperatorKind.COMBINE, strategies, "YoDb assembles records across sources")


def describe_capabilities(capabilities: SourceCapabilities) -> str:
    """A readable summary of what a source can do, one operator per line."""

    lines = [f"{capabilities.source_kind.value}"]
    for item in operator_support(capabilities):
        strategies = ", ".join(item.strategies) if item.strategies else "-"
        lines.append(f"  {item.kind.value:<16} {item.level.value:<12} strategies: {strategies:<34} {item.detail}")
    return "\n".join(lines)
