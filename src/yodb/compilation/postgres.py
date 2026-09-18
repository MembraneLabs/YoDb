"""Compile one resolved single-source YoDb query into parameterized PostgreSQL SQL."""

from __future__ import annotations

from dataclasses import dataclass

from ..catalog import SourceKind
from ..errors import ErrorCode, ErrorDetail, QueryError
from ..query.models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    ComparisonOperator,
)
from ..planning.contracts import SourceScanPlan
from ..query.resolution import (
    QuerySourceShape,
    ResolvedField,
    SingleSourceQueryBinding,
    SourceResolvedQuery,
)
from .contracts import CompiledOutputColumn, CompiledPostgresQuery


_COMPILED_OPERATORS = frozenset(
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
)


@dataclass(frozen=True)
class _CompileContext:
    source: SingleSourceQueryBinding
    fields_by_name: dict[str, ResolvedField]


class PostgresQueryCompiler:
    """Compile the first source-local relational slice of YoDb queries.

    This compiler intentionally has no database connection. The subsequent
    executor will lease ``connection_ref`` from the PostgreSQL connection
    adapter and submit the compiled statement with its parameter tuple.
    """

    source_kind = SourceKind.POSTGRES

    def compile(self, query: SourceScanPlan | SourceResolvedQuery) -> CompiledPostgresQuery:
        """Compile a single-source PostgreSQL query or raise a structured error."""

        scan = _legacy_scan(query) if isinstance(query, SourceResolvedQuery) else query
        source = scan.source
        if isinstance(query, SourceResolvedQuery) and query.shape is not QuerySourceShape.SINGLE_SOURCE:
            _fail(
                ErrorCode.QUERY_SOURCE_SHAPE_UNSUPPORTED,
                "The PostgreSQL compiler currently accepts single-source queries only.",
            )
        if source.source_kind is not SourceKind.POSTGRES:
            _fail(
                ErrorCode.QUERY_COMPILATION_UNSUPPORTED,
                "The PostgreSQL compiler requires a PostgreSQL source binding.",
            )
        if isinstance(query, SourceResolvedQuery) and query.query.page.after is not None:
            _fail(
                ErrorCode.QUERY_COMPILATION_UNSUPPORTED,
                "Cursor pagination is not compiled until signed cursor verification is implemented.",
                "page.after",
            )

        context = _CompileContext(source=source, fields_by_name=_field_map(source))
        selected = scan.projection
        projections = ", ".join(
            f"{_column(field)} AS {_quote_identifier(field.field.name)}" for field in selected
        )
        parameters: list[object] = []
        where = _compile_expression(scan.where, context, parameters)
        order_by = ", ".join(
            f"{_column(_require_resolved_field(context, term.field.name, 'order_by'))} "
            f"{term.direction.value.upper()}"
            for term in scan.order_by
        )

        statements = [f"SELECT {projections}", f"FROM {_resource(source.resource)}"]
        if where is not None:
            statements.append(f"WHERE {where}")
        if order_by:
            statements.append(f"ORDER BY {order_by}")
        if scan.limit is not None:
            parameters.append(scan.limit)
            statements.append("LIMIT %s")
        return CompiledPostgresQuery(
            source_name=source.source_name,
            connection_ref=source.connection_ref,
            sql="\n".join(statements),
            parameters=tuple(parameters),
            output_columns=tuple(
                CompiledOutputColumn(sql_alias=field.field.name, logical_field=field.field.name)
                for field in selected
            ),
        )


def _compile_expression(
    expression: BoundFilterExpression | None,
    context: _CompileContext,
    parameters: list[object],
) -> str | None:
    if expression is None:
        return None
    if isinstance(expression, BoundPredicate):
        return _compile_predicate(expression, context, parameters)
    if isinstance(expression, BoundAllExpression):
        return "(" + " AND ".join(
            _compile_expression(child, context, parameters) for child in expression.expressions
        ) + ")"
    if isinstance(expression, BoundAnyExpression):
        return "(" + " OR ".join(
            _compile_expression(child, context, parameters) for child in expression.expressions
        ) + ")"
    if isinstance(expression, BoundNotExpression):
        child = _compile_expression(expression.expression, context, parameters)
        assert child is not None
        return f"(NOT {child})"
    raise AssertionError(f"Unknown bound expression: {expression!r}")


def _compile_predicate(
    predicate: BoundPredicate,
    context: _CompileContext,
    parameters: list[object],
) -> str:
    field = _require_resolved_field(context, predicate.field.name, "where")
    operator = predicate.operator
    if operator not in _COMPILED_OPERATORS:
        _fail(
            ErrorCode.QUERY_COMPILATION_UNSUPPORTED,
            f"Operator '{operator.value}' is not compiled for PostgreSQL yet.",
            "where.op",
        )
    column = _column(field)
    if operator is ComparisonOperator.IS_NULL:
        return f"{column} IS NULL"
    if operator is ComparisonOperator.IS_NOT_NULL:
        return f"{column} IS NOT NULL"
    if operator in {ComparisonOperator.IN, ComparisonOperator.NOT_IN}:
        assert isinstance(predicate.value, tuple)
        placeholders = ", ".join("%s" for _ in predicate.value)
        parameters.extend(predicate.value)
        keyword = "IN" if operator is ComparisonOperator.IN else "NOT IN"
        return f"{column} {keyword} ({placeholders})"

    token = {
        ComparisonOperator.EQ: "=",
        ComparisonOperator.NE: "<>",
        ComparisonOperator.GT: ">",
        ComparisonOperator.GTE: ">=",
        ComparisonOperator.LT: "<",
        ComparisonOperator.LTE: "<=",
    }.get(operator)
    if token is None:
        raise AssertionError(f"Unhandled compiled operator: {operator!r}")
    parameters.append(predicate.value)
    return f"{column} {token} %s"


def _field_map(source: SingleSourceQueryBinding) -> dict[str, ResolvedField]:
    fields = {field.field.name: field for field in source.fields}
    fields.setdefault(source.logical_id.field.name, source.logical_id)
    return fields


def _require_resolved_field(
    context: _CompileContext,
    field_name: str,
    location: str,
) -> ResolvedField:
    try:
        return context.fields_by_name[field_name]
    except KeyError:
        _fail(
            ErrorCode.SOURCE_BINDING_UNAVAILABLE,
            f"Field '{field_name}' is not available in the selected PostgreSQL source binding.",
            location,
        )


def _effective_limit(query: SourceResolvedQuery) -> int:
    page_limit = query.query.page.first
    assert page_limit is not None
    requested_maximum = query.query.constraints.maximum_results
    return min(page_limit, requested_maximum) if requested_maximum is not None else page_limit


def _legacy_scan(query: SourceResolvedQuery) -> SourceScanPlan:
    """Keep direct compiler callers working while the engine uses physical plans."""

    source = query.identity_source
    by_name = {field.field.name: field for field in source.fields}
    by_name.setdefault(source.logical_id.field.name, source.logical_id)
    return SourceScanPlan(
        source=source,
        projection=tuple(by_name[field.name] for field in query.query.select),
        where=query.query.where,
        order_by=query.query.order_by,
        limit=_effective_limit(query),
    )


def _resource(resource: str) -> str:
    parts = resource.split(".")
    if not all(part for part in parts):
        _fail(
            ErrorCode.SOURCE_BINDING_UNAVAILABLE,
            "The selected source resource has an invalid qualified name.",
        )
    return ".".join(_quote_identifier(part) for part in parts)


def _column(field: ResolvedField) -> str:
    return _quote_identifier(field.physical_name)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _fail(code: ErrorCode, message: str, location: str | None = None) -> None:
    raise QueryError(ErrorDetail(code=code, message=message, retryable=False, location=location))
