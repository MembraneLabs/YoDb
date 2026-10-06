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
- Semantic filter: verify-everything and vector-shortlist plans, budgets, a
  per-record report; vectors in the same source or in a separate vector store.
- `yodb` command (`catalog`, `validate`, `explain`, `query`) and the
  `yodb.connect` front door; connection references from `YODB_CONN_<REF>`.
- Read-only sessions, a whole-query time budget with database statement timeouts,
  fail-fast connection errors, bounded filter depth and size.
- Unit tests and end-to-end suites against real PostgreSQL (hand-written cases,
  a generated matrix against a SQL oracle, robustness, real-data semantic runs).

### Known limits
See `docs/reference/limits.mdx`: no cursor paging, no joins or aggregation,
in-memory assembly under row guards, at most 1,000 IDs to restrict a read,
text ordering across sources by code point, PostgreSQL only.
