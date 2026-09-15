from __future__ import annotations

from datetime import UTC, datetime
import unittest

from yodb import (
    FindingSeverity,
    InspectionCapability,
    PhysicalField,
    PhysicalResource,
    ResourceKind,
    SourceInspection,
    SourceValidationReport,
    ValidationFinding,
)
from yodb.catalog import SourceKind


class InspectionContractsTests(unittest.TestCase):
    def test_source_inspection_represents_adapter_native_schema_facts(self) -> None:
        inspection = SourceInspection(
            source_name="crm_postgres",
            source_kind=SourceKind.POSTGRES,
            inspected_at=datetime(2026, 9, 15, tzinfo=UTC),
            capabilities={
                InspectionCapability.RELATIONAL_SCHEMA,
                InspectionCapability.PRIMARY_KEYS,
                InspectionCapability.INDEXES,
            },
            resources={
                "public.accounts": PhysicalResource(
                    name="public.accounts",
                    kind=ResourceKind.TABLE,
                    fields={
                        "account_uuid": PhysicalField(
                            name="account_uuid", type_name="uuid", nullable=False
                        )
                    },
                    primary_key=("account_uuid",),
                )
            },
        )

        account = inspection.resources["public.accounts"]
        self.assertEqual(account.fields["account_uuid"].type_name, "uuid")
        self.assertIn(InspectionCapability.INDEXES, inspection.capabilities)

    def test_validation_report_is_invalid_only_when_it_contains_an_error(self) -> None:
        warning_only = SourceValidationReport(
            source_name="crm_postgres",
            inspected_at=datetime(2026, 9, 15, tzinfo=UTC),
            findings=(
                ValidationFinding(
                    severity=FindingSeverity.WARNING,
                    code="index_not_found",
                    message="No index was found for a mapped field.",
                    location="sources.crm_postgres.datasets.customer.fields.name",
                ),
            ),
        )
        invalid = warning_only.model_copy(
            update={
                "findings": warning_only.findings
                + (
                    ValidationFinding(
                        severity=FindingSeverity.ERROR,
                        code="resource_not_found",
                        message="Configured resource was not found.",
                        location="sources.crm_postgres.datasets.customer.resource",
                    ),
                )
            }
        )

        self.assertTrue(warning_only.is_valid)
        self.assertFalse(invalid.is_valid)

