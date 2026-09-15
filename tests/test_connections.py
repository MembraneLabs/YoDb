from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
import unittest

from yodb import (
    ConnectionAdapterRegistry,
    ErrorCode,
    MappingPostgresConnectionResolver,
    PostgresConnectionAdapter,
    PostgresConnectionSettings,
    SourceConnectionError,
)
from yodb.catalog import SourceKind


class ConnectionsTests(unittest.TestCase):
    def test_postgres_adapter_reuses_a_pool_for_one_connection_reference(self) -> None:
        created: list[FakePool] = []

        def create_pool(
            settings: PostgresConnectionSettings, min_size: int, max_size: int, timeout: float
        ) -> FakePool:
            self.assertEqual(settings.conninfo, "postgresql://readonly")
            self.assertEqual((min_size, max_size, timeout), (0, 2, 3.0))
            pool = FakePool()
            created.append(pool)
            return pool

        adapter = PostgresConnectionAdapter(
            MappingPostgresConnectionResolver(
                {"secret://yodb/crm-readonly": PostgresConnectionSettings("postgresql://readonly")}
            ),
            max_size=2,
            acquire_timeout_seconds=3.0,
            pool_factory=create_pool,
        )

        with adapter.acquire("secret://yodb/crm-readonly") as first:
            self.assertEqual(first, "connection")
        with adapter.acquire("secret://yodb/crm-readonly", timeout_seconds=1.5) as second:
            self.assertEqual(second, "connection")

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].timeouts, [3.0, 1.5])
        adapter.close()
        self.assertTrue(created[0].closed)

    def test_pool_timeout_is_exposed_as_a_structured_retryable_error(self) -> None:
        adapter = PostgresConnectionAdapter(
            MappingPostgresConnectionResolver(
                {"secret://yodb/crm-readonly": PostgresConnectionSettings("postgresql://readonly")}
            ),
            pool_factory=lambda settings, min_size, max_size, timeout: TimedOutPool(),
        )

        with self.assertRaises(SourceConnectionError) as raised:
            with adapter.acquire("secret://yodb/crm-readonly", timeout_seconds=0.25):
                pass

        self.assertEqual(raised.exception.code, ErrorCode.CONNECTION_POOL_TIMEOUT)
        self.assertTrue(raised.exception.retryable)

    def test_registry_selects_the_postgres_adapter_by_source_kind(self) -> None:
        adapter = PostgresConnectionAdapter(
            MappingPostgresConnectionResolver(
                {"secret://yodb/crm-readonly": PostgresConnectionSettings("postgresql://readonly")}
            ),
            pool_factory=lambda settings, min_size, max_size, timeout: FakePool(),
        )

        registry = ConnectionAdapterRegistry([adapter])

        self.assertIs(registry.adapter_for(SourceKind.POSTGRES), adapter)


class FakePool:
    def __init__(self) -> None:
        self.timeouts: list[float | None] = []
        self.closed = False

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[str]:
        self.timeouts.append(timeout)
        yield "connection"

    def close(self) -> None:
        self.closed = True


class PoolTimeout(Exception):
    pass


class TimedOutPool:
    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[Any]:
        raise PoolTimeout("timed out")
        yield None

    def close(self) -> None:
        pass
