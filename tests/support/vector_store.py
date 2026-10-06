"""Tickets whose embeddings live in a separate vector-store source (no database needed)."""

from __future__ import annotations

from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from yodb.catalog import SourceKind, load_catalog
from yodb.inspection import SourceInspection, SourceValidationReport
from yodb.runtime import CatalogEvaluation, SourceRuntimeState, SourceRuntimeStatus

from support.tickets import DATASETS, RELATIONS

SOURCES = """\
api_version: yodb/v0.1
sources:
  helpdesk:
    kind: postgres
    connection_ref: helpdesk
    read_only: true
    datasets:
      ticket:
        resource: public.tickets
        identity: [id]
        fields:
          id: {physical_name: id}
          subject: {physical_name: subject}
          body: {physical_name: body}
          priority: {physical_name: priority}
  directory:
    kind: postgres
    connection_ref: directory
    read_only: true
    datasets:
      ticket:
        resource: public.owners
        identity: [id]
        fields:
          id: {physical_name: ticket_id}
          owner: {physical_name: owner}
          memo: {physical_name: memo}
  vectors:
    kind: postgres
    connection_ref: vectors
    read_only: true
    datasets:
      ticket:
        resource: vec.ticket_vectors
        identity: [id]
        fields:
          id: {physical_name: ticket_id}
        embeddings:
          body: {column: embedding, model: embed-v1, dimensions: 3, metric: cosine}
resolution:
  ticket:
    identity_source: helpdesk
    field_sources: {id: helpdesk, subject: helpdesk, body: helpdesk, priority: helpdesk, owner: directory, memo: directory}
"""

_NOW = datetime(2026, 10, 3, tzinfo=UTC)


def store_catalog(sources: str = SOURCES) -> CatalogEvaluation:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for name, text in (("datasets", DATASETS), ("sources", sources), ("relations", RELATIONS)):
            (root / f"{name}.yaml").write_text(text, encoding="utf-8")
        catalog = load_catalog(root)
    return CatalogEvaluation(
        catalog=catalog,
        evaluated_at=_NOW,
        sources={
            name: SourceRuntimeState(
                source_name=name,
                status=SourceRuntimeStatus.VALID,
                inspection=SourceInspection(source_name=name, source_kind=source.kind, inspected_at=_NOW),
                validation=SourceValidationReport(source_name=name, inspected_at=_NOW),
            )
            for name, source in catalog.sources.items()
        },
    )


def cosine_distance(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = sqrt(sum(x * x for x in a)) * sqrt(sum(y * y for y in b))
    return 1.0 - (dot / norm if norm else 0.0)


_IN_LIST = re.compile(r"IN \(((?:%s(?:, )?)+)\)")


class VectorStoreExecutor:
    """Serves record sources and a vector store, honouring key restrictions and ranking by cosine."""

    source_kind = SourceKind.POSTGRES

    def __init__(self, records: dict[str, tuple[dict, ...]], vectors: dict[str, tuple[float, ...]]) -> None:
        self.records = records
        self.vectors = vectors
        self.queries = []

    def execute(self, query, *, timeout_seconds=None):
        self.queries.append(query)
        match = _IN_LIST.search(query.sql)
        parameters = list(query.parameters)
        count = match.group(1).count("%s") if match else 0
        if query.source_name == "vectors":
            # parameters: the key restriction (if any), then the query vector, then the limit
            keys = set(parameters[-2 - count:-2]) if match else None
            vector = [float(x) for x in parameters[-2].strip("[]").split(",")]
            candidates = [(i, v) for i, v in self.vectors.items() if keys is None or i in keys]
            ranked = sorted(candidates, key=lambda item: (cosine_distance(vector, item[1]), item[0]))
            return tuple({"id": i} for i, _ in ranked[: parameters[-1]])
        keys = set(parameters[-1 - count:-1]) if match else None      # the limit is last
        rows = self.records[query.source_name]
        return tuple(row for row in rows if keys is None or row["id"] in keys)

    def sql_for(self, source: str) -> list[str]:
        return [q.sql for q in self.queries if q.source_name == source]

    def ids_read(self, source: str) -> list[tuple]:
        """The key restriction of each read of ``source`` (empty when it was not restricted)."""

        reads = []
        for query in self.queries:
            if query.source_name != source:
                continue
            match = _IN_LIST.search(query.sql)
            count = match.group(1).count("%s") if match else 0
            end = -2 if source == "vectors" else -1
            reads.append(tuple(query.parameters[end - count:end]) if count else ())
        return reads
