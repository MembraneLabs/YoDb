from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from yodb import CatalogValidationError, load_catalog
from yodb.query.fingerprint import catalog_fingerprint


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
    direction: uni
    implementations:
      - from: {source: crm_postgres, field: crm_account_key}
        to: {source: support_postgres, field: customer_crm_key}
      - from: {source: relationship_graph, field: crm_account_key}
        to: {source: relationship_graph, field: id}
        edge_type: HAS_TICKET
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

    def test_each_file_may_declare_any_format_version_up_to_the_one_this_yodb_reads(self) -> None:
        for datasets, sources, relations in (
            ("yodb/v0.1", "yodb/v0.2", "yodb/v0.1"),
            ("yodb/v0.2", "yodb/v0.2", "yodb/v0.2"),
        ):
            with self.subTest(datasets=datasets, sources=sources, relations=relations):
                with catalog_directory(
                    datasets=DATASETS.replace("yodb/v0.1", datasets),
                    sources=SOURCES.replace("yodb/v0.1", sources),
                    relations=RELATIONS.replace("yodb/v0.1", relations),
                ) as directory:
                    self.assertEqual(load_catalog(directory).metadata.name, "acme_data")

    def test_a_file_newer_than_this_yodb_reads_is_refused_whichever_file_it_is(self) -> None:
        for name in ("datasets", "sources", "relations"):
            with self.subTest(name=name):
                files = {"datasets": DATASETS, "sources": SOURCES, "relations": RELATIONS}
                files[name] = files[name].replace("yodb/v0.1", "yodb/v0.3")
                with catalog_directory(**files) as directory:
                    with self.assertRaisesRegex(CatalogValidationError, f"{name}.yaml.*yodb/v0.3.*newer"):
                        load_catalog(directory)

    def test_a_file_with_another_major_or_a_malformed_version_is_refused(self) -> None:
        for version in ("yodb/v1.0", "yodb/v0", "v0.1", "yodb/v0.1.1", "0.1"):
            with self.subTest(version=version):
                with catalog_directory(datasets=DATASETS.replace("yodb/v0.1", version)) as directory:
                    with self.assertRaisesRegex(CatalogValidationError, "datasets.yaml.*api_version"):
                        load_catalog(directory)

    def test_the_catalog_fingerprint_covers_content_not_the_declared_format_version(self) -> None:
        with catalog_directory() as directory:
            before = catalog_fingerprint(load_catalog(directory))
        with catalog_directory(
            datasets=DATASETS.replace("yodb/v0.1", "yodb/v0.2"), sources=SOURCES.replace("yodb/v0.1", "yodb/v0.2")
        ) as directory:
            relabelled = catalog_fingerprint(load_catalog(directory))
        with catalog_directory(datasets=DATASETS.replace("Customer company name.", "Customer name.")) as directory:
            edited = catalog_fingerprint(load_catalog(directory))

        self.assertEqual(before, relabelled)
        self.assertNotEqual(before, edited)

    def test_rules_yaml_is_optional_and_an_empty_one_changes_nothing(self) -> None:
        with catalog_directory() as directory:
            without = load_catalog(directory)
        with catalog_directory(rules="api_version: yodb/v0.2\nrules: {}\n") as directory:
            empty = load_catalog(directory)

        self.assertEqual(without, empty)
        self.assertEqual(catalog_fingerprint(without), catalog_fingerprint(empty))

    def test_rules_yaml_is_checked_like_the_other_files(self) -> None:
        cases = (
            ("api_version: yodb/v0.3\nrules: {}\n", "rules.yaml.*newer"),
            ("api_version: yodb/v0.1\nrules: {}\n", "rules.yaml.*yodb/v0.2 or later"),
            ("api_version: yodb/v0.2\nrules: {}\nextra: 1\n", "Invalid rules.yaml"),
            ("api_version: yodb/v0.2\n", "Invalid rules.yaml"),
            ("api_version: yodb/v0.2\nrules: {customer: {paying: {}}}\n", "cannot define rules yet"),
        )
        for text, message in cases:
            with self.subTest(text=text):
                with catalog_directory(rules=text) as directory:
                    with self.assertRaisesRegex(CatalogValidationError, message):
                        load_catalog(directory)

    def test_a_catalog_may_declare_no_relationships(self) -> None:
        with catalog_directory(relations="api_version: yodb/v0.1\nrelationships: {}\n") as directory:
            catalog = load_catalog(directory)

        self.assertEqual(catalog.relationships, {})
        self.assertIn("customer", catalog.datasets)

    def test_a_catalog_still_needs_datasets_and_sources(self) -> None:
        with catalog_directory(datasets=DATASETS.split("datasets:")[0] + "datasets: {}\n") as directory:
            with self.assertRaisesRegex(CatalogValidationError, "datasets must not be empty"):
                load_catalog(directory)

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
        invalid_sources = SOURCES.replace(
            "relationship_graph:\n    kind: neo4j",
            "relationship_graph:\n    kind: postgres",
        )
        with catalog_directory(sources=invalid_sources) as directory:
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

    def test_requires_logical_id_to_come_from_the_identity_source(self) -> None:
        invalid_sources = SOURCES.replace(
            "      id: crm_postgres\n      name: crm_postgres",
            "      id: relationship_graph\n      name: crm_postgres",
        )
        with catalog_directory(sources=invalid_sources) as directory:
            with self.assertRaisesRegex(CatalogValidationError, "field_sources.id must match identity_source"):
                load_catalog(directory)


class catalog_directory:
    def __init__(
        self, *, datasets: str = DATASETS, sources: str = SOURCES, relations: str = RELATIONS, rules: str | None = None
    ) -> None:
        self._rules = rules
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
        if self._rules is not None:
            (directory / "rules.yaml").write_text(self._rules, encoding="utf-8")
        return directory

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        assert self._temporary_directory is not None
        self._temporary_directory.cleanup()
