"""Statistics the planner may use to estimate work, and where they come from.

Three layers keep this extensible:

* a :class:`StatisticsProvider` per source kind reports facts about one source
  representation (row count, per-column distinct/null counts, latency hints).
  A new database writes its own provider; nothing else changes;
* an :class:`ObservationStore` remembers what scans really returned, so
  estimates improve as queries run;
* :class:`StatisticsService` combines them (observation > provider > default)
  and is the only thing the optimizer talks to.

Statistics can only ever influence *which valid plan* runs, never what a plan
means.  A missing, stale or failing provider must degrade to "unknown" (the
planner then uses its fixed rules); it must never fail a query.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
import json
import math
from threading import Lock
from typing import Protocol, runtime_checkable

from ..catalog import SourceKind
from ..query.models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundFilterExpression,
    BoundExtensionTerm,
    BoundNotExpression,
    BoundPredicate,
    ComparisonOperator,
)
from ..query.resolution import SingleSourceQueryBinding


def _check_fraction(name: str, value: float | None) -> None:
    if value is not None and not (math.isfinite(value) and 0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be within [0, 1]")


def _check_non_negative(name: str, value: float | None) -> None:
    if value is not None and not (math.isfinite(value) and value >= 0):
        raise ValueError(f"{name} must be a finite non-negative number")


@dataclass(frozen=True)
class ColumnStatistics:
    """What is known about one logical field of one source (all optional)."""

    distinct_count: float | None = None
    null_fraction: float | None = None
    minimum: float | None = None   # numeric (timestamps as epoch seconds) for range estimates
    maximum: float | None = None
    # The most common values and the fraction of *all* rows each one has (skewed columns:
    # a status that is 85% "placed" is not "1 of 5 values").
    common_values: tuple[tuple[object, float], ...] = ()

    def __post_init__(self) -> None:
        _check_non_negative("distinct_count", self.distinct_count)
        _check_fraction("null_fraction", self.null_fraction)
        for _, frequency in self.common_values:
            _check_fraction("common value frequency", frequency)
        if sum(frequency for _, frequency in self.common_values) > 1.0 + 1e-6:
            raise ValueError("common value frequencies must not sum above 1")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")


@dataclass(frozen=True)
class SourceStatistics:
    """Facts about one source representation.  ``None`` always means unknown."""

    row_count: float | None = None
    columns: Mapping[str, ColumnStatistics] = field(default_factory=dict)  # keyed by logical field name
    call_latency_ms: float | None = None
    per_row_latency_ms: float | None = None
    per_key_latency_ms: float | None = None

    def __post_init__(self) -> None:
        for name in ("row_count", "call_latency_ms", "per_row_latency_ms", "per_key_latency_ms"):
            _check_non_negative(name, getattr(self, name))


@runtime_checkable
class StatisticsProvider(Protocol):
    """Reports :class:`SourceStatistics` for one source kind.

    Return ``None`` (or raise) when nothing is known; the service treats both as
    "unknown".  Implementations should cache: this is called while planning.
    """

    def statistics(self, source: SingleSourceQueryBinding) -> SourceStatistics | None: ...


@dataclass(frozen=True)
class CostDefaults:
    """Latency assumed when neither a provider nor an observation says otherwise."""

    call_latency_ms: float = 5.0
    per_row_latency_ms: float = 0.01
    per_key_latency_ms: float = 0.002

    def __post_init__(self) -> None:
        for name in ("call_latency_ms", "per_row_latency_ms", "per_key_latency_ms"):
            _check_non_negative(name, getattr(self, name))


@dataclass(frozen=True)
class SourceCostProfile:
    """Fully resolved per-source latency parameters (no unknowns)."""

    call_latency_ms: float
    per_row_latency_ms: float
    per_key_latency_ms: float


@dataclass(frozen=True)
class SelectivityDefaults:
    """Fractions assumed when a column's statistics are missing."""

    equality: float = 0.05
    range: float = 1 / 3
    is_null: float = 0.02
    text_match: float = 0.1

    def __post_init__(self) -> None:
        for name in ("equality", "range", "is_null", "text_match"):
            _check_fraction(name, getattr(self, name))


@dataclass(frozen=True)
class ScanEstimate:
    """Estimated size of one source's scan under its pushed filter."""

    total_rows: float | None        # None: the source's size is unknown
    selectivity: float              # fraction of rows the pushed filter keeps
    filtered_rows: float | None
    profile: SourceCostProfile
    observed: bool = False          # filtered_rows came from a past execution

    @property
    def known(self) -> bool:
        return self.total_rows is not None


# --- selectivity -------------------------------------------------------------------

def estimate_selectivity(
    expression: BoundFilterExpression | None,
    columns: Mapping[str, ColumnStatistics],
    *,
    row_count: float | None = None,
    defaults: SelectivityDefaults = SelectivityDefaults(),
) -> float:
    """Fraction of rows ``expression`` is expected to keep, in [0, 1].

    Leaves are estimated from column statistics when present and from
    ``defaults`` otherwise; combinators assume independence.  A filter that is
    not provably empty never estimates below one row, so a mis-estimate cannot
    make a later step look free.
    """

    value = _selectivity(expression, columns, defaults)
    value = min(1.0, max(0.0, value))
    if row_count is not None and row_count > 0 and 0.0 < value < 1.0 / row_count:
        value = 1.0 / row_count
    return value


def _selectivity(
    expression: BoundFilterExpression | None,
    columns: Mapping[str, ColumnStatistics],
    defaults: SelectivityDefaults,
) -> float:
    if expression is None or isinstance(expression, BoundExtensionTerm):
        return 1.0  # an extension term is costed by its own operator, not here
    if isinstance(expression, BoundPredicate):
        return _predicate_selectivity(expression, columns.get(expression.field.name), defaults)
    if isinstance(expression, BoundAllExpression):
        result = 1.0
        for child in expression.expressions:
            result *= _selectivity(child, columns, defaults)
        return result
    if isinstance(expression, BoundAnyExpression):
        miss = 1.0
        for child in expression.expressions:
            miss *= 1.0 - min(1.0, max(0.0, _selectivity(child, columns, defaults)))
        return 1.0 - miss
    if isinstance(expression, BoundNotExpression):
        return 1.0 - min(1.0, max(0.0, _selectivity(expression.expression, columns, defaults)))
    raise AssertionError(f"Unknown bound expression: {expression!r}")


def _predicate_selectivity(
    predicate: BoundPredicate,
    column: ColumnStatistics | None,
    defaults: SelectivityDefaults,
) -> float:
    op = predicate.operator
    null_fraction = column.null_fraction if column and column.null_fraction is not None else None
    non_null = 1.0 - (null_fraction if null_fraction is not None else 0.0)
    if op is ComparisonOperator.IS_NULL:
        return null_fraction if null_fraction is not None else defaults.is_null
    if op is ComparisonOperator.IS_NOT_NULL:
        return non_null if null_fraction is not None else 1.0 - defaults.is_null
    ndv = column.distinct_count if column and column.distinct_count else None
    equal = non_null / ndv if ndv else defaults.equality * non_null
    common = column.common_values if column else ()
    if op is ComparisonOperator.EQ:
        return _equality(predicate.value, common, ndv, non_null, equal)
    if op is ComparisonOperator.NE:
        return max(0.0, non_null - _equality(predicate.value, common, ndv, non_null, equal))
    if op in (ComparisonOperator.IN, ComparisonOperator.NOT_IN):
        values = predicate.value if isinstance(predicate.value, (tuple, list)) else (predicate.value,)
        member = min(non_null, sum(_equality(v, common, ndv, non_null, equal) for v in values))
        return member if op is ComparisonOperator.IN else max(0.0, non_null - member)
    if op in (ComparisonOperator.CONTAINS, ComparisonOperator.STARTS_WITH):
        return defaults.text_match * non_null
    # Range comparisons: interpolate between known bounds, else a flat default.
    fraction = defaults.range
    low, high = (column.minimum, column.maximum) if column else (None, None)
    number = _as_number(predicate.value)
    if low is not None and high is not None and number is not None and high > low:
        below = min(1.0, max(0.0, (number - low) / (high - low)))
        fraction = below if op in (ComparisonOperator.LT, ComparisonOperator.LTE) else 1.0 - below
    return fraction * non_null


def _equality(
    value: object,
    common: tuple[tuple[object, float], ...],
    ndv: float | None,
    non_null: float,
    uniform: float,
) -> float:
    """Fraction of rows equal to ``value``: its measured frequency if it is a common value,
    else what the common values leave, spread over the remaining distinct values."""

    if not common:
        return uniform
    for candidate, frequency in common:
        if _same(candidate, value):
            return frequency
    remaining = max(0.0, non_null - sum(frequency for _, frequency in common))
    others = (ndv - len(common)) if ndv else None
    if others is not None and others >= 1:
        return remaining / others
    return min(uniform, remaining)       # every distinct value is common and this one is not among them


def _same(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.timestamp()
    return None


# --- observations (feedback from executed queries) ------------------------------------

@dataclass(frozen=True)
class ScanKey:
    """Identifies "this source scanned with this filter" without storing values."""

    source_name: str
    resource: str
    filter_signature: str  # "" for an unfiltered scan


def filter_signature(expression: BoundFilterExpression | None) -> str:
    """A stable digest of a filter's shape *and* values (values are never stored)."""

    if expression is None:
        return ""
    return sha256(json.dumps(_shape(expression), sort_keys=True, default=repr).encode()).hexdigest()[:16]


def _shape(expression: BoundFilterExpression) -> object:
    if isinstance(expression, BoundPredicate):
        return {"f": expression.field.name, "o": expression.operator.value, "v": repr(expression.value)}
    if isinstance(expression, BoundExtensionTerm):
        return {"extension": type(expression).__name__}
    if isinstance(expression, BoundAllExpression):
        return {"all": sorted(json.dumps(_shape(c), sort_keys=True) for c in expression.expressions)}
    if isinstance(expression, BoundAnyExpression):
        return {"any": sorted(json.dumps(_shape(c), sort_keys=True) for c in expression.expressions)}
    if isinstance(expression, BoundNotExpression):
        return {"not": _shape(expression.expression)}
    raise AssertionError(f"Unknown bound expression: {expression!r}")


class ObservationStore:
    """A bounded, thread-safe memory of how many rows scans actually returned.

    Each new observation moves the remembered value toward it (exponential
    moving average), so the estimate tracks change without chasing noise.  The
    oldest-used keys are evicted past ``capacity``.
    """

    def __init__(self, *, capacity: int = 1_000, smoothing: float = 0.5) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        if not 0.0 < smoothing <= 1.0:
            raise ValueError("smoothing must be within (0, 1]")
        self._capacity = capacity
        self._smoothing = smoothing
        self._values: OrderedDict[ScanKey, float] = OrderedDict()
        self._lock = Lock()

    def record(self, key: ScanKey, rows: float) -> None:
        if not (math.isfinite(rows) and rows >= 0):
            return
        with self._lock:
            previous = self._values.pop(key, None)
            self._values[key] = rows if previous is None else previous + self._smoothing * (rows - previous)
            while len(self._values) > self._capacity:
                self._values.popitem(last=False)

    def lookup(self, key: ScanKey) -> float | None:
        with self._lock:
            value = self._values.get(key)
            if value is not None:
                self._values.move_to_end(key)
            return value

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)


# --- the service the optimizer uses ------------------------------------------------------

class StatisticsService:
    """Resolves estimates: observation, else provider, else default, else unknown."""

    def __init__(
        self,
        providers: Mapping[SourceKind, StatisticsProvider] | Iterable[tuple[SourceKind, StatisticsProvider]] = (),
        *,
        observations: ObservationStore | None = None,
        cost_defaults: CostDefaults = CostDefaults(),
        selectivity_defaults: SelectivityDefaults = SelectivityDefaults(),
    ) -> None:
        self._providers = dict(providers)
        self._observations = observations
        self._cost_defaults = cost_defaults
        self._selectivity_defaults = selectivity_defaults

    @property
    def observations(self) -> ObservationStore | None:
        return self._observations

    def source_statistics(self, source: SingleSourceQueryBinding) -> SourceStatistics:
        """Provider facts for ``source``; an empty record when unknown or failing."""

        provider = self._providers.get(source.source_kind)
        if provider is None:
            return SourceStatistics()
        try:
            return provider.statistics(source) or SourceStatistics()
        except Exception:  # noqa: BLE001 - statistics must never fail a query
            return SourceStatistics()

    def profile(self, stats: SourceStatistics) -> SourceCostProfile:
        d = self._cost_defaults
        return SourceCostProfile(
            call_latency_ms=d.call_latency_ms if stats.call_latency_ms is None else stats.call_latency_ms,
            per_row_latency_ms=d.per_row_latency_ms if stats.per_row_latency_ms is None else stats.per_row_latency_ms,
            per_key_latency_ms=d.per_key_latency_ms if stats.per_key_latency_ms is None else stats.per_key_latency_ms,
        )

    def estimate_scan(
        self,
        source: SingleSourceQueryBinding,
        pushed_filter: BoundFilterExpression | None,
    ) -> ScanEstimate:
        stats = self.source_statistics(source)
        total = stats.row_count
        observed_total = self._observed(source, None)
        if observed_total is not None:
            total = observed_total          # what a scan really returned beats a catalog estimate
        selectivity = estimate_selectivity(
            pushed_filter, stats.columns, row_count=total, defaults=self._selectivity_defaults
        )
        filtered: float | None = None if total is None else total * selectivity
        observed = False
        if pushed_filter is not None:
            seen = self._observed(source, pushed_filter)
            if seen is not None:
                filtered, observed = seen, True
                if total is not None and total > 0:
                    selectivity = min(1.0, filtered / total)
        elif observed_total is not None:
            observed = True
        return ScanEstimate(total, selectivity, filtered, self.profile(stats), observed)

    def observe(
        self,
        source: SingleSourceQueryBinding,
        pushed_filter: BoundFilterExpression | None,
        rows: int,
    ) -> None:
        """Record that an unrestricted, untruncated scan returned ``rows`` rows."""

        if self._observations is not None:
            self._observations.record(
                ScanKey(source.source_name, source.resource, filter_signature(pushed_filter)), float(rows)
            )

    def _observed(self, source: SingleSourceQueryBinding, expression: BoundFilterExpression | None) -> float | None:
        if self._observations is None:
            return None
        return self._observations.lookup(
            ScanKey(source.source_name, source.resource, filter_signature(expression))
        )
