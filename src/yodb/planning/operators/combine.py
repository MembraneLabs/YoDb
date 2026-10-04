"""COMBINE: assemble one logical record from several sources, in a cost-chosen order."""

from __future__ import annotations

from dataclasses import dataclass

from ...operators import OperatorKind
from ...query.models import BoundQuery
from ...query.resolution import SourceResolvedQuery
from ..contracts import (
    AssemblyStep,
    PlanProperties,
    RecordAssembly,
    RemoteScan,
    ResultCompleteness,
    ResultShape,
    StepRole,
    coordinator_location,
    default_schedule,
)
from ..optimizer import Constraints, Fallback, OptimizerResult, Problem, SourceInput, optimize
from .base import ExtensionPlan, PlanningServices, Strategy
from .scan import ScanPlan, deduplicate_fields


@dataclass(frozen=True)
class CombineDecision:
    """How to read the sources, and which extension strategies the costing picked."""

    schedule: tuple[AssemblyStep, ...]           # empty: use the fixed-rule order
    chosen: tuple[Strategy | None, ...]          # per extension plan: the cost-based pick, or None
    notes: tuple[str, ...]                       # how it was decided (cost-based, or rules and why)


class CombineOperator:
    kind = OperatorKind.COMBINE

    def __init__(self, services: PlanningServices) -> None:
        self._services = services

    def build(
        self,
        scans: tuple[RemoteScan, ...],
        resolved: SourceResolvedQuery,
        schedule: tuple[AssemblyStep, ...] = (),
    ) -> RecordAssembly:
        anchor, *contributors = scans
        fields = deduplicate_fields(field for scan in scans for field in scan.projection)
        # A contributor scan with a pushed top-level AND conjunct is a candidate-ID
        # restriction as well as an enrichment source.  The executor retains only
        # anchor records present in that scan, so a non-returned contributor is
        # never mistaken for a logical NULL.
        required = tuple(scan.source.source_name for scan in contributors if scan.pushed_filter is not None)
        return RecordAssembly(
            anchor=anchor,
            contributors=tuple(contributors),
            required_contributor_matches=required,
            properties=PlanProperties(
                output_fields=fields,
                logical_id=anchor.properties.logical_id,
                ids_are_unique=True,
                ordering=None,
                location=coordinator_location(),
                completeness=ResultCompleteness.EXACT,
                result_shape=ResultShape.RECORDS,
                catalog_fingerprint=resolved.query.catalog_fingerprint,
            ),
            # Each scan carries its own source's limit; 0 disables transfer.
            maximum_transfer_keys=self._services.policy.maximum_transfer_keys or None,
            schedule=schedule or default_schedule(anchor, tuple(contributors), required),
        )

    def decide(
        self,
        scan_plan: ScanPlan,
        extensions: tuple[ExtensionPlan, ...],
        query: BoundQuery,
    ) -> CombineDecision:
        """Fixed rules always give a correct plan.  With enough statistics the optimizer may
        replace the read order and pick cheaper extension strategies; otherwise it says why
        it declined (and the fixed-rule plan stands)."""

        outcome = self._optimize(scan_plan, extensions, query)
        if isinstance(outcome, OptimizerResult):
            chosen = tuple(
                ext.strategy_for(outcome.variant) if len(extensions) == 1 else None for ext in extensions
            ) or ()
            return CombineDecision(
                outcome.schedule,
                chosen,
                (
                    "strategy=cost_based",
                    f"estimated_latency_ms={outcome.estimate.latency_ms:.1f}",
                    f"estimated_money={outcome.estimate.money:.6f}",
                    f"candidates_considered={outcome.candidates_considered}",
                ),
            )
        return CombineDecision((), tuple(None for _ in extensions), ("strategy=rules", f"reason={outcome.reason}"))

    def _optimize(
        self, scan_plan: ScanPlan, extensions: tuple[ExtensionPlan, ...], query: BoundQuery
    ) -> OptimizerResult | Fallback:
        services = self._services
        if services.statistics is None:
            return Fallback("no statistics are configured")
        if len(extensions) > 1:
            return Fallback("cost-based choice supports one extension operator per query")
        scans = scan_plan.scans
        inputs: list[SourceInput] = []
        for index, scan in enumerate(scans):
            estimate = services.statistics.estimate_scan(scan.source, scan.pushed_filter)
            if not estimate.known or estimate.filtered_rows is None:
                return Fallback(f"statistics are unavailable for source '{scan.source.source_name}'")
            caps = services.capabilities(scan.source.source_kind)
            role = StepRole.ANCHOR if index == 0 else (StepRole.REQUIRED if scan.pushed_filter is not None else StepRole.OPTIONAL)
            inputs.append(
                SourceInput(
                    name=scan.source.source_name,
                    role=role,
                    total_rows=estimate.total_rows,
                    filtered_rows=estimate.filtered_rows,
                    profile=estimate.profile,
                    key_limit=None if caps.key_lookup is None else caps.key_lookup.maximum_keys,
                    row_cap=services.row_cap(scan.source.source_kind) if scan.maximum_rows is not None else None,
                )
            )
        extension = extensions[0] if extensions else None
        problem = Problem(
            inputs,
            services.costs,
            maximum_transfer_keys=services.policy.maximum_transfer_keys or None,
            variants=() if extension is None else extension.variants,
            constraints=Constraints(
                maximum_money=query.constraints.maximum_cost,
                maximum_latency_ms=None
                if query.constraints.maximum_latency_ms is None
                else float(query.constraints.maximum_latency_ms),
            ),
        )
        required = tuple(scan.source.source_name for scan in scans[1:] if scan.pushed_filter is not None)
        rule_order = [s.source_name for s in default_schedule(scans[0], scans[1:], required) if s.role is not StepRole.OPTIONAL]
        return optimize(
            problem, rule_order=rule_order, rule_variant=None if extension is None else extension.default.variant
        )
