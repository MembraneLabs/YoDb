"""Immutable backend-neutral models for YoDb's initial logical query language."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from ..catalog import DatasetSpec, FieldSpec


class ComparisonOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True)
class DatasetReference:
    """The root logical dataset requested by a caller."""

    dataset: str
    scope: str


@dataclass(frozen=True)
class Predicate:
    """One field/operator/value condition before catalog binding."""

    field: str
    operator: ComparisonOperator
    value: object | None
    value_supplied: bool


@dataclass(frozen=True)
class AllExpression:
    expressions: tuple["FilterExpression", ...]


@dataclass(frozen=True)
class AnyExpression:
    expressions: tuple["FilterExpression", ...]


@dataclass(frozen=True)
class NotExpression:
    expression: "FilterExpression"


FilterExpression: TypeAlias = Predicate | AllExpression | AnyExpression | NotExpression


@dataclass(frozen=True)
class OrderTerm:
    field: str
    direction: SortDirection


@dataclass(frozen=True)
class PageRequest:
    first: int | None = None
    after: str | None = None


@dataclass(frozen=True)
class QueryConstraints:
    maximum_results: int | None = None
    maximum_latency_ms: int | None = None
    maximum_cost: float | None = None
    minimum_quality: float | None = None
    allow_partial_results: bool | None = None


@dataclass(frozen=True)
class QueryRequest:
    """A parsed, untrusted query that has not yet been checked against a catalog."""

    root: DatasetReference
    select: tuple[str, ...] | None = None
    where: FilterExpression | None = None
    order_by: tuple[OrderTerm, ...] = ()
    page: PageRequest | None = None
    constraints: QueryConstraints | None = None


@dataclass(frozen=True)
class BoundField:
    """A public logical field resolved in one active catalog snapshot."""

    dataset_name: str
    scope: str
    name: str
    spec: FieldSpec


@dataclass(frozen=True)
class BoundDataset:
    """The root dataset resolved in one active catalog snapshot."""

    name: str
    scope: str
    spec: DatasetSpec


@dataclass(frozen=True)
class BoundPredicate:
    field: BoundField
    operator: ComparisonOperator
    value: object | None
    value_supplied: bool


@dataclass(frozen=True)
class BoundAllExpression:
    expressions: tuple["BoundFilterExpression", ...]


@dataclass(frozen=True)
class BoundAnyExpression:
    expressions: tuple["BoundFilterExpression", ...]


@dataclass(frozen=True)
class BoundNotExpression:
    expression: "BoundFilterExpression"


BoundFilterExpression: TypeAlias = (
    BoundPredicate | BoundAllExpression | BoundAnyExpression | BoundNotExpression
)


@dataclass(frozen=True)
class BoundOrderTerm:
    field: BoundField
    direction: SortDirection


@dataclass(frozen=True)
class BoundQuery:
    """A query safe for planning, bound to one active catalog evaluation."""

    root: BoundDataset
    select: tuple[BoundField, ...]
    where: BoundFilterExpression | None
    order_by: tuple[BoundOrderTerm, ...]
    page: PageRequest
    constraints: QueryConstraints
    query_fingerprint: str
    catalog_fingerprint: str

