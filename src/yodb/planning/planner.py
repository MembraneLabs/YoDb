"""Build a small, bounded set of federated candidates from source resolution."""

from __future__ import annotations

from dataclasses import dataclass

from ..query.models import BoundAllExpression, BoundFilterExpression, BoundPredicate
from ..query.resolution import QuerySourceShape, SingleSourceQueryBinding, SourceResolvedQuery
from .contracts import (
    CandidatePlan,
    InMemoryAssemblyPlan,
    KeySetPlan,
    KeyTransferPlan,
    SingleSourcePlan,
    SourceCapabilities,
    SourceScanPlan,
)
from .registry import SourceCapabilityRegistry


@dataclass(frozen=True)
class FederatedPlanningPolicy:
    """Static bounds that keep candidate enumeration and later execution safe."""

    maximum_rows_per_source: int = 10_000
    maximum_distinct_keys: int = 10_000
    maximum_key_transfer_candidates: int = 2


class FederatedPhysicalPlanner:
    """Apply mandatory safe pushdown and enumerate bounded federation choices.

    The planner does not rank or execute candidates.  Source-local conjunction
    predicates are always placed in their eligible remote scans; the complete
    expression remains a residual for the coordinator until the executor has
    a formally verified residual-elimination rule.
    """

    def __init__(
        self,
        capabilities: SourceCapabilityRegistry,
        *,
        policy: FederatedPlanningPolicy = FederatedPlanningPolicy(),
    ) -> None:
        self._capabilities = capabilities
        self._policy = policy

    def enumerate(self, resolved: SourceResolvedQuery) -> tuple[CandidatePlan, ...]:
        """Return every small, semantically valid V0.1 candidate strategy."""

        scans = tuple(self._scan_for(source, resolved) for source in resolved.sources)
        if resolved.shape is QuerySourceShape.SINGLE_SOURCE:
            scan = scans[0]
            return (
                CandidatePlan(
                    strategy="single_source_remote",
                    plan=SingleSourcePlan(
                        scan=scan,
                        residual_where=None
                        if _fully_pushable(resolved.query.where, scan.source, self._capabilities.capabilities_for(scan.source))
                        else resolved.query.where,
                    ),
                ),
            )

        baseline = InMemoryAssemblyPlan(
            anchor=scans[0],
            contributors=tuple(scans[1:]),
            query=resolved,
            residual_where=resolved.query.where,
            maximum_rows_per_source=self._policy.maximum_rows_per_source,
        )
        candidates: list[CandidatePlan] = [CandidatePlan("remote_scans_then_assembly", baseline)]
        # Key transfer is intentionally bounded to an anchor plus direct
        # contributors. Multi-hop transfer direction enumeration is deferred
        # until relationship traversal is implemented.
        key_transfer_count = 0
        for contributor_index, (contributor, link) in enumerate(
            zip(scans[1:], resolved.logical_id_links, strict=True)
        ):
            if key_transfer_count >= self._policy.maximum_key_transfer_candidates:
                break
            source_capabilities = self._capabilities.capabilities_for(contributor.source)
            if not source_capabilities.supports_key_lookup:
                continue
            batch_size = source_capabilities.maximum_keys_per_lookup
            if batch_size is None or batch_size <= 0:
                continue
            keys = KeySetPlan(
                input=scans[0],
                logical_id=scans[0].source.logical_id,
                maximum_distinct_keys=self._policy.maximum_distinct_keys,
            )
            transfer = KeyTransferPlan(
                keys=keys,
                destination=contributor,
                link=link,
                maximum_keys_per_batch=min(batch_size, self._policy.maximum_distinct_keys),
            )
            candidates.append(
                CandidatePlan(
                    strategy=f"key_transfer:{scans[0].source.source_name}->{contributor.source.source_name}",
                    plan=InMemoryAssemblyPlan(
                        anchor=scans[0],
                        contributors=tuple(
                            transfer if index == contributor_index else other
                            for index, other in enumerate(scans[1:])
                        ),
                        query=resolved,
                        residual_where=resolved.query.where,
                        maximum_rows_per_source=self._policy.maximum_rows_per_source,
                    ),
                )
            )
            key_transfer_count += 1
        return tuple(candidates)

    def _scan_for(self, source: SingleSourceQueryBinding, resolved: SourceResolvedQuery) -> SourceScanPlan:
        capabilities = self._capabilities.capabilities_for(source)
        pushed = (
            resolved.query.where
            if resolved.shape is QuerySourceShape.SINGLE_SOURCE
            and _fully_pushable(resolved.query.where, source, capabilities)
            else _safe_conjunctive_pushdown(resolved.query.where, source, capabilities)
        )
        # A source-local ordered limit is safe only for a fully single-source
        # result. In a federated plan, order/page remains global coordinator
        # work until an explicit proof says otherwise.
        if resolved.shape is QuerySourceShape.SINGLE_SOURCE and capabilities.supports_ordered_limit:
            return SourceScanPlan(
                source=source,
                projection=source.fields,
                pushed_where=pushed,
                order_by=resolved.query.order_by,
                limit=resolved.query.page.first,
            )
        return SourceScanPlan(source=source, projection=source.fields, pushed_where=pushed)


def _safe_conjunctive_pushdown(
    expression: BoundFilterExpression | None,
    source: SingleSourceQueryBinding,
    capabilities: SourceCapabilities,
) -> BoundFilterExpression | None:
    """Return only same-source leaf predicates under conjunctions.

    Splitting OR/NOT across sources changes SQL three-valued semantics.  Those
    expressions remain coordinator residual work until a later rule can prove
    equivalence.
    """

    predicates = _conjunctive_predicates(expression)
    if predicates is None:
        return None
    source_fields = {field.field.name for field in source.fields}
    pushed = tuple(
        predicate
        for predicate in predicates
        if predicate.field.name in source_fields and predicate.operator in capabilities.predicate_operators
    )
    if not pushed:
        return None
    return pushed[0] if len(pushed) == 1 else BoundAllExpression(pushed)


def _fully_pushable(
    expression: BoundFilterExpression | None,
    source: SingleSourceQueryBinding,
    capabilities: SourceCapabilities,
) -> bool:
    if expression is None:
        return True
    source_fields = {field.field.name for field in source.fields}
    if isinstance(expression, BoundPredicate):
        return expression.field.name in source_fields and expression.operator in capabilities.predicate_operators
    if isinstance(expression, BoundAllExpression):
        return all(_fully_pushable(child, source, capabilities) for child in expression.expressions)
    # The current PostgreSQL adapter can compile any/not when every leaf is
    # supported. Other adapters can extend this through their capability
    # contract without teaching the caller source-native syntax.
    expressions = getattr(expression, "expressions", None)
    if expressions is not None:
        return all(_fully_pushable(child, source, capabilities) for child in expressions)
    child = getattr(expression, "expression", None)
    return child is not None and _fully_pushable(child, source, capabilities)


def _conjunctive_predicates(expression: BoundFilterExpression | None) -> tuple[BoundPredicate, ...] | None:
    if expression is None:
        return ()
    if isinstance(expression, BoundPredicate):
        return (expression,)
    if isinstance(expression, BoundAllExpression):
        children = tuple(_conjunctive_predicates(child) for child in expression.expressions)
        if any(child is None for child in children):
            return None
        return tuple(predicate for child in children for predicate in child or ())
    return None
