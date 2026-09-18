"""PostgreSQL adapter estimates for source-local scans and keyed lookups."""

from __future__ import annotations

from math import ceil

from ..catalog import SourceKind
from ..planning import KeyTransferPlan, SourceScanPlan
from ..query.models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    ComparisonOperator,
)
from ..runtime import CatalogEvaluation
from .contracts import (
    EstimateConfidence,
    RangeEstimate,
    RemoteOperationEstimate,
    ResultEstimate,
    ResultCompleteness,
)


_FALLBACK_ROWS = 100_000.0
_FALLBACK_ROW_BYTES = 256.0
_FALLBACK_DISTINCT_FRACTION = 0.10
_FALLBACK_NULL_FRACTION = 0.10
_RANGE_SELECTIVITY = 1 / 3
_CONFIDENCE_RANK = {EstimateConfidence.LOW: 0, EstimateConfidence.MEDIUM: 1, EstimateConfidence.HIGH: 2}


class PostgresCostEstimator:
    """Estimate portable remote effects from PostgreSQL inspection statistics.

    PostgreSQL retains ownership of access-path and join selection.  This
    adapter estimates only the remote response shape, call count, and rough
    response time needed by YoDb's federated chooser.
    """

    source_kind = SourceKind.POSTGRES

    def estimate_remote_scan(self, scan: SourceScanPlan, active: CatalogEvaluation) -> RemoteOperationEstimate:
        inspection = _inspection_for(scan, active)
        resource = inspection.resources.get(scan.source.resource)
        input_rows, base_confidence, assumptions = _resource_rows(resource)
        selectivity, filter_confidence, filter_assumptions = _selectivity(scan, resource)
        row_bytes, width_confidence, width_assumptions = _projected_row_bytes(scan, resource)
        expected_rows = input_rows.expected * selectivity
        upper_rows = input_rows.upper_bound * min(1.0, selectivity * _uncertainty_multiplier(filter_confidence))
        if scan.limit is not None:
            expected_rows = min(expected_rows, scan.limit)
            upper_rows = min(upper_rows, float(scan.limit))
        result = ResultEstimate(
            rows=RangeEstimate(expected_rows, max(expected_rows, upper_rows)),
            row_bytes=row_bytes,
        )
        confidence = _lowest(base_confidence, filter_confidence, width_confidence)
        return RemoteOperationEstimate(
            result=result,
            remote_calls=RangeEstimate(1, 1),
            startup_latency_ms=RangeEstimate(3, 15),
            execution_latency_ms=_latency_for(result.output_bytes),
            confidence=confidence,
            statistics_collected_at=inspection.inspected_at,
            provenance=("postgresql:pg_class", "postgresql:pg_stats", "postgresql:index_catalog"),
            assumptions=(*assumptions, *filter_assumptions, *width_assumptions),
            source_details={"leading_filter_index": _has_leading_filter_index(scan, resource)},
        )

    def estimate_key_lookup(
        self,
        transfer: KeyTransferPlan,
        key_result: ResultEstimate,
        active: CatalogEvaluation,
    ) -> RemoteOperationEstimate:
        inspection = _inspection_for(transfer.destination, active)
        resource = inspection.resources.get(transfer.destination.source.resource)
        source_rows, source_confidence, source_assumptions = _resource_rows(resource)
        row_bytes, width_confidence, width_assumptions = _projected_row_bytes(transfer.destination, resource)
        # Each contributor representation must declare logical id as a unique
        # identity. At most one contributor record can enrich each transferred
        # anchor ID, so key count is a conservative response-cardinality cap.
        rows = RangeEstimate(
            min(key_result.rows.expected, source_rows.expected),
            min(key_result.rows.upper_bound, source_rows.upper_bound),
        )
        calls = RangeEstimate(
            ceil(key_result.rows.expected / transfer.maximum_keys_per_batch),
            ceil(key_result.rows.upper_bound / transfer.maximum_keys_per_batch),
        )
        result = ResultEstimate(rows=rows, row_bytes=row_bytes)
        confidence = _lowest(source_confidence, width_confidence, EstimateConfidence.MEDIUM)
        return RemoteOperationEstimate(
            result=result,
            remote_calls=calls,
            startup_latency_ms=RangeEstimate(3 * calls.expected, 15 * calls.upper_bound),
            execution_latency_ms=_latency_for(result.output_bytes, calls),
            confidence=confidence,
            statistics_collected_at=inspection.inspected_at,
            provenance=("postgresql:pg_class", "postgresql:index_catalog"),
            assumptions=(
                *source_assumptions,
                *width_assumptions,
                "declared contributor logical id is unique",
                f"keyed lookup is batched at {transfer.maximum_keys_per_batch} ids",
            ),
            source_details={"key_lookup_indexed": _has_identity_index(transfer.destination, resource)},
        )


def _inspection_for(scan: SourceScanPlan, active: CatalogEvaluation):
    inspection = active.sources[scan.source.source_name].inspection
    if inspection is None:
        raise ValueError(f"source '{scan.source.source_name}' has no inspection snapshot")
    return inspection


def _resource_rows(resource) -> tuple[RangeEstimate, EstimateConfidence, tuple[str, ...]]:
    if resource is not None and resource.estimated_rows is not None and resource.estimated_rows > 0:
        return RangeEstimate(resource.estimated_rows, resource.estimated_rows * 3), EstimateConfidence.MEDIUM, ()
    return (
        RangeEstimate(_FALLBACK_ROWS, _FALLBACK_ROWS * 10),
        EstimateConfidence.LOW,
        ("resource cardinality is unavailable; a conservative fallback was used",),
    )


def _projected_row_bytes(
    scan: SourceScanPlan,
    resource,
) -> tuple[RangeEstimate, EstimateConfidence, tuple[str, ...]]:
    if resource is None:
        return RangeEstimate(_FALLBACK_ROW_BYTES, _FALLBACK_ROW_BYTES * 2), EstimateConfidence.LOW, (
            "resource width is unavailable; a conservative fallback was used",
        )
    fields = [resource.fields.get(field.physical_name) for field in scan.projection]
    if fields and all(field is not None and field.average_value_bytes is not None for field in fields):
        width = sum(field.average_value_bytes or 0 for field in fields) + 16 * len(fields)
        return RangeEstimate(width, width * 1.5), EstimateConfidence.MEDIUM, ()
    if resource.average_row_bytes is not None and resource.fields:
        projected_ratio = min(1.0, max(1 / len(resource.fields), len(fields) / len(resource.fields)))
        width = max(16.0, resource.average_row_bytes * projected_ratio)
        return RangeEstimate(width, width * 2), EstimateConfidence.LOW, (
            "projected width was derived from resource average width",)
    return RangeEstimate(_FALLBACK_ROW_BYTES, _FALLBACK_ROW_BYTES * 2), EstimateConfidence.LOW, (
        "projected width is unavailable; a conservative fallback was used",)


def _selectivity(
    scan: SourceScanPlan,
    resource,
) -> tuple[float, EstimateConfidence, tuple[str, ...]]:
    if scan.pushed_where is None:
        return 1.0, EstimateConfidence.HIGH, ()
    return _expression_selectivity(scan.pushed_where, scan, resource)


def _expression_selectivity(
    expression: BoundFilterExpression,
    scan: SourceScanPlan,
    resource,
) -> tuple[float, EstimateConfidence, tuple[str, ...]]:
    if isinstance(expression, BoundPredicate):
        return _predicate_selectivity(expression, scan, resource)
    if isinstance(expression, BoundAllExpression):
        parts = tuple(_expression_selectivity(child, scan, resource) for child in expression.expressions)
        return (
            _clamp_product(*(part[0] for part in parts)),
            _lowest(*(part[1] for part in parts)),
            tuple(item for part in parts for item in part[2]) + ("conjunct selectivities assume independence",),
        )
    if isinstance(expression, BoundAnyExpression):
        parts = tuple(_expression_selectivity(child, scan, resource) for child in expression.expressions)
        probability_none = _clamp_product(*(1 - part[0] for part in parts))
        return (
            1 - probability_none,
            _lowest(*(part[1] for part in parts)),
            tuple(item for part in parts for item in part[2]) + ("disjunct selectivities assume independence",),
        )
    if isinstance(expression, BoundNotExpression):
        selectivity, confidence, assumptions = _expression_selectivity(expression.expression, scan, resource)
        return 1 - selectivity, confidence, (*assumptions, "negation ignores source null-distribution detail")
    raise AssertionError(f"Unknown bound expression: {expression!r}")


def _predicate_selectivity(
    predicate: BoundPredicate,
    scan: SourceScanPlan,
    resource,
) -> tuple[float, EstimateConfidence, tuple[str, ...]]:
    physical_name = next(
        (field.physical_name for field in scan.source.fields if field.field.name == predicate.field.name),
        None,
    )
    physical = resource.fields.get(physical_name) if resource is not None and physical_name is not None else None
    distinct = physical.estimated_distinct_values if physical is not None else None
    null_fraction = physical.null_fraction if physical is not None else None
    if predicate.operator in {ComparisonOperator.EQ, ComparisonOperator.NE, ComparisonOperator.IN, ComparisonOperator.NOT_IN}:
        values = len(predicate.value) if predicate.operator in {ComparisonOperator.IN, ComparisonOperator.NOT_IN} else 1
        if distinct is not None and distinct > 0:
            match = min(1.0, values / distinct)
            confidence = EstimateConfidence.MEDIUM
            assumptions: tuple[str, ...] = ()
        else:
            match = min(1.0, values * _FALLBACK_DISTINCT_FRACTION)
            confidence = EstimateConfidence.LOW
            assumptions = (f"distinct count unavailable for '{predicate.field.name}'",)
        return (1 - match if predicate.operator in {ComparisonOperator.NE, ComparisonOperator.NOT_IN} else match, confidence, assumptions)
    if predicate.operator is ComparisonOperator.IS_NULL:
        if null_fraction is not None:
            return null_fraction, EstimateConfidence.MEDIUM, ()
        return _FALLBACK_NULL_FRACTION, EstimateConfidence.LOW, (f"null fraction unavailable for '{predicate.field.name}'",)
    if predicate.operator is ComparisonOperator.IS_NOT_NULL:
        if null_fraction is not None:
            return 1 - null_fraction, EstimateConfidence.MEDIUM, ()
        return 1 - _FALLBACK_NULL_FRACTION, EstimateConfidence.LOW, (f"null fraction unavailable for '{predicate.field.name}'",)
    return _RANGE_SELECTIVITY, EstimateConfidence.LOW, (f"range/text selectivity fallback used for '{predicate.field.name}'",)


def _has_leading_filter_index(scan: SourceScanPlan, resource) -> bool:
    if resource is None or scan.pushed_where is None:
        return False
    first = _first_predicate(scan.pushed_where)
    if first is None:
        return False
    physical = next((field.physical_name for field in scan.source.fields if field.field.name == first.field.name), None)
    return physical is not None and any(index.valid is not False and index.fields[:1] == (physical,) for index in resource.indexes)


def _has_identity_index(scan: SourceScanPlan, resource) -> bool:
    if resource is None:
        return False
    physical = scan.source.logical_id.physical_name
    return any(index.valid is not False and index.fields[:1] == (physical,) for index in resource.indexes)


def _first_predicate(expression: BoundFilterExpression) -> BoundPredicate | None:
    if isinstance(expression, BoundPredicate):
        return expression
    if isinstance(expression, BoundAllExpression):
        return next((found for child in expression.expressions if (found := _first_predicate(child)) is not None), None)
    return None


def _latency_for(bytes_out: RangeEstimate, calls: RangeEstimate | None = None) -> RangeEstimate:
    calls = calls or RangeEstimate(1, 1)
    return RangeEstimate(
        expected=bytes_out.expected / 5_000_000 + calls.expected,
        upper_bound=bytes_out.upper_bound / 1_000_000 + calls.upper_bound * 5,
    )


def _uncertainty_multiplier(confidence: EstimateConfidence) -> float:
    return {EstimateConfidence.HIGH: 1.5, EstimateConfidence.MEDIUM: 3.0, EstimateConfidence.LOW: 10.0}[confidence]


def _lowest(*confidences: EstimateConfidence) -> EstimateConfidence:
    return min(confidences, key=_CONFIDENCE_RANK.__getitem__)


def _clamp_product(*values: float) -> float:
    result = 1.0
    for value in values:
        result *= min(1.0, max(0.0, value))
    return result
