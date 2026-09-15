from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from yodb import CatalogValidationError, load_catalog


DATASETS = """\
api_version: yodb/v0.1
catalog:
  name: acme_data
  version: 1
datasets:
  customer:
    description: A company with a commercial account.
    fields:
      id: {type: id, description: Stable customer identity.}
      name: {type: string, description: Customer company name.}
      crm_account_key: {type: uuid, description: CRM key, visibility: internal}
  support_ticket:
    description: A customer support case.
    fields:
      id: {type: id, description: Stable ticket identity.}
      customer_crm_key: {type: uuid, description: CRM customer key, visibility: internal}
      content: {type: text, description: Ticket conversation., semantic_eligible: true}
"""

SOURCES = """\
api_version: yodb/v0.1
sources:
  crm_postgres:
    kind: postgres
    connection_ref: secret://yodb/crm-readonly
    read_only: true
    datasets:
      customer:
        resource: public.accounts
        identity: [id]
        fields:
          id: {physical_name: account_uuid}
          name: {physical_name: company_name}
          crm_account_key: {physical_name: account_uuid}
  support_postgres:
    kind: postgres
    connection_ref: secret://yodb/support-readonly
    read_only: true
    datasets:
      support_ticket:
        resource: public.tickets
        identity: [id]
        fields:
          id: {physical_name: ticket_id}
          customer_crm_key: {physical_name: crm_account_uuid}
          content: {physical_name: body}
  relationship_graph:
    kind: neo4j
    connection_ref: secret://yodb/graph-readonly
    read_only: true
    datasets:
      customer:
        resource: Customer
        identity: [crm_account_key]
        fields:
          crm_account_key: {physical_name: crmAccountUuid}
      support_ticket:
        resource: SupportTicket
        identity: [id]
        fields:
          id: {physical_name: ticketId}
resolution:
  customer:
    identity_source: crm_postgres
    field_sources:
      id: crm_postgres
      name: crm_postgres
      crm_account_key: crm_postgres
  support_ticket:
    identity_source: support_postgres
    field_sources:
      id: support_postgres
      customer_crm_key: support_postgres
      content: support_postgres
"""

RELATIONS = """\
api_version: yodb/v0.1
relationships:
  customer_has_ticket:
    from: customer
    to: support_ticket
    description: A ticket associated with a customer.
    cardinality: one_to_many
    implementations:
      - kind: key_match
        from: {source: crm_postgres, field: crm_account_key}
        to: {source: support_postgres, field: customer_crm_key}
      - kind: edge
        source: relationship_graph
        edge: HAS_TICKET
        direction: out
"""


class CatalogLoaderTests(unittest.TestCase):
    def test_loads_complete_three_file_catalog(self) -> None:
        with catalog_directory() as directory:
            catalog = load_catalog(directory)

        self.assertEqual(catalog.metadata.name, "acme_data")
        self.assertEqual(catalog.datasets["customer"].fields["name"].type.value, "string")
        self.assertEqual(
            catalog.sources["crm_postgres"].datasets["customer"].fields["name"].physical_name,
            "company_name",
        )
        self.assertEqual(len(catalog.relationships["customer_has_ticket"].implementations), 2)

    def test_rejects_unknown_physical_field_mapping(self) -> None:
        invalid_sources = SOURCES.replace(
            "          name: {physical_name: company_name}\n",
            "          unknown_field: {physical_name: company_name}\n",
        )
        with catalog_directory(sources=invalid_sources) as directory:
            with self.assertRaisesRegex(CatalogValidationError, "unknown field 'unknown_field'"):
                load_catalog(directory)

    def test_rejects_join_using_unmapped_field(self) -> None:
        invalid_relations = RELATIONS.replace("field: crm_account_key", "field: unapproved_key")
        with catalog_directory(relations=invalid_relations) as directory:
            with self.assertRaisesRegex(CatalogValidationError, "unapproved_key.*not mapped"):
                load_catalog(directory)

    def test_rejects_non_neo4j_edge_source(self) -> None:
        invalid_relations = RELATIONS.replace("source: relationship_graph\n        edge", "source: crm_postgres\n        edge")
        with catalog_directory(relations=invalid_relations) as directory:
            with self.assertRaisesRegex(CatalogValidationError, "must be neo4j"):
                load_catalog(directory)

    def test_requires_a_resolution_for_every_dataset(self) -> None:
        invalid_sources = SOURCES.replace(
            "  support_ticket:\n    identity_source: support_postgres\n    field_sources:\n      id: support_postgres\n      customer_crm_key: support_postgres\n      content: support_postgres\n",
            "",
        )
        with catalog_directory(sources=invalid_sources) as directory:
            with self.assertRaisesRegex(CatalogValidationError, "missing resolutions for: support_ticket"):
                load_catalog(directory)


class catalog_directory:
    def __init__(self, *, datasets: str = DATASETS, sources: str = SOURCES, relations: str = RELATIONS) -> None:
        self._datasets = datasets
        self._sources = sources
        self._relations = relations
        self._temporary_directory: TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path:
        self._temporary_directory = TemporaryDirectory()
        directory = Path(self._temporary_directory.name)
        (directory / "datasets.yaml").write_text(self._datasets, encoding="utf-8")
        (directory / "sources.yaml").write_text(self._sources, encoding="utf-8")
        (directory / "relations.yaml").write_text(self._relations, encoding="utf-8")
        return directory

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        assert self._temporary_directory is not None
        self._temporary_directory.cleanup()
