"""PostgreSQL connection resolution and bounded pooling."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
import re
import time
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


# Every session YoDb opens is UTC and read-only, whatever the role may be allowed to do.
SESSION_OPTIONS = "-c TimeZone=UTC -c default_transaction_read_only=on"

PostgresPoolFactory = Callable[[PostgresConnectionSettings, int, int, float], PostgresPool]
# Opens one connection and closes it, or raises a SourceConnectionError that says why not.
PostgresProbe = Callable[[PostgresConnectionSettings, str, float], None]

_PROBE_TIMEOUT_SECONDS = 10.0
_FAILURE_MEMORY_SECONDS = 5.0


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


class EnvPostgresConnectionResolver:
    """Resolve a connection reference from an environment variable.

    ``connection_ref: e2e-records`` reads ``YODB_CONN_E2E_RECORDS`` (upper-cased,
    every non-alphanumeric character becomes ``_``).  Secrets stay out of the
    catalog and out of files; the value is a libpq conninfo string or URI.
    """

    def __init__(self, *, prefix: str = "YODB_CONN_", environ: Mapping[str, str] | None = None) -> None:
        self._prefix = prefix
        self._environ = os.environ if environ is None else environ

    def variable_for(self, connection_ref: str) -> str:
        return self._prefix + re.sub(r"[^A-Za-z0-9]", "_", connection_ref).upper()

    def resolve(self, connection_ref: str) -> PostgresConnectionSettings:
        value = self._environ.get(self.variable_for(connection_ref))
        if not value:
            raise SourceConnectionError(
                ErrorDetail(
                    code=ErrorCode.CONNECTION_REFERENCE_NOT_FOUND,
                    message=(
                        f"No connection settings for reference '{connection_ref}': "
                        f"set the environment variable {self.variable_for(connection_ref)}."
                    ),
                    retryable=False,
                )
            )
        return PostgresConnectionSettings(conninfo=value)


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
        probe: PostgresProbe | None = None,
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
        # A pool retries a bad connection in the background for minutes; open one connection first,
        # so wrong credentials or an unreachable host fail at once with a clear, safe error.
        self._probe = probe if probe is not None else (_probe_with_psycopg if pool_factory is None else None)
        self._pools: dict[str, PostgresPool] = {}
        self._failures: dict[str, tuple[float, SourceConnectionError]] = {}
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
        pool = self._pool_for(connection_ref)
        timeout = timeout_seconds if timeout_seconds is not None else self._acquire_timeout_seconds
        return _lease(pool, timeout)

    def _pool_for(self, connection_ref: str) -> PostgresPool:
        with self._lock:
            self._raise_if_closed()
            pool = self._pools.get(connection_ref)
            if pool is not None:
                return pool
            failure = self._failures.get(connection_ref)
            if failure is not None and failure[0] > time.monotonic():
                raise failure[1]                       # a reference that just failed is not retried at once
            settings = self._resolver.resolve(connection_ref)
        if self._probe is not None:
            try:
                self._probe(settings, connection_ref, min(self._acquire_timeout_seconds, _PROBE_TIMEOUT_SECONDS))
            except SourceConnectionError as error:
                with self._lock:
                    self._failures[connection_ref] = (time.monotonic() + _FAILURE_MEMORY_SECONDS, error)
                raise
        with self._lock:
            self._raise_if_closed()
            self._failures.pop(connection_ref, None)
            pool = self._pools.get(connection_ref)
            if pool is None:
                pool = self._pool_factory(settings, self._min_size, self._max_size, self._acquire_timeout_seconds)
                self._pools[connection_ref] = pool
            return pool

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise SourceConnectionError(
                ErrorDetail(
                    code=ErrorCode.CONNECTION_POOL_CLOSED,
                    message="The PostgreSQL connection adapter is closed.",
                    retryable=False,
                )
            )

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


def _probe_with_psycopg(settings: PostgresConnectionSettings, connection_ref: str, timeout_seconds: float) -> None:
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError("PostgreSQL connections require the 'psycopg' dependency.") from error
    try:
        psycopg.connect(settings.conninfo, connect_timeout=max(1, math.ceil(timeout_seconds)), autocommit=True).close()
    except psycopg.Error as error:
        # The driver's message can carry hosts, users and parts of the conninfo: say only what happened.
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in ("28P01", "28000") or "authentication failed" in str(error).lower():
            raise SourceConnectionError(
                ErrorDetail(
                    code=ErrorCode.SOURCE_AUTHENTICATION_FAILED,
                    message=f"The PostgreSQL source rejected the credentials for connection reference '{connection_ref}'.",
                    retryable=False,
                )
            ) from error
        raise SourceConnectionError(
            ErrorDetail(
                code=ErrorCode.SOURCE_UNAVAILABLE,
                message=f"The PostgreSQL source for connection reference '{connection_ref}' could not be reached.",
                retryable=True,
            )
        ) from error


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
        kwargs={"autocommit": False, "options": SESSION_OPTIONS},
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
