"""PostgreSQL scan estimates from portable inspected metadata."""

from __future__ import annotations

from ..catalog import SourceKind
from ..inspection import PhysicalResource, SourceInspection
from ..planning import SourceScanPlan
from ..query.models import BoundAllExpression, BoundAnyExpression, BoundNotExpression, BoundPredicate, ComparisonOperator
from .contracts import CostConfidence, CostEstimate


class PostgresCostEstimator:
    source_kind = SourceKind.POSTGRES
    _DEFAULT_ROWS = 100_000.0
    _DEFAULT_ROW_BYTES = 128.0

    def estimate_scan(self, scan: SourceScanPlan, inspection: SourceInspection) -> CostEstimate:
        resource = inspection.resources.get(scan.source.resource)
        if resource is None:
            raise ValueError("scan resource was not found in its inspection")
        rows = resource.estimated_rows if resource.estimated_rows is not None else self._DEFAULT_ROWS
        selectivity, confidence, assumptions = _selectivity(scan.where, resource, scan)
        output = max(0.0, rows * selectivity)
        if scan.limit is not None:
            output = min(output, float(scan.limit))
        row_bytes = _row_bytes(resource, scan)
        indexed = _has_indexed_predicate(scan.where, resource, scan)
        work = (output * 1.2 if indexed else rows) + output
        latency = 3.0 + work / 25_000.0
        if resource.estimated_rows is None:
            assumptions = (*assumptions, "resource row count unavailable; used conservative default")
            confidence = CostConfidence.LOW
        multiplier = {CostConfidence.HIGH: 1.5, CostConfidence.MEDIUM: 3.0, CostConfidence.LOW: 10.0}[confidence]
        upper_rows = min(rows, output * multiplier)
        if scan.limit is not None:
            upper_rows = min(upper_rows, float(scan.limit))
        return CostEstimate(
            output, upper_rows, row_bytes, output * row_bytes, upper_rows * row_bytes,
            0.0, 0.0, work, latency, confidence, inspection.inspected_at,
            ("postgresql:pg_class", "postgresql:pg_stats", "postgresql:index_catalog"), assumptions,
        )


def _selectivity(expression, resource: PhysicalResource, scan: SourceScanPlan):
    if expression is None:
        return 1.0, CostConfidence.HIGH, ()
    if isinstance(expression, BoundPredicate):
        field = next(field for field in scan.source.fields if field.field.name == expression.field.name)
        stats = resource.fields.get(field.physical_name)
        if expression.operator in {ComparisonOperator.IS_NULL, ComparisonOperator.IS_NOT_NULL}:
            fraction = stats.null_fraction if stats and stats.null_fraction is not None else 0.1
            return (fraction if expression.operator is ComparisonOperator.IS_NULL else 1 - fraction), (CostConfidence.MEDIUM if stats and stats.null_fraction is not None else CostConfidence.LOW), ()
        if expression.operator in {ComparisonOperator.EQ, ComparisonOperator.NE, ComparisonOperator.IN, ComparisonOperator.NOT_IN}:
            distinct = stats.estimated_distinct_values if stats else None
            base = min(1.0, 1.0 / distinct) if distinct else 0.1
            count = len(expression.value) if expression.operator in {ComparisonOperator.IN, ComparisonOperator.NOT_IN} else 1
            value = min(1.0, base * count)
            if expression.operator in {ComparisonOperator.NE, ComparisonOperator.NOT_IN}:
                value = 1 - value
            return value, (CostConfidence.MEDIUM if distinct else CostConfidence.LOW), ()
        if expression.operator in {ComparisonOperator.GT, ComparisonOperator.GTE, ComparisonOperator.LT, ComparisonOperator.LTE}:
            return 1 / 3, CostConfidence.LOW, ("range histogram unavailable; used one-third selectivity",)
        return 0.1, CostConfidence.LOW, ("text predicate statistics unavailable; used ten-percent selectivity",)
    if isinstance(expression, BoundAllExpression):
        values = [_selectivity(child, resource, scan) for child in expression.expressions]
        return _combine_all(values)
    if isinstance(expression, BoundAnyExpression):
        values = [_selectivity(child, resource, scan) for child in expression.expressions]
        probability = 0.0
        for value, _, _ in values:
            probability = probability + value - probability * value
        return probability, _lowest(values), ("assumed independent OR predicates",)
    if isinstance(expression, BoundNotExpression):
        value, confidence, assumptions = _selectivity(expression.expression, resource, scan)
        return 1 - value, confidence, assumptions
    raise AssertionError(expression)


def _combine_all(values):
    probability = 1.0
    for value, _, _ in values:
        probability *= value
    return probability, _lowest(values), ("assumed independent AND predicates",)


def _lowest(values):
    order = {CostConfidence.LOW: 0, CostConfidence.MEDIUM: 1, CostConfidence.HIGH: 2}
    return min((confidence for _, confidence, _ in values), key=order.get)


def _row_bytes(resource: PhysicalResource, scan: SourceScanPlan) -> float:
    known = [resource.fields.get(field.physical_name).average_value_bytes for field in scan.projection if resource.fields.get(field.physical_name) and resource.fields.get(field.physical_name).average_value_bytes is not None]
    return float(sum(known) + 24 * len(scan.projection)) if known else float(resource.average_row_bytes or PostgresCostEstimator._DEFAULT_ROW_BYTES)


def _has_indexed_predicate(expression, resource: PhysicalResource, scan: SourceScanPlan) -> bool:
    if not isinstance(expression, BoundPredicate) or expression.operator not in {ComparisonOperator.EQ, ComparisonOperator.IN}:
        return False
    field = next(field for field in scan.source.fields if field.field.name == expression.field.name)
    return any(index.fields and index.fields[0] == field.physical_name for index in resource.indexes)
