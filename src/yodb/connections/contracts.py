"""Provider-neutral contracts for resolving and leasing source connections."""

from __future__ import annotations

from typing import ContextManager, Protocol, TypeVar, runtime_checkable

from ..catalog import SourceKind


ConnectionT = TypeVar("ConnectionT", covariant=True)
SettingsT = TypeVar("SettingsT", covariant=True)


@runtime_checkable
class ConnectionReferenceResolver(Protocol[SettingsT]):
    """Resolve an opaque catalog connection reference into provider settings.

    Implementations may use a secret manager, environment variables, a local
    development file, or another approved configuration mechanism.  The
    resolver is intentionally separate from a database adapter.
    """

    def resolve(self, connection_ref: str) -> SettingsT:
        """Return private provider settings for one opaque reference."""


@runtime_checkable
class SourceConnectionAdapter(Protocol[ConnectionT]):
    """Lease one source connection, returning it safely when the block exits."""

    @property
    def source_kind(self) -> SourceKind:
        """The catalog source kind supported by this adapter."""

    def acquire(
        self,
        connection_ref: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ContextManager[ConnectionT]:
        """Block until a connection is available or the timeout expires."""
