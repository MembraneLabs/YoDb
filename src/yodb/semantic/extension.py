"""The semantic filter as one pluggable extension.

This is the whole interface between the semantic filter and the rest of YoDb::

    extension = SemanticExtension(SemanticRuntime(verifier, embedder))
    engine = QueryExecutionEngine(catalog, compilers, executors, extensions=[extension])

It offers the two halves an engine needs: the planning operator (which strategies
exist and what they cost) and the execution handler (how a verification node
runs).  The query term (``{"semantic": ...}``) is registered by default in
:mod:`yodb.query.semantic`.  Nothing else in the planner, optimizer or executor
refers to semantic conditions; a result's report is ``result.reports["semantic"]``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import partial

from ..execution.operators.base import Handler
from ..planning.operators.base import PlanningServices
from .contracts import SemanticRuntime
from .execution import execute
from .planning import SemanticCosts, SemanticOperator, SemanticPolicy, SemanticVerify

NAME = "semantic"


class SemanticExtension:
    """Plan and run semantic conditions with the given providers.

    ``runtime`` may be omitted to plan only (or to get a clear error when a query
    asks for verification with no provider).  Unless given, the policy learns the
    embedding space from the embedder, and the costs from the providers' own
    ``cost_hint`` and batch size.
    """

    name = NAME

    def __init__(
        self,
        runtime: SemanticRuntime | None = None,
        *,
        policy: SemanticPolicy | None = None,
        costs: SemanticCosts | None = None,
    ) -> None:
        self.runtime = runtime
        self.policy = policy or _policy_from(runtime)
        self.costs = costs or _costs_from(runtime)

    def planning_operator(self, services: PlanningServices) -> SemanticOperator:
        return SemanticOperator(services, self.policy, self.costs)

    def handlers(self) -> Mapping[type, Handler]:
        return {SemanticVerify: partial(execute, self.runtime)}


def _policy_from(runtime: SemanticRuntime | None) -> SemanticPolicy:
    embedder = runtime.embedder if runtime is not None else None
    return SemanticPolicy(
        embedder=None if embedder is None else embedder.info,
        embedder_dimensions=None if embedder is None else embedder.dimensions,
    )


def _costs_from(runtime: SemanticRuntime | None, base: SemanticCosts = SemanticCosts()) -> SemanticCosts:
    """Use the providers' own cost hints and batch size when they publish them."""

    if runtime is None:
        return base
    verification = getattr(runtime.verifier, "cost_hint", None)
    embedding = getattr(runtime.embedder, "cost_hint", None) if runtime.embedder is not None else None
    return replace(
        base,
        verifier_batch_size=runtime.batch_size,
        verification=verification or base.verification,
        embedding=embedding or base.embedding,
    )
