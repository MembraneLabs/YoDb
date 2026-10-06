"""The catalog of operators: what exists, what is planned, and how each can run."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum


class OperatorKind(str, Enum):
    # Relational
    SCAN = "scan"
    FILTER = "filter"
    PROJECT = "project"
    ORDER = "order"
    LIMIT = "limit"
    COMBINE = "combine"
    JOIN = "join"
    AGGREGATE = "aggregate"
    GROUP_BY = "group_by"
    DISTINCT = "distinct"
    UNION = "union"
    # Physical support: abilities a source may offer to other operators
    KEY_LOOKUP = "key_lookup"
    VECTOR_SEARCH = "vector_search"
    # Extensions beyond relational algebra
    SEMANTIC_FILTER = "semantic_filter"
    TRAVERSE = "traverse"


class OperatorCategory(str, Enum):
    RELATIONAL = "relational"    # classical relational algebra
    SUPPORT = "support"          # a source ability other operators build on
    EXTENSION = "extension"      # beyond relational algebra


class OperatorStatus(str, Enum):
    IMPLEMENTED = "implemented"  # planned, costed and executed today
    PLANNED = "planned"          # in the catalog so adapters/plans can be designed against it


@dataclass(frozen=True)
class OperatorSpec:
    kind: OperatorKind
    category: OperatorCategory
    status: OperatorStatus
    title: str
    summary: str
    # Named ways the planner may run it (empty for planned operators).
    strategies: tuple[str, ...] = ()
    # Operators whose support this one needs (e.g. a limit is only meaningful with its order).
    requires: tuple[OperatorKind, ...] = ()

    @property
    def implemented(self) -> bool:
        return self.status is OperatorStatus.IMPLEMENTED


_K, _C, _S = OperatorKind, OperatorCategory, OperatorStatus

_SPECS: tuple[OperatorSpec, ...] = (
    OperatorSpec(_K.SCAN, _C.RELATIONAL, _S.IMPLEMENTED, "Scan", "Read rows of one source representation.", ("plain",)),
    OperatorSpec(
        _K.FILTER, _C.RELATIONAL, _S.IMPLEMENTED, "Filter",
        "Keep rows satisfying a Boolean condition.", ("push_to_source", "run_in_yodb"),
    ),
    OperatorSpec(
        _K.PROJECT, _C.RELATIONAL, _S.IMPLEMENTED, "Project",
        "Return only the requested fields (sources read only the columns needed).", ("push_columns",),
    ),
    OperatorSpec(_K.ORDER, _C.RELATIONAL, _S.IMPLEMENTED, "Order", "Sort rows deterministically.", ("push_to_source", "run_in_yodb")),
    OperatorSpec(
        _K.LIMIT, _C.RELATIONAL, _S.IMPLEMENTED, "Limit", "Keep the first N rows of the order.",
        ("push_to_source", "run_in_yodb"), requires=(_K.ORDER,),
    ),
    OperatorSpec(
        _K.COMBINE, _C.RELATIONAL, _S.IMPLEMENTED, "Combine sources",
        "Assemble one logical record from several sources by logical ID: the identity source's rows "
        "enriched by contributors, with filtered contributors acting as an inner restriction.",
        ("read_order", "restrict_by_ids"), requires=(_K.SCAN,),
    ),
    OperatorSpec(
        _K.JOIN, _C.RELATIONAL, _S.IMPLEMENTED, "Join",
        "Join two datasets over a declared relationship (not a same-dataset ID combine).",
        ("driver_left", "driver_right"), requires=(_K.SCAN,),
    ),
    OperatorSpec(
        _K.AGGREGATE, _C.RELATIONAL, _S.PLANNED, "Aggregate",
        "COUNT, SUM, AVG, MIN, MAX over rows; a source may compute them or YoDb may.",
    ),
    OperatorSpec(_K.GROUP_BY, _C.RELATIONAL, _S.PLANNED, "Group by", "Partition rows and aggregate each group.", requires=(_K.AGGREGATE,)),
    OperatorSpec(_K.DISTINCT, _C.RELATIONAL, _S.PLANNED, "Distinct", "Remove duplicate rows."),
    OperatorSpec(_K.UNION, _C.RELATIONAL, _S.PLANNED, "Union", "Combine the rows of several queries (and intersect / except)."),
    OperatorSpec(
        _K.KEY_LOOKUP, _C.SUPPORT, _S.IMPLEMENTED, "Key lookup",
        "Restrict a read to a set of logical IDs learned elsewhere (a semi-join).", ("in_list",),
    ),
    OperatorSpec(
        _K.VECTOR_SEARCH, _C.SUPPORT, _S.IMPLEMENTED, "Vector search",
        "Return the K rows nearest to a query vector, under the read's other filters.", ("ranked_read",),
    ),
    OperatorSpec(
        _K.SEMANTIC_FILTER, _C.EXTENSION, _S.IMPLEMENTED, "Semantic filter",
        "Keep records for which a natural-language proposition is true of their text.",
        ("verify_all", "vector_shortlist"),
    ),
    OperatorSpec(
        _K.TRAVERSE, _C.EXTENSION, _S.PLANNED, "Traverse",
        "Follow a declared relationship across a bounded number of hops.",
    ),
)


class OperatorCatalog:
    """Read-only lookup over the operator specs."""

    def __init__(self, specs: Iterable[OperatorSpec]) -> None:
        self._specs = {spec.kind: spec for spec in specs}
        missing = set(OperatorKind) - set(self._specs)
        if missing:
            raise ValueError(f"operators without a spec: {sorted(kind.value for kind in missing)}")
        for spec in self._specs.values():
            unknown = [r for r in spec.requires if r not in self._specs]
            if unknown or spec.kind in spec.requires:
                raise ValueError(f"operator '{spec.kind.value}' has an invalid requirement")
            if spec.implemented and not spec.strategies:
                raise ValueError(f"implemented operator '{spec.kind.value}' must name at least one strategy")
            if not spec.implemented and spec.strategies:
                raise ValueError(f"planned operator '{spec.kind.value}' must not claim strategies yet")

    def get(self, kind: OperatorKind) -> OperatorSpec:
        return self._specs[kind]

    def __iter__(self) -> Iterator[OperatorSpec]:
        return iter(self._specs.values())

    def implemented(self) -> tuple[OperatorSpec, ...]:
        return tuple(spec for spec in self if spec.implemented)

    def planned(self) -> tuple[OperatorSpec, ...]:
        return tuple(spec for spec in self if not spec.implemented)

    def in_category(self, category: OperatorCategory) -> tuple[OperatorSpec, ...]:
        return tuple(spec for spec in self if spec.category is category)


OPERATORS = OperatorCatalog(_SPECS)
