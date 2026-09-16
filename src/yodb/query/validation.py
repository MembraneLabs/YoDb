"""Bind parsed logical queries to one activated, immutable YoDb catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import math
from typing import Any
from uuid import UUID

from ..catalog import LogicalType, Visibility
from ..errors import ErrorCode, ErrorDetail, QueryError
from ..runtime.contracts import CatalogEvaluation
from ..runtime.service import InMemoryCatalogRuntime
from .fingerprint import catalog_fingerprint, query_fingerprint
from .models import (
    AllExpression,
    AnyExpression,
    BoundAllExpression,
    BoundAnyExpression,
    BoundDataset,
    BoundField,
    BoundFilterExpression,
    BoundNotExpression,
    BoundOrderTerm,
    BoundPredicate,
    BoundQuery,
    ComparisonOperator,
    FilterExpression,
    NotExpression,
    OrderTerm,
    PageRequest,
    Predicate,
    QueryConstraints,
    QueryRequest,
    SortDirection,
)


_EQUALITY_OPERATORS = frozenset(
    {
        ComparisonOperator.EQ,
        ComparisonOperator.NE,
        ComparisonOperator.IN,
        ComparisonOperator.NOT_IN,
        ComparisonOperator.IS_NULL,
        ComparisonOperator.IS_NOT_NULL,
    }
)
_RANGE_OPERATORS = frozenset(
    {ComparisonOperator.GT, ComparisonOperator.GTE, ComparisonOperator.LT, ComparisonOperator.LTE}
)
_TEXT_OPERATORS = frozenset({ComparisonOperator.CONTAINS, ComparisonOperator.STARTS_WITH})
_EQUALITY_TYPES = frozenset(
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


@dataclass(frozen=True)
class QueryValidationPolicy:
    """Deployment-local bounds for the backend-neutral query validator."""

    default_page_size: int = 100
    maximum_page_size: int = 500
    maximum_in_values: int = 1_000

    def __post_init__(self) -> None:
        if self.default_page_size <= 0:
            raise ValueError("default_page_size must be positive")
        if self.maximum_page_size < self.default_page_size:
            raise ValueError("maximum_page_size must be at least default_page_size")
        if self.maximum_in_values <= 0:
            raise ValueError("maximum_in_values must be positive")


def validate_query(
    request: QueryRequest,
    runtime: InMemoryCatalogRuntime,
    *,
    policy: QueryValidationPolicy = QueryValidationPolicy(),
) -> BoundQuery:
    """Bind a query to the runtime's active catalog or raise a structured error."""

    return bind_query(request, runtime.require_active(), policy=policy)


def bind_query(
    request: QueryRequest,
    active: CatalogEvaluation,
    *,
    policy: QueryValidationPolicy = QueryValidationPolicy(),
) -> BoundQuery:
    """Validate a parsed request against exactly one active catalog evaluation."""

    catalog = active.catalog
    dataset = catalog.datasets.get(request.root.dataset)
    if dataset is None:
        _fail(ErrorCode.DATASET_NOT_FOUND, f"Unknown dataset '{request.root.dataset}'.", "from.dataset")
    root = BoundDataset(name=request.root.dataset, scope=request.root.scope, spec=dataset)

    selected_names = (
        request.select
        if request.select is not None
        else tuple(
            field_name
            for field_name, field in dataset.fields.items()
            if field.visibility is Visibility.PUBLIC
        )
    )
    selected_fields = tuple(
        _bind_public_field(root, field_name, f"select[{index}]")
        for index, field_name in enumerate(selected_names)
    )
    if "id" not in selected_names:
        selected_fields = (_bind_public_field(root, "id", "select"), *selected_fields)

    bound_filter = (
        _bind_expression(request.where, root, "where", policy) if request.where is not None else None
    )
    bound_order = _bind_order(request.order_by, root)
    effective_order = _with_id_tiebreaker(bound_order, root)
    page = _validate_page(request.page, policy)
    constraints = request.constraints or QueryConstraints()
    _validate_constraints(constraints, policy)

    semantic_payload = {
        "query_language_version": "yodb/v0.1-query-core",
        "from": {"dataset": root.name, "scope": root.scope},
        "select": sorted(field.name for field in selected_fields),
        "where": _expression_payload(bound_filter),
        "order_by": [
            {"field": term.field.name, "direction": term.direction.value} for term in effective_order
        ],
        "constraints": _semantic_constraints_payload(constraints),
    }
    return BoundQuery(
        root=root,
        select=selected_fields,
        where=bound_filter,
        order_by=effective_order,
        page=page,
        constraints=constraints,
        query_fingerprint=query_fingerprint(semantic_payload),
        catalog_fingerprint=catalog_fingerprint(catalog),
    )


def _bind_expression(
    expression: FilterExpression,
    root: BoundDataset,
    location: str,
    policy: QueryValidationPolicy,
) -> BoundFilterExpression:
    if isinstance(expression, Predicate):
        field = _bind_public_field(root, expression.field, f"{location}.field")
        _validate_operator(field, expression, location)
        normalized_value = _normalize_predicate_value(field, expression, location, policy)
        return BoundPredicate(field, expression.operator, normalized_value, expression.value_supplied)
    if isinstance(expression, AllExpression):
        return BoundAllExpression(
            tuple(
                _bind_expression(item, root, f"{location}.all[{index}]", policy)
                for index, item in enumerate(expression.expressions)
            )
        )
    if isinstance(expression, AnyExpression):
        return BoundAnyExpression(
            tuple(
                _bind_expression(item, root, f"{location}.any[{index}]", policy)
                for index, item in enumerate(expression.expressions)
            )
        )
    if isinstance(expression, NotExpression):
        return BoundNotExpression(_bind_expression(expression.expression, root, f"{location}.not", policy))
    raise AssertionError(f"Unknown expression: {expression!r}")


def _bind_order(terms: tuple[OrderTerm, ...], root: BoundDataset) -> tuple[BoundOrderTerm, ...]:
    bound: list[BoundOrderTerm] = []
    for index, term in enumerate(terms):
        field = _bind_public_field(root, term.field, f"order_by[{index}].field")
        if field.spec.type in {LogicalType.JSON, LogicalType.BYTES}:
            _fail(
                ErrorCode.QUERY_OPERATOR_NOT_SUPPORTED,
                f"Field '{field.name}' cannot be ordered with its logical type.",
                f"order_by[{index}].field",
            )
        bound.append(BoundOrderTerm(field, term.direction))
    return tuple(bound)


def _with_id_tiebreaker(
    terms: tuple[BoundOrderTerm, ...], root: BoundDataset) -> tuple[BoundOrderTerm, ...]:
    if terms and terms[-1].field.name == "id":
        return terms
    return (*terms, BoundOrderTerm(_bind_public_field(root, "id", "order_by"), SortDirection.ASC))


def _bind_public_field(root: BoundDataset, field_name: str, location: str) -> BoundField:
    field = root.spec.fields.get(field_name)
    if field is None:
        _fail(ErrorCode.FIELD_NOT_FOUND, f"Unknown field '{field_name}' on dataset '{root.name}'.", location)
    if field.visibility is not Visibility.PUBLIC:
        _fail(
            ErrorCode.FIELD_NOT_ACCESSIBLE,
            f"Field '{field_name}' on dataset '{root.name}' is not publicly queryable.",
            location,
        )
    return BoundField(dataset_name=root.name, scope=root.scope, name=field_name, spec=field)


def _validate_operator(field: BoundField, predicate: Predicate, location: str) -> None:
    operator = predicate.operator
    field_type = field.spec.type
    if operator in _EQUALITY_OPERATORS and field_type not in _EQUALITY_TYPES:
        _unsupported_operator(field, operator, location)
    if operator in _RANGE_OPERATORS and field_type not in {
        LogicalType.INT,
        LogicalType.FLOAT,
        LogicalType.TIMESTAMP,
    }:
        _unsupported_operator(field, operator, location)
    if operator in _TEXT_OPERATORS and field_type not in {LogicalType.STRING, LogicalType.TEXT}:
        _unsupported_operator(field, operator, location)
    if operator in {ComparisonOperator.IS_NULL, ComparisonOperator.IS_NOT_NULL}:
        if predicate.value_supplied:
            _fail(
                ErrorCode.QUERY_VALUE_TYPE_INVALID,
                f"Operator '{operator.value}' does not take a value.",
                f"{location}.value",
            )
    elif not predicate.value_supplied or predicate.value is None:
        _fail(
            ErrorCode.QUERY_VALUE_TYPE_INVALID,
            f"Operator '{operator.value}' requires a non-null value.",
            f"{location}.value",
        )


def _unsupported_operator(field: BoundField, operator: ComparisonOperator, location: str) -> None:
    _fail(
        ErrorCode.QUERY_OPERATOR_NOT_SUPPORTED,
        f"Operator '{operator.value}' is not supported for field '{field.name}' of type '{field.spec.type.value}'.",
        f"{location}.op",
    )


def _normalize_predicate_value(
    field: BoundField,
    predicate: Predicate,
    location: str,
    policy: QueryValidationPolicy,
) -> object | None:
    if predicate.operator in {ComparisonOperator.IS_NULL, ComparisonOperator.IS_NOT_NULL}:
        return None
    if predicate.operator in {ComparisonOperator.IN, ComparisonOperator.NOT_IN}:
        if not isinstance(predicate.value, list) or not predicate.value:
            _fail(
                ErrorCode.QUERY_VALUE_TYPE_INVALID,
                f"Operator '{predicate.operator.value}' requires a non-empty array.",
                f"{location}.value",
            )
        if len(predicate.value) > policy.maximum_in_values:
            _fail(ErrorCode.QUERY_LIMIT_INVALID, "Filter list exceeds the configured maximum.", f"{location}.value")
        return tuple(_normalize_scalar(field, item, f"{location}.value[{index}]") for index, item in enumerate(predicate.value))
    return _normalize_scalar(field, predicate.value, f"{location}.value")


def _normalize_scalar(field: BoundField, value: Any, location: str) -> object:
    logical_type = field.spec.type
    if logical_type in {LogicalType.ID, LogicalType.STRING, LogicalType.TEXT}:
        if not isinstance(value, str) or (logical_type is LogicalType.ID and not value.strip()):
            _type_error(field, location)
        return value
    if logical_type is LogicalType.UUID:
        if not isinstance(value, str):
            _type_error(field, location)
        try:
            return str(UUID(value))
        except ValueError:
            _type_error(field, location)
    if logical_type is LogicalType.INT:
        if type(value) is not int:
            _type_error(field, location)
        return value
    if logical_type is LogicalType.FLOAT:
        if type(value) not in {int, float} or isinstance(value, bool) or not math.isfinite(value):
            _type_error(field, location)
        return float(value)
    if logical_type is LogicalType.BOOL:
        if type(value) is not bool:
            _type_error(field, location)
        return value
    if logical_type is LogicalType.TIMESTAMP:
        if not isinstance(value, str):
            _type_error(field, location)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            _type_error(field, location)
        if parsed.tzinfo is None:
            _type_error(field, location)
        return parsed.astimezone(UTC)
    _type_error(field, location)


def _type_error(field: BoundField, location: str) -> None:
    _fail(
        ErrorCode.QUERY_VALUE_TYPE_INVALID,
        f"Expected a value compatible with logical type '{field.spec.type.value}'.",
        location,
    )


def _validate_page(page: PageRequest | None, policy: QueryValidationPolicy) -> PageRequest:
    resolved = page or PageRequest()
    first = resolved.first if resolved.first is not None else policy.default_page_size
    if first > policy.maximum_page_size:
        _fail(
            ErrorCode.QUERY_LIMIT_INVALID,
            f"'page.first' exceeds the maximum page size of {policy.maximum_page_size}.",
            "page.first",
        )
    return PageRequest(first=first, after=resolved.after)


def _validate_constraints(constraints: QueryConstraints, policy: QueryValidationPolicy) -> None:
    if constraints.maximum_results is not None and constraints.maximum_results > policy.maximum_page_size:
        _fail(
            ErrorCode.QUERY_LIMIT_INVALID,
            f"'constraints.maximum_results' exceeds the maximum of {policy.maximum_page_size}.",
            "constraints.maximum_results",
        )
    if constraints.minimum_quality is not None and constraints.minimum_quality > 1:
        _fail(
            ErrorCode.QUERY_LIMIT_INVALID,
            "'constraints.minimum_quality' must not exceed 1.",
            "constraints.minimum_quality",
        )


def _expression_payload(expression: BoundFilterExpression | None) -> Any:
    if expression is None:
        return None
    if isinstance(expression, BoundPredicate):
        return {
            "field": expression.field.name,
            "op": expression.operator.value,
            "value": expression.value,
            "value_supplied": expression.value_supplied,
        }
    if isinstance(expression, BoundAllExpression):
        return {"all": sorted((_expression_payload(item) for item in expression.expressions), key=_canonical_sort_key)}
    if isinstance(expression, BoundAnyExpression):
        return {"any": sorted((_expression_payload(item) for item in expression.expressions), key=_canonical_sort_key)}
    if isinstance(expression, BoundNotExpression):
        return {"not": _expression_payload(expression.expression)}
    raise AssertionError(f"Unknown bound expression: {expression!r}")


def _semantic_constraints_payload(constraints: QueryConstraints) -> dict[str, object]:
    """Include only constraints that can alter a query's logical result meaning."""

    return {
        "maximum_results": constraints.maximum_results,
        "minimum_quality": constraints.minimum_quality,
        "allow_partial_results": constraints.allow_partial_results,
    }


def _canonical_sort_key(value: Any) -> str:
    import json

    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _fail(code: ErrorCode, message: str, location: str | None = None) -> None:
    raise QueryError(ErrorDetail(code=code, message=message, retryable=False, location=location))
