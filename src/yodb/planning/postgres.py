"""PostgreSQL logical-operation capabilities used by the federated planner."""

from __future__ import annotations

from ..catalog import SourceKind
from ..query.models import ComparisonOperator
from ..query.resolution import SingleSourceQueryBinding
from .contracts import SourceCapabilities, SourceCapabilityProvider


class PostgresSourceCapabilities(SourceCapabilityProvider):
    """Declare logical features PostgreSQL can preserve in the V0.1 core.

    This is deliberately a semantic contract, not a statement about a
    particular index or SQL strategy.  PostgreSQL itself chooses those details
    when the adapter compiles and runs a remote operation.
    """

    _capabilities = SourceCapabilities(
        predicate_operators=frozenset(
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
        ),
        supports_projection_pushdown=True,
        supports_ordered_limit=True,
        supports_key_lookup=True,
        # This is a conservative application-level batch ceiling. The future
        # PostgreSQL compiler owns whether a batch becomes ANY(array), IN, or
        # another parameterized native representation.
        maximum_keys_per_lookup=1_000,
        supports_native_relation=True,
    )

    def capabilities_for(self, source: SingleSourceQueryBinding) -> SourceCapabilities:
        if source.source_kind is not SourceKind.POSTGRES:
            raise ValueError("PostgreSQL capabilities require a postgres source binding")
        return self._capabilities

