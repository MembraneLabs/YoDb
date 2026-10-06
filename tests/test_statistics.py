"""Statistics: selectivity estimates, observations, the service, and the Postgres provider."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from threading import Thread

from yodb.catalog import SourceKind
from yodb.planning.postgres_statistics import PostgresStatisticsProvider
from yodb.planning.statistics import (
    ColumnStatistics,
    CostDefaults,
    estimate_selectivity,
    filter_signature,
    ObservationStore,
    ScanKey,
    SelectivityDefaults,
    SourceStatistics,
    StatisticsService,
)
from yodb.query import bind_query, parse_query, resolve_query_sources

from support.tickets import ticket_catalog as _ticket_catalog


ACTIVE = _ticket_catalog()


def where(raw):
    """Bind a filter over the ticket dataset (subject: string, priority: int)."""

    query = {"from": {"dataset": "ticket"}, "select": ["subject", "priority"], "where": raw}
    bound = bind_query(parse_query(query), ACTIVE)
    return bound.where


def p(field, op, value=None):
    leaf = {"field": field, "op": op}
    if op not in ("is_null", "is_not_null"):
        leaf["value"] = value
    return leaf


def source():
    query = {"from": {"dataset": "ticket"}, "select": ["subject", "priority"], "where": p("priority", "gte", 1)}
    return resolve_query_sources(bind_query(parse_query(query), ACTIVE), ACTIVE).sources[0]


def sel(raw, columns=None, rows=None, **kwargs):
    return estimate_selectivity(where(raw), columns or {}, row_count=rows, **kwargs)


class SelectivityTests(unittest.TestCase):
    def test_equality_uses_distinct_count_and_scales_by_non_null_fraction(self) -> None:
        cols = {"subject": ColumnStatistics(distinct_count=50, null_fraction=0.2)}
        self.assertAlmostEqual(sel(p("subject", "eq", "x"), cols), 0.8 / 50)
        self.assertAlmostEqual(sel(p("subject", "eq", "x"), {"subject": ColumnStatistics(distinct_count=4)}), 0.25)

    def test_missing_statistics_fall_back_to_documented_defaults(self) -> None:
        d = SelectivityDefaults()
        self.assertAlmostEqual(sel(p("subject", "eq", "x")), d.equality)
        self.assertAlmostEqual(sel(p("priority", "gt", 3)), d.range)
        self.assertAlmostEqual(sel(p("subject", "is_null")), d.is_null)
        self.assertAlmostEqual(sel(p("subject", "contains", "a")), d.text_match)
        self.assertAlmostEqual(sel(p("subject", "eq", "x"), defaults=SelectivityDefaults(equality=0.5)), 0.5)

    def test_in_and_not_in_count_their_values_and_never_exceed_the_non_null_fraction(self) -> None:
        cols = {"priority": ColumnStatistics(distinct_count=10, null_fraction=0.0)}
        self.assertAlmostEqual(sel(p("priority", "in", [1, 2, 3]), cols), 0.3)
        self.assertAlmostEqual(sel(p("priority", "not_in", [1, 2, 3]), cols), 0.7)
        self.assertAlmostEqual(sel(p("priority", "in", list(range(40))), cols), 1.0)  # capped
        self.assertAlmostEqual(sel(p("priority", "not_in", list(range(40))), cols), 0.0)

    def test_not_equal_excludes_nulls(self) -> None:
        cols = {"subject": ColumnStatistics(distinct_count=4, null_fraction=0.5)}
        self.assertAlmostEqual(sel(p("subject", "ne", "x"), cols), 0.5 - 0.5 / 4)

    def test_null_tests_use_the_measured_fraction(self) -> None:
        cols = {"subject": ColumnStatistics(null_fraction=0.3)}
        self.assertAlmostEqual(sel(p("subject", "is_null"), cols), 0.3)
        self.assertAlmostEqual(sel(p("subject", "is_not_null"), cols), 0.7)

    def test_ranges_interpolate_between_known_bounds_and_clamp(self) -> None:
        cols = {"priority": ColumnStatistics(minimum=0, maximum=10, null_fraction=0.0)}
        self.assertAlmostEqual(sel(p("priority", "gte", 3), cols), 0.7)
        self.assertAlmostEqual(sel(p("priority", "lt", 3), cols), 0.3)
        self.assertAlmostEqual(sel(p("priority", "gt", 99), cols), 0.0)   # above the max
        self.assertAlmostEqual(sel(p("priority", "lt", 99), cols), 1.0)
        flat = {"priority": ColumnStatistics(minimum=5, maximum=5)}        # no spread: default
        self.assertAlmostEqual(sel(p("priority", "gt", 1), flat), SelectivityDefaults().range)

    def test_combinators_assume_independence(self) -> None:
        cols = {"subject": ColumnStatistics(distinct_count=10, null_fraction=0.0), "priority": ColumnStatistics(distinct_count=5, null_fraction=0.0)}
        a, b = p("subject", "eq", "x"), p("priority", "eq", 1)
        self.assertAlmostEqual(sel({"all": [a, b]}, cols), 0.1 * 0.2)
        self.assertAlmostEqual(sel({"any": [a, b]}, cols), 1 - 0.9 * 0.8)
        self.assertAlmostEqual(sel({"not": a}, cols), 0.9)

    def test_a_possible_match_never_estimates_below_one_row_but_zero_stays_zero(self) -> None:
        cols = {"subject": ColumnStatistics(distinct_count=1_000_000, null_fraction=0.0)}
        self.assertAlmostEqual(sel(p("subject", "eq", "x"), cols, rows=100), 1 / 100)
        self.assertEqual(sel(p("subject", "eq", "x"), {"subject": ColumnStatistics(null_fraction=1.0)}, rows=100), 0.0)

    def test_no_filter_and_semantic_terms_keep_every_row(self) -> None:
        self.assertEqual(estimate_selectivity(None, {}), 1.0)
        semantic = bind_query(parse_query({"from": {"dataset": "ticket"}, "select": ["subject"], "where": {"semantic": {"field": "body", "proposition": "x"}}}), ACTIVE).where
        self.assertEqual(estimate_selectivity(semantic, {}), 1.0)

    def test_result_is_always_a_probability(self) -> None:
        cols = {"priority": ColumnStatistics(distinct_count=1, null_fraction=0.0)}
        for raw in (p("priority", "eq", 1), {"any": [p("priority", "eq", 1)] * 5}, {"not": p("priority", "in", [1, 2, 3])}):
            self.assertTrue(0.0 <= sel(raw, cols) <= 1.0)


class CommonValueTests(unittest.TestCase):
    """A skewed column: 85% 'placed', 10% 'paid', and 5 other values sharing the remaining 5%."""

    COLUMN = {"subject": ColumnStatistics(distinct_count=7.0, null_fraction=0.0, common_values=(("placed", 0.85), ("paid", 0.10)))}

    def test_a_common_value_has_its_measured_frequency(self) -> None:
        self.assertAlmostEqual(sel(p("subject", "eq", "placed"), self.COLUMN), 0.85)
        self.assertAlmostEqual(sel(p("subject", "ne", "placed"), self.COLUMN), 0.15)

    def test_any_other_value_shares_what_the_common_values_leave(self) -> None:
        self.assertAlmostEqual(sel(p("subject", "eq", "returned"), self.COLUMN), 0.05 / 5)

    def test_in_adds_the_frequencies_and_not_in_is_the_rest(self) -> None:
        self.assertAlmostEqual(sel(p("subject", "in", ["placed", "paid"]), self.COLUMN), 0.95)
        self.assertAlmostEqual(sel(p("subject", "not_in", ["placed", "paid"]), self.COLUMN), 0.05)

    def test_without_common_values_the_uniform_estimate_is_unchanged(self) -> None:
        plain = {"subject": ColumnStatistics(distinct_count=2.0, null_fraction=0.0)}
        self.assertAlmostEqual(sel(p("subject", "eq", "x"), plain), 0.5)

    def test_when_every_distinct_value_is_common_an_unlisted_value_is_nearly_impossible(self) -> None:
        full = {"subject": ColumnStatistics(distinct_count=2.0, null_fraction=0.0, common_values=(("a", 0.75), ("b", 0.25)))}
        self.assertLess(sel(p("subject", "eq", "zzz"), full, rows=1000), 0.01)

    def test_numbers_compare_by_value(self) -> None:
        numeric = {"priority": ColumnStatistics(distinct_count=5.0, null_fraction=0.0, common_values=((5, 0.6),))}
        self.assertAlmostEqual(sel(p("priority", "eq", 5), numeric), 0.6)
        self.assertAlmostEqual(sel(p("priority", "eq", 4), numeric), 0.4 / 4)      # not common: the rest, over the other values

    def test_frequencies_cannot_sum_above_one(self) -> None:
        with self.assertRaises(ValueError):
            ColumnStatistics(common_values=(("a", 0.7), ("b", 0.7)))


class HistogramTests(unittest.TestCase):
    """A numeric column from 0 to 1000, evenly spread (a 10-bucket histogram), 10% null."""

    EVEN = {"priority": ColumnStatistics(distinct_count=1001.0, null_fraction=0.1, histogram=tuple(float(x) for x in range(0, 1001, 100)))}
    SKEWED = {"priority": ColumnStatistics(distinct_count=500.0, null_fraction=0.0, histogram=(0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 1000.0))}

    def test_a_range_is_the_fraction_of_the_histogram_it_covers_of_the_non_null_rows(self) -> None:
        self.assertAlmostEqual(sel(p("priority", "lt", 500), self.EVEN), 0.5 * 0.9)
        self.assertAlmostEqual(sel(p("priority", "gte", 950), self.EVEN), 0.05 * 0.9)
        self.assertAlmostEqual(sel(p("priority", "gt", 100), self.EVEN), 0.9 * 0.9)

    def test_outside_the_histogram_is_none_or_all(self) -> None:
        self.assertAlmostEqual(sel(p("priority", "lt", -5), self.EVEN), 0.0)
        self.assertAlmostEqual(sel(p("priority", "lte", 5000), self.EVEN), 0.9)
        self.assertAlmostEqual(sel(p("priority", "gt", 5000), self.EVEN), 0.0)

    def test_equal_population_buckets_make_a_skewed_column_estimate_by_where_the_rows_are_not_by_the_value_range(self) -> None:
        # nine buckets hold 90% of the rows below 9; the last bucket spreads the remaining 10% up to 1000
        self.assertAlmostEqual(sel(p("priority", "lt", 4), self.SKEWED), 4 / 9)
        self.assertLess(sel(p("priority", "gte", 500), self.SKEWED), 0.06)
        self.assertGreater(sel(p("priority", "gte", 500), self.SKEWED), 0.04)

    def test_common_values_and_the_histogram_of_the_rest_combine(self) -> None:
        column = {"priority": ColumnStatistics(distinct_count=100.0, null_fraction=0.0, common_values=((5, 0.4),), histogram=(0.0, 50.0, 100.0))}
        # 40% are exactly 5; the other 60% spread evenly over 0..100
        self.assertAlmostEqual(sel(p("priority", "lt", 50), column), 0.4 + 0.6 * 0.5)
        self.assertAlmostEqual(sel(p("priority", "gt", 50), column), 0.6 * 0.5)

    def test_without_a_histogram_the_older_estimates_are_unchanged(self) -> None:
        bounded = {"priority": ColumnStatistics(null_fraction=0.0, minimum=0.0, maximum=100.0)}
        self.assertAlmostEqual(sel(p("priority", "lt", 25), bounded), 0.25)
        self.assertAlmostEqual(sel(p("priority", "lt", 25), {}), 1 / 3)

    def test_a_histogram_must_be_ascending_and_have_two_bounds(self) -> None:
        for bounds in ((1.0,), (3.0, 2.0)):
            with self.assertRaises(ValueError):
                ColumnStatistics(histogram=bounds)


class ValueValidationTests(unittest.TestCase):
    def test_nonsense_statistics_are_rejected(self) -> None:
        for build in (
            lambda: ColumnStatistics(null_fraction=1.5),
            lambda: ColumnStatistics(distinct_count=-1),
            lambda: ColumnStatistics(minimum=5, maximum=1),
            lambda: SourceStatistics(row_count=float("nan")),
            lambda: SourceStatistics(call_latency_ms=-1),
            lambda: CostDefaults(per_row_latency_ms=-0.1),
            lambda: SelectivityDefaults(equality=2),
        ):
            with self.assertRaises(ValueError):
                build()

    def test_unknown_is_expressible_everywhere(self) -> None:
        stats = SourceStatistics()
        self.assertIsNone(stats.row_count)
        self.assertEqual(dict(stats.columns), {})


class ObservationStoreTests(unittest.TestCase):
    KEY = ScanKey("crm", "crm.accounts", "")

    def test_values_move_toward_new_observations(self) -> None:
        store = ObservationStore(smoothing=0.5)
        store.record(self.KEY, 100)
        self.assertEqual(store.lookup(self.KEY), 100)
        store.record(self.KEY, 200)
        self.assertEqual(store.lookup(self.KEY), 150)
        self.assertIsNone(store.lookup(ScanKey("other", "x", "")))

    def test_the_least_recently_used_key_is_evicted_and_lookups_refresh(self) -> None:
        store = ObservationStore(capacity=2)
        a, b, c = (ScanKey("s", "r", n) for n in "abc")
        store.record(a, 1)
        store.record(b, 2)
        store.lookup(a)                 # a is now fresher than b
        store.record(c, 3)
        self.assertEqual((store.lookup(a), store.lookup(b), store.lookup(c)), (1, None, 3))
        self.assertEqual(len(store), 2)

    def test_nonsense_observations_are_ignored(self) -> None:
        store = ObservationStore()
        for bad in (float("nan"), float("inf"), -1.0):
            store.record(self.KEY, bad)
        self.assertEqual(len(store), 0)

    def test_arguments_are_validated(self) -> None:
        for kwargs in ({"capacity": 0}, {"smoothing": 0}, {"smoothing": 1.5}):
            with self.assertRaises(ValueError):
                ObservationStore(**kwargs)

    def test_concurrent_recording_is_safe(self) -> None:
        store = ObservationStore(capacity=50)
        threads = [Thread(target=lambda n=n: [store.record(ScanKey("s", "r", str(i % 80)), n) for i in range(500)]) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(len(store), 50)


class FilterSignatureTests(unittest.TestCase):
    def test_signature_depends_on_values_and_shape_but_not_term_order(self) -> None:
        a, b = p("subject", "eq", "x"), p("priority", "gte", 3)
        self.assertEqual(filter_signature(where({"all": [a, b]})), filter_signature(where({"all": [b, a]})))
        self.assertNotEqual(filter_signature(where(a)), filter_signature(where(p("subject", "eq", "y"))))
        self.assertNotEqual(filter_signature(where(a)), filter_signature(where(p("subject", "ne", "x"))))
        self.assertEqual(filter_signature(None), "")

    def test_signature_does_not_contain_the_values(self) -> None:
        self.assertNotIn("secret-value", filter_signature(where(p("subject", "eq", "secret-value"))))


class FixedProvider:
    def __init__(self, stats=None, error=None):
        self.stats, self.error, self.calls = stats, error, 0

    def statistics(self, source):
        self.calls += 1
        if self.error:
            raise self.error
        return self.stats


def service(provider=None, observations=None, **kwargs):
    providers = {} if provider is None else {SourceKind.POSTGRES: provider}
    return StatisticsService(providers, observations=observations, **kwargs)


class StatisticsServiceTests(unittest.TestCase):
    STATS = SourceStatistics(row_count=1000, columns={"priority": ColumnStatistics(distinct_count=5, null_fraction=0.0)})

    def test_nothing_known_is_reported_as_unknown(self) -> None:
        for svc in (service(), service(FixedProvider(None)), service(FixedProvider(error=RuntimeError("down")))):
            estimate = svc.estimate_scan(source(), where(p("priority", "eq", 1)))
            self.assertFalse(estimate.known)
            self.assertIsNone(estimate.filtered_rows)

    def test_provider_statistics_drive_the_estimate(self) -> None:
        estimate = service(FixedProvider(self.STATS)).estimate_scan(source(), where(p("priority", "eq", 1)))
        self.assertTrue(estimate.known)
        self.assertEqual(estimate.total_rows, 1000)
        self.assertAlmostEqual(estimate.selectivity, 0.2)
        self.assertAlmostEqual(estimate.filtered_rows, 200)
        self.assertFalse(estimate.observed)

    def test_an_unfiltered_scan_keeps_every_row(self) -> None:
        estimate = service(FixedProvider(self.STATS)).estimate_scan(source(), None)
        self.assertEqual((estimate.selectivity, estimate.filtered_rows), (1.0, 1000))

    def test_observations_override_the_provider(self) -> None:
        store = ObservationStore()
        svc = service(FixedProvider(self.STATS), store)
        src, flt = source(), where(p("priority", "eq", 1))
        svc.observe(src, None, 4000)     # the table is really 4,000 rows
        svc.observe(src, flt, 40)        # and this filter really kept 40
        estimate = svc.estimate_scan(src, flt)
        self.assertEqual((estimate.total_rows, estimate.filtered_rows), (4000, 40))
        self.assertAlmostEqual(estimate.selectivity, 40 / 4000)
        self.assertTrue(estimate.observed)
        other = svc.estimate_scan(src, where(p("priority", "eq", 2)))  # a different filter value
        self.assertAlmostEqual(other.filtered_rows, 4000 * 0.2)       # uses the observed total only

    def test_observations_alone_do_not_make_a_source_known_without_its_size(self) -> None:
        svc = service(None, ObservationStore())
        svc.observe(source(), where(p("priority", "eq", 1)), 10)
        self.assertFalse(svc.estimate_scan(source(), where(p("priority", "eq", 1))).known)

    def test_latency_hints_come_from_the_provider_else_the_defaults(self) -> None:
        plain = service(FixedProvider(self.STATS)).estimate_scan(source(), None).profile
        self.assertEqual((plain.call_latency_ms, plain.per_row_latency_ms), (CostDefaults().call_latency_ms, CostDefaults().per_row_latency_ms))
        hinted = SourceStatistics(row_count=1, call_latency_ms=40.0)
        profile = service(FixedProvider(hinted)).estimate_scan(source(), None).profile
        self.assertEqual(profile.call_latency_ms, 40.0)
        self.assertEqual(profile.per_key_latency_ms, CostDefaults().per_key_latency_ms)

    def test_observing_without_a_store_is_a_no_op(self) -> None:
        service().observe(source(), None, 5)  # must not raise


class FakeConnections:
    source_kind = SourceKind.POSTGRES

    def __init__(self, reltuples=1000.0, columns=(), error=None, table_missing=False):
        self.reltuples, self.columns, self.error, self.table_missing = reltuples, columns, error, table_missing
        self.executed = []

    @contextmanager
    def acquire(self, connection_ref, *, timeout_seconds=None):
        if self.error:
            raise self.error
        yield self

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, params):
        self.executed.append((sql, params))
        self._sql = sql

    def fetchall(self):
        if "stats_rows" in self._sql:
            return [] if self.table_missing else [(self.reltuples,)]
        return list(self.columns)


class PostgresProviderTests(unittest.TestCase):
    def provider(self, connections, **kwargs):
        self.now = 0.0
        return PostgresStatisticsProvider(connections, clock=lambda: self.now, **kwargs)

    def test_reads_row_count_and_maps_physical_columns_to_logical_fields(self) -> None:
        connections = FakeConnections(1000.0, [("priority", 5.0, 0.1, None, None, None), ("not_queried", 3.0, 0.0, None, None, None)])
        stats = self.provider(connections).statistics(source())
        self.assertEqual(stats.row_count, 1000.0)
        self.assertEqual(stats.columns["priority"], ColumnStatistics(distinct_count=5.0, null_fraction=0.1))
        self.assertNotIn("not_queried", stats.columns)
        rows_sql, rows_params = connections.executed[0]
        self.assertEqual(rows_params, ('"public"."tickets"',))   # quoted, schema-qualified, parameterized

    def test_the_whole_tables_statistics_are_cached_not_just_the_columns_of_the_first_query(self) -> None:
        # Regression: the first query to touch a table used to decide which columns every later query got,
        # so a later filter on another column silently fell back to a default selectivity.
        connections = FakeConnections(1000.0, [("priority", 5.0, 0.0, None, None, None), ("subject", 40.0, 0.0, None, None, None)])
        provider = self.provider(connections)
        first = resolve_query_sources(
            bind_query(parse_query({"from": {"dataset": "ticket"}, "select": ["priority"], "where": p("priority", "gte", 1)}), ACTIVE), ACTIVE
        ).sources[0]
        second = resolve_query_sources(
            bind_query(parse_query({"from": {"dataset": "ticket"}, "select": ["subject"], "where": p("subject", "eq", "x")}), ACTIVE), ACTIVE
        ).sources[0]
        self.assertNotIn("subject", provider.statistics(first).columns)          # this query does not use it
        self.assertEqual(provider.statistics(second).columns["subject"].distinct_count, 40.0)
        self.assertEqual(len([e for e in connections.executed if "stats_rows" in e[0]]), 1)   # one read served both

    def test_most_common_values_are_read_and_typed_by_the_logical_field(self) -> None:
        connections = FakeConnections(1000.0, [
            ("subject", 40.0, 0.0, ["S1", "S2"], [0.5, 0.25], None),
            ("priority", 5.0, 0.0, ["5", "3"], [0.6, 0.2], None),
        ])
        stats = self.provider(connections).statistics(source())
        self.assertEqual(stats.columns["subject"].common_values, (("S1", 0.5), ("S2", 0.25)))
        self.assertEqual(stats.columns["priority"].common_values, ((5, 0.6), (3, 0.2)))

    def test_histogram_bounds_are_read_typed_and_ordered_numbers_with_timestamps_as_epoch_seconds(self) -> None:
        connections = FakeConnections(1000.0, [("priority", 5.0, 0.0, None, None, ["1", "10", "100"])])
        stats = self.provider(connections).statistics(source())
        self.assertEqual(stats.columns["priority"].histogram, (1.0, 10.0, 100.0))

    def test_a_histogram_that_cannot_be_read_as_numbers_is_left_out_not_guessed(self) -> None:
        connections = FakeConnections(1000.0, [("priority", 5.0, 0.0, None, None, ["a", "b"]), ("subject", 5.0, 0.0, None, None, ["x", "y"])])
        stats = self.provider(connections).statistics(source())
        self.assertEqual(stats.columns["priority"].histogram, ())
        self.assertEqual(stats.columns["subject"].histogram, ())       # text has no numeric order to interpolate

    def test_a_common_value_that_cannot_be_typed_is_left_out_rather_than_compared_wrongly(self) -> None:
        connections = FakeConnections(1000.0, [("priority", 5.0, 0.0, ["five", "3"], [0.6, 0.2], None)])
        stats = self.provider(connections).statistics(source())
        self.assertEqual(stats.columns["priority"].common_values, ((3, 0.2),))

    def test_negative_n_distinct_is_a_fraction_of_the_rows_and_zero_is_unknown(self) -> None:
        connections = FakeConnections(2000.0, [("priority", -0.5, 0.0, None, None, None), ("subject", 0.0, 0.0, None, None, None)])
        stats = self.provider(connections).statistics(source())
        self.assertEqual(stats.columns["priority"].distinct_count, 1000.0)
        self.assertIsNone(stats.columns["subject"].distinct_count)

    def test_never_analyzed_or_missing_tables_and_failures_are_unknown(self) -> None:
        for connections in (FakeConnections(-1.0), FakeConnections(None), FakeConnections(table_missing=True), FakeConnections(error=RuntimeError("down"))):
            self.assertIsNone(self.provider(connections).statistics(source()))

    def test_results_are_cached_until_the_ttl_and_failures_are_cached_too(self) -> None:
        connections = FakeConnections(10.0)
        provider = self.provider(connections, ttl_seconds=60)
        provider.statistics(source())
        provider.statistics(source())
        self.assertEqual(len([e for e in connections.executed if "stats_rows" in e[0]]), 1)
        self.now = 61.0
        provider.statistics(source())
        self.assertEqual(len([e for e in connections.executed if "stats_rows" in e[0]]), 2)
        failing = FakeConnections(error=RuntimeError("down"))
        provider = self.provider(failing)
        provider.statistics(source())
        provider.statistics(source())  # the failure is remembered, not retried every plan
        provider.invalidate()

    def test_a_failure_leaves_a_diagnosable_trace_without_the_message(self) -> None:
        provider = self.provider(FakeConnections(error=RuntimeError("password=hunter2")))
        self.assertIsNone(provider.statistics(source()))
        self.assertEqual(provider.last_error, "RuntimeError")

    def test_every_parameter_the_server_must_type_is_cast(self) -> None:
        # An uncast `%s IS NULL` is "could not determine data type of parameter" on a real server,
        # which a fake connection cannot show; the end-to-end run exercises the real thing.
        from yodb.planning.postgres_statistics import _COLUMNS_SQL

        self.assertIn("%s::text IS NULL", _COLUMNS_SQL)

    def test_invalid_arguments_are_rejected(self) -> None:
        for kwargs in ({"ttl_seconds": -1}, {"timeout_seconds": 0}):
            with self.assertRaises(ValueError):
                PostgresStatisticsProvider(FakeConnections(), **kwargs)


if __name__ == "__main__":
    unittest.main()
