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

The initial runtime reads and statically validates a three-file YAML catalog.
It does not yet connect to source databases or execute queries.

The Mintlify-ready documentation site lives in [docs/](docs/).
