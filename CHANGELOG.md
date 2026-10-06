# Changelog

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
