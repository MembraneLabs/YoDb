# Changelog

## 0.1.1 (unreleased)

### Added
- MCP server: `yodb mcp CATALOG` serves a catalog to an AI agent over the Model Context Protocol
  (standard input/output) with three read-only tools, `describe_catalog`, `query` and `explain`.
  The agent sees public fields and declared relationships only; a refused query is a tool error
  with its code and location. `yodb.mcp_server.serve(db)` serves a client opened from Python,
  including one with a semantic filter. Needs the optional `mcp` extra (`pip install ".[mcp]"`).
- `YoDb.semantic_enabled`: whether a semantic filter was configured. `YoDb.limits`: the bounds in force.
- End-to-end suite for the MCP server (`tests/e2e/run_mcp.py`): a real MCP client against the
  server as a subprocess, every answer compared with an independent oracle.
- `examples/shop`: a three-database sample with a setup script.

### Fixed
- `constraints.maximum_results` was ignored when YoDb itself paged the result (a dataset spread over
  several sources, or a semantic condition); it bounded only single-source queries and joins.
- A join with a semantic condition on the probed side reported the work of its last batch of keys only;
  the report now adds up every batch.

### Changed
- A semantic condition sent to a YoDb with no semantic filter is refused with
  "The query has a semantic condition, but nothing is configured to answer one."
  (it named an internal class before).
- The top-level `yodb` package exports only the public surface: `connect`, `YoDb`, the result and
  explanation types, the catalog loader and the errors. Everything else is imported from its own
  subpackage (for example `from yodb.inspection import PostgresSourceInspector`) and is internal.
- `docs/reference/compatibility.mdx` says which parts are stable, experimental or internal.

### Removed
- `page.after`. It was accepted and then refused with `query_feature_not_supported`; `page` now takes
  `first` only, and `after` is refused as an unknown key (`query_shape_invalid`).
- `constraints.allow_partial_results`. It was accepted and never used.
- The error code `cursor_query_mismatch`. It was never raised.
- Query fingerprints differ from 0.1.0, because the removed option was part of what they covered.

## 0.1.0

First version.

### Added
- Strict three-file YAML catalog, read-only PostgreSQL inspection and validation,
  all-or-nothing activation.
- Typed logical queries over one dataset spread across PostgreSQL sources:
  filters, ordering, paging, assembly by logical ID, key transfer between sources.
- Physical planner built from operators, with source capability negotiation and
  a plan explanation; cost-based read ordering (dynamic programming over
  statistics from `pg_stats` and past scans) with a stated fallback to fixed rules.
- Joins between two datasets over a declared relationship (`traverse`): flat rows, inner or left,
  forward or reverse, filters and ordering on either side, the side expected to be smaller drives
  and the other is read in batches of 1,000 keys; the join is a plan node run by the executor.
- Key restriction in batches (up to 20 batches of 1,000 IDs), whole `any`/`not` conditions pushed to the one
  source that owns their fields, and histogram-based range estimates from `pg_stats`.
- Semantic filter: verify-everything and vector-shortlist plans, budgets, a
  per-record report; vectors in the same source or in a separate vector store.
- `yodb` command (`catalog`, `validate`, `explain`, `query`) and the
  `yodb.connect` front door; connection references from `YODB_CONN_<REF>`.
- Read-only sessions, a whole-query time budget with database statement timeouts,
  fail-fast connection errors, bounded filter depth and size.
- Unit tests and end-to-end suites against real PostgreSQL (hand-written cases,
  a generated matrix against a SQL oracle, robustness, real-data semantic runs).

### Known limits
See `docs/reference/limits.mdx`: no cursor paging, no aggregation, one join step per query,
in-memory joins and assembly under row guards, text ordering across sources by code point,
PostgreSQL only.
