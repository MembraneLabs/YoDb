from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from yodb import (
    CatalogRuntimeError,
    ErrorCode,
    ErrorDetail,
    FindingSeverity,
    InMemoryCatalogRuntime,
    InspectionAdapterBinding,
    InspectionRequest,
    PhysicalField,
    PhysicalKey,
    PhysicalResource,
    RefreshStatus,
    ResourceKind,
    SourceInspection,
    SourceInspectionError,
    SourceInspectionRegistry,
    SourceRuntimeStatus,
    SourceValidationReport,
    ValidationFinding,
)
from yodb.catalog import SourceKind


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
resolution:
  customer:
    identity_source: crm_postgres
    field_sources:
      id: crm_postgres
      name: crm_postgres
"""

RELATIONS = """\
api_version: yodb/v0.1
relationships:
  customer_reference:
    from: customer
    to: customer
    description: A test-only approved customer reference.
    cardinality: one_to_one
    direction: uni
    implementations:
      - from: {source: crm_postgres, field: id}
        to: {source: crm_postgres, field: id}
"""

_NOW = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)


class InMemoryCatalogRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.catalog_directory = Path(self._temporary_directory.name)
        (self.catalog_directory / "datasets.yaml").write_text(DATASETS, encoding="utf-8")
        (self.catalog_directory / "sources.yaml").write_text(SOURCES, encoding="utf-8")
        (self.catalog_directory / "relations.yaml").write_text(RELATIONS, encoding="utf-8")

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_refresh_loads_inspects_validates_and_activates_one_catalog_snapshot(self) -> None:
        inspector = FakeInspector()
        validator = FakeValidator()
        runtime = InMemoryCatalogRuntime(
            self.catalog_directory,
            _registry(inspector, validator),
            clock=lambda: _NOW,
        )

        result = runtime.refresh()

        self.assertEqual(result.status, RefreshStatus.ACTIVATED)
        self.assertIs(result.active, runtime.active)
        assert result.active is not None
        self.assertTrue(result.active.is_activatable)
        self.assertEqual(result.active.catalog.metadata.name, "acme_data")
        self.assertEqual(result.active.sources["crm_postgres"].status, SourceRuntimeStatus.VALID)
        self.assertEqual(inspector.requests[0].source_name, "crm_postgres")
        self.assertEqual(validator.catalogs[0].metadata.version, 1)
        self.assertEqual(validator.inspections[0].resources["public.accounts"].primary_key.fields, ("account_uuid",))

    def test_invalid_candidate_does_not_replace_the_last_active_catalog(self) -> None:
        validator = FakeValidator()
        runtime = InMemoryCatalogRuntime(
            self.catalog_directory, _registry(FakeInspector(), validator), clock=lambda: _NOW
        )
        first = runtime.refresh()
        assert first.active is not None

        validator.findings = (
            ValidationFinding(
                severity=FindingSeverity.ERROR,
                code="physical_field_not_found",
                message="Configured field was not found.",
                location="sources.crm_postgres.datasets.customer.fields.name",
            ),
        )
        rejected = runtime.refresh()

        self.assertEqual(rejected.status, RefreshStatus.REJECTED)
        self.assertIs(rejected.active, first.active)
        assert rejected.candidate is not None
        self.assertFalse(rejected.candidate.is_activatable)
        self.assertEqual(
            rejected.candidate.sources["crm_postgres"].status, SourceRuntimeStatus.INVALID
        )
        self.assertIs(runtime.last_attempt, rejected)

    def test_inspection_failure_is_recorded_and_validator_is_not_called(self) -> None:
        inspector = FakeInspector(
            error=SourceInspectionError(
                ErrorDetail(
                    code=ErrorCode.SOURCE_UNAVAILABLE,
                    message="The source is temporarily unavailable.",
                    retryable=True,
                )
            )
        )
        validator = FakeValidator()
        runtime = InMemoryCatalogRuntime(
            self.catalog_directory, _registry(inspector, validator), clock=lambda: _NOW
        )

        result = runtime.refresh()

        self.assertEqual(result.status, RefreshStatus.REJECTED)
        assert result.candidate is not None
        state = result.candidate.sources["crm_postgres"]
        self.assertEqual(state.status, SourceRuntimeStatus.INSPECTION_FAILED)
        self.assertEqual(state.error.code, ErrorCode.SOURCE_UNAVAILABLE)
        self.assertEqual(state.error.source_name, "crm_postgres")
        self.assertEqual(validator.catalogs, [])

    def test_unregistered_source_kind_is_an_explicit_rejected_source(self) -> None:
        runtime = InMemoryCatalogRuntime(
            self.catalog_directory, SourceInspectionRegistry(()), clock=lambda: _NOW
        )

        result = runtime.refresh()

        self.assertEqual(result.status, RefreshStatus.REJECTED)
        assert result.candidate is not None
        error = result.candidate.sources["crm_postgres"].error
        assert error is not None
        self.assertEqual(error.code, ErrorCode.SOURCE_KIND_UNSUPPORTED)

    def test_bad_yaml_is_a_structured_load_failure_without_a_candidate(self) -> None:
        runtime = InMemoryCatalogRuntime(
            Path("/definitely-not-a-yodb-catalog"),
            _registry(FakeInspector(), FakeValidator()),
            clock=lambda: _NOW,
        )

        result = runtime.refresh()

        self.assertEqual(result.status, RefreshStatus.LOAD_FAILED)
        self.assertIsNone(result.candidate)
        self.assertIsNone(runtime.active)
        assert result.error is not None
        self.assertEqual(result.error.code, ErrorCode.CATALOG_LOAD_FAILED)

    def test_require_active_rejects_an_uninitialized_runtime(self) -> None:
        runtime = InMemoryCatalogRuntime(
            self.catalog_directory, _registry(FakeInspector(), FakeValidator()), clock=lambda: _NOW
        )

        with self.assertRaises(CatalogRuntimeError) as caught:
            runtime.require_active()

        self.assertEqual(caught.exception.code, ErrorCode.CATALOG_RUNTIME_UNINITIALIZED)


class FakeInspector:
    def __init__(self, *, error: SourceInspectionError | None = None) -> None:
        self.error = error
        self.requests: list[InspectionRequest] = []

    def inspect(self, request: InspectionRequest) -> SourceInspection:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return SourceInspection(
            source_name=request.source_name,
            source_kind=request.source.kind,
            inspected_at=_NOW,
            resources={
                "public.accounts": PhysicalResource(
                    name="public.accounts",
                    kind=ResourceKind.TABLE,
                    fields={
                        "account_uuid": PhysicalField(
                            name="account_uuid", native_type="uuid", type_family="uuid", nullable=False
                        ),
                        "company_name": PhysicalField(
                            name="company_name", native_type="text", type_family="string", nullable=False
                        ),
                    },
                    primary_key=PhysicalKey(name="accounts_pkey", fields=("account_uuid",)),
                )
            },
        )


class FakeValidator:
    def __init__(self) -> None:
        self.findings: tuple[ValidationFinding, ...] = ()
        self.catalogs: list[object] = []
        self.inspections: list[SourceInspection] = []

    def validate(self, catalog: object, inspection: SourceInspection) -> SourceValidationReport:
        self.catalogs.append(catalog)
        self.inspections.append(inspection)
        return SourceValidationReport(
            source_name=inspection.source_name, inspected_at=inspection.inspected_at, findings=self.findings
        )


def _registry(inspector: FakeInspector, validator: FakeValidator) -> SourceInspectionRegistry:
    return SourceInspectionRegistry(
        (
            InspectionAdapterBinding(
                source_kind=SourceKind.POSTGRES, inspector=inspector, validator=validator
            ),
        )
    )
