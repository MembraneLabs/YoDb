"""PostgreSQL execution adapter for parameterized compiled YoDb statements."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..catalog import SourceKind
from ..compilation import CompiledPostgresQuery, CompiledQuery
from ..connections import SourceConnectionAdapter
from ..errors import ErrorCode, ErrorDetail, QueryExecutionError, YoDbError
from .contracts import LogicalRow


class PostgresQueryExecutionAdapter:
    """Execute :class:`CompiledPostgresQuery` through a supplied connection adapter.

    The adapter deliberately accepts the provider-neutral connection protocol,
    rather than constructing a pool itself.  That keeps pooling and secret
    resolution reusable by inspection and future PostgreSQL query features.
    """

    source_kind = SourceKind.POSTGRES

    def __init__(self, connections: SourceConnectionAdapter[Any]) -> None:
        if connections.source_kind is not SourceKind.POSTGRES:
            raise ValueError("PostgresQueryExecutionAdapter requires a PostgreSQL connection adapter")
        self._connections = connections

    def execute(
        self,
        query: CompiledQuery,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[LogicalRow, ...]:
        """Run a PostgreSQL statement and normalize rows to logical aliases."""

        if not isinstance(query, CompiledPostgresQuery):
            raise QueryExecutionError(
                ErrorDetail(
                    code=ErrorCode.QUERY_EXECUTION_UNSUPPORTED,
                    message="The PostgreSQL executor requires a PostgreSQL compiled query.",
                    retryable=False,
                )
            )
        try:
            with self._connections.acquire(
                query.connection_ref,
                timeout_seconds=timeout_seconds,
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(query.sql, query.parameters)
                    rows = cursor.fetchall()
        except YoDbError:
            raise
        except Exception as error:
            raise QueryExecutionError(
                ErrorDetail(
                    code=ErrorCode.QUERY_EXECUTION_FAILED,
                    message="The PostgreSQL source could not execute the compiled query.",
                    retryable=False,
                    source_name=query.source_name,
                )
            ) from error

        fields = tuple(column.logical_field for column in query.output_columns)
        return tuple(_logical_row(fields, row, query.source_name) for row in rows)


def _logical_row(
    fields: tuple[str, ...],
    row: Any,
    source_name: str,
) -> LogicalRow:
    if not isinstance(row, (tuple, list)) or len(row) != len(fields):
        raise QueryExecutionError(
            ErrorDetail(
                code=ErrorCode.QUERY_EXECUTION_FAILED,
                message="The PostgreSQL source returned an unexpected result shape.",
                retryable=False,
                source_name=source_name,
            )
        )
    return dict(zip(fields, row, strict=True))
