"""PostgreSQL connection resolution and bounded pooling."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any, ContextManager, Protocol

from ..catalog import SourceKind
from ..errors import ErrorCode, ErrorDetail, SourceConnectionError


@dataclass(frozen=True)
class PostgresConnectionSettings:
    """Private PostgreSQL settings returned by a connection-reference resolver."""

    conninfo: str


class PostgresConnectionReferenceResolver(Protocol):
    """Resolve an opaque catalog reference into PostgreSQL connection settings."""

    def resolve(self, connection_ref: str) -> PostgresConnectionSettings:
        """Return the private conninfo for ``connection_ref``."""


class PostgresPool(Protocol):
    """The small Psycopg pool surface YoDb needs."""

    def connection(self, timeout: float | None = None) -> ContextManager[Any]: ...

    def close(self) -> Any: ...


PostgresPoolFactory = Callable[[PostgresConnectionSettings, int, int, float], PostgresPool]


class MappingPostgresConnectionResolver:
    """A simple resolver for local development and deterministic tests.

    Production deployments should use an implementation backed by the chosen
    secret manager instead of placing secret conninfo in catalog YAML.
    """

    def __init__(self, settings_by_ref: Mapping[str, PostgresConnectionSettings]) -> None:
        self._settings_by_ref = dict(settings_by_ref)

    def resolve(self, connection_ref: str) -> PostgresConnectionSettings:
        try:
            return self._settings_by_ref[connection_ref]
        except KeyError as error:
            raise SourceConnectionError(
                ErrorDetail(
                    code=ErrorCode.CONNECTION_REFERENCE_NOT_FOUND,
                    message="No PostgreSQL connection settings were found for the configured reference.",
                    retryable=False,
                )
            ) from error


class PostgresConnectionAdapter:
    """Lease connections from one bounded Psycopg pool per connection reference.

    ``acquire()`` blocks when the pool is fully leased and raises a structured
    timeout error only after the configured or caller-supplied timeout.
    """

    source_kind = SourceKind.POSTGRES

    def __init__(
        self,
        resolver: PostgresConnectionReferenceResolver,
        *,
        min_size: int = 0,
        max_size: int = 10,
        acquire_timeout_seconds: float = 30.0,
        pool_factory: PostgresPoolFactory | None = None,
    ) -> None:
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("pool size must satisfy 0 <= min_size <= max_size")
        if acquire_timeout_seconds <= 0:
            raise ValueError("acquire_timeout_seconds must be positive")
        self._resolver = resolver
        self._min_size = min_size
        self._max_size = max_size
        self._acquire_timeout_seconds = acquire_timeout_seconds
        self._pool_factory = pool_factory or _create_psycopg_pool
        self._pools: dict[str, PostgresPool] = {}
        self._lock = Lock()
        self._closed = False

    def acquire(
        self,
        connection_ref: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ContextManager[Any]:
        """Return a lease that waits for an available PostgreSQL connection."""

        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        with self._lock:
            if self._closed:
                raise SourceConnectionError(
                    ErrorDetail(
                        code=ErrorCode.CONNECTION_POOL_CLOSED,
                        message="The PostgreSQL connection adapter is closed.",
                        retryable=False,
                    )
                )
            pool = self._pools.get(connection_ref)
            if pool is None:
                settings = self._resolver.resolve(connection_ref)
                pool = self._pool_factory(
                    settings,
                    self._min_size,
                    self._max_size,
                    self._acquire_timeout_seconds,
                )
                self._pools[connection_ref] = pool

        timeout = timeout_seconds if timeout_seconds is not None else self._acquire_timeout_seconds
        return _lease(pool, timeout)

    def close(self) -> None:
        """Close all owned pools and reject future leases."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            pools = tuple(self._pools.values())
            self._pools.clear()
        for pool in pools:
            pool.close()


def _create_psycopg_pool(
    settings: PostgresConnectionSettings,
    min_size: int,
    max_size: int,
    timeout_seconds: float,
) -> PostgresPool:
    try:
        from psycopg_pool import ConnectionPool
    except ImportError as error:
        raise RuntimeError(
            "PostgreSQL pooling requires the optional 'psycopg-pool' dependency."
        ) from error
    return ConnectionPool(
        conninfo=settings.conninfo,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout_seconds,
        kwargs={"autocommit": False},
        open=True,
    )


@contextmanager
def _lease(pool: PostgresPool, timeout_seconds: float) -> Iterator[Any]:
    try:
        with pool.connection(timeout=timeout_seconds) as connection:
            yield connection
    except Exception as error:
        if _is_pool_timeout(error):
            raise SourceConnectionError(
                ErrorDetail(
                    code=ErrorCode.CONNECTION_POOL_TIMEOUT,
                    message="No PostgreSQL connection became available before the timeout.",
                    retryable=True,
                    details={"timeout_seconds": timeout_seconds},
                )
            ) from error
        raise


def _is_pool_timeout(error: Exception) -> bool:
    return error.__class__.__name__ == "PoolTimeout"
