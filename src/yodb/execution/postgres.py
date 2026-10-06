"""PostgreSQL execution adapter for parameterized compiled YoDb statements."""

from __future__ import annotations

from datetime import UTC, datetime
import math
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

    def __init__(
        self,
        connections: SourceConnectionAdapter[Any],
        *,
        default_statement_timeout_seconds: float | None = 60.0,
    ) -> None:
        if connections.source_kind is not SourceKind.POSTGRES:
            raise ValueError("PostgresQueryExecutionAdapter requires a PostgreSQL connection adapter")
        if default_statement_timeout_seconds is not None and default_statement_timeout_seconds <= 0:
            raise ValueError("default_statement_timeout_seconds must be positive")
        self._connections = connections
        self._default_statement_timeout = default_statement_timeout_seconds

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
        limit = timeout_seconds if timeout_seconds is not None else self._default_statement_timeout
        try:
            with self._connections.acquire(
                query.connection_ref,
                timeout_seconds=timeout_seconds,
            ) as connection:
                with connection.cursor() as cursor:
                    if limit is not None:
                        # The database stops the statement itself; the pool timeout only bounds waiting.
                        cursor.execute(
                            "SELECT set_config('statement_timeout', %s, true)", (str(max(1, math.ceil(limit * 1000))),)
                        )
                    cursor.execute(query.sql, query.parameters)
                    rows = cursor.fetchall()
        except YoDbError:
            raise
        except Exception as error:
            if type(error).__name__ == "QueryCanceled":
                raise QueryExecutionError(
                    ErrorDetail(
                        code=ErrorCode.QUERY_TIMEOUT,
                        message="The PostgreSQL source did not answer within the time limit.",
                        retryable=True,
                        source_name=query.source_name,
                    )
                ) from error
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
    return {name: _normalize(value) for name, value in zip(fields, row, strict=True)}


def _normalize(value: Any) -> Any:
    """Return timestamps as UTC-aware, matching how query values are bound.

    A ``timestamp`` (without time zone) column arrives naive; comparing it with
    an aware query value would raise and the coordinator would drop every row.
    Naive source timestamps are interpreted as UTC.
    """

    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value
