# YoDb

YoDb is a planned read-only, typed, explainable federated data layer for
relational, semantic, and bounded graph queries. Its initial implementation
will target PostgreSQL, pgvector, and optional Neo4j without migrating source
data.

The active design and delivery references are under [plans/](plans/):

- [V0.1 federated semantic data layer](plans/v0.1-federated-semantic-data-layer.md)
- [V0.1 implementation plan](plans/v0.1-implementation-plan.md)
- [Phase 0 logical semantics and catalog decisions](plans/phase-0-semantics.md)
- [V0.1 YAML catalog schema](plans/v0.1-yaml-catalog-schema.md)

The current runtime strictly loads and validates a three-file YAML catalog. It
also includes a read-only PostgreSQL connection pool, schema inspector, and
catalog validator. Typed query execution, semantic search execution, and graph
execution are not implemented yet.

Documentation content lives in [docs/](docs/). The separate, dependency-free
static site module is in [docs-site/](docs-site/).
