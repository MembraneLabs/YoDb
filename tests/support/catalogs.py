"""Shared test fixtures: catalogs."""

from __future__ import annotations

from datetime import datetime, UTC

from yodb.catalog import (
    Catalog,
    CatalogMetadata,
    DatasetResolution,
    DatasetSpec,
    FieldSpec,
    LogicalType,
    SourceDatasetSpec,
    SourceFieldSpec,
    SourceKind,
    SourceSpec,
)
from yodb.inspection import SourceInspection, SourceValidationReport
from yodb.runtime import CatalogEvaluation, SourceRuntimeState, SourceRuntimeStatus


_NOW = datetime(2026, 9, 17, tzinfo=UTC)


class StaticRuntime:
    def __init__(self, active: CatalogEvaluation) -> None:
        self._active = active

    def require_active(self) -> CatalogEvaluation:
        return self._active


class SourceRowsExecutor:
    source_kind = SourceKind.POSTGRES

    def __init__(self, rows: dict[str, tuple[dict[str, object], ...]]) -> None:
        self._rows = rows
        self.queries = []

    def execute(self, query, *, timeout_seconds: float | None = None):
        self.queries.append(query)
        return self._rows[query.source_name]


def multi_source_catalog() -> CatalogEvaluation:
    sources = {
        "crm": SourceSpec(
            kind=SourceKind.POSTGRES, connection_ref="crm", read_only=True,
            datasets={"customer": SourceDatasetSpec(resource="crm.accounts", identity=("id",), fields={
                "id": SourceFieldSpec(physical_name="account_id"), "name": SourceFieldSpec(physical_name="name"), "status": SourceFieldSpec(physical_name="status"),
            })},
        ),
        "billing": SourceSpec(
            kind=SourceKind.POSTGRES, connection_ref="billing", read_only=True,
            datasets={"customer": SourceDatasetSpec(resource="billing.customers", identity=("id",), fields={
                "id": SourceFieldSpec(physical_name="customer_id"), "plan": SourceFieldSpec(physical_name="plan"),
            })},
        ),
    }
    catalog = Catalog(
        metadata=CatalogMetadata(name="execution-multi", version=1),
        datasets={"customer": DatasetSpec(description="Customer.", fields={
            "id": FieldSpec(type=LogicalType.ID, description="ID."),
            "name": FieldSpec(type=LogicalType.STRING, description="Name."),
            "status": FieldSpec(type=LogicalType.STRING, description="Status."),
            "plan": FieldSpec(type=LogicalType.STRING, description="Plan."),
        })},
        sources=sources,
        resolution={"customer": DatasetResolution(identity_source="crm", field_sources={"id": "crm", "name": "crm", "status": "crm", "plan": "billing"})},
        relationships={},
    )
    return CatalogEvaluation(
        catalog=catalog, evaluated_at=_NOW,
        sources={name: SourceRuntimeState(source_name=name, status=SourceRuntimeStatus.VALID, inspection=SourceInspection(source_name=name, source_kind=source.kind, inspected_at=_NOW), validation=SourceValidationReport(source_name=name, inspected_at=_NOW)) for name, source in sources.items()},
    )


_NOW = datetime(2026, 9, 17, tzinfo=UTC)


def crm_billing_catalog() -> CatalogEvaluation:
    customer = DatasetSpec(
        description="Customer.",
        fields={
            "id": FieldSpec(type=LogicalType.ID, description="ID."),
            "name": FieldSpec(type=LogicalType.STRING, description="Name."),
            "status": FieldSpec(type=LogicalType.STRING, description="Status."),
            "plan": FieldSpec(type=LogicalType.STRING, description="Plan."),
        },
    )
    catalog = Catalog(
        metadata=CatalogMetadata(name="planner", version=1),
        datasets={"customer": customer},
        sources={
            "crm": SourceSpec(kind=SourceKind.POSTGRES, connection_ref="crm", read_only=True, datasets={"customer": SourceDatasetSpec(resource="crm.accounts", identity=("id",), fields={"id": SourceFieldSpec(physical_name="account_id"), "name": SourceFieldSpec(physical_name="name"), "status": SourceFieldSpec(physical_name="status")})}),
            "billing": SourceSpec(kind=SourceKind.POSTGRES, connection_ref="billing", read_only=True, datasets={"customer": SourceDatasetSpec(resource="billing.customers", identity=("id",), fields={"id": SourceFieldSpec(physical_name="customer_id"), "plan": SourceFieldSpec(physical_name="plan")})}),
        },
        resolution={"customer": DatasetResolution(identity_source="crm", field_sources={"id": "crm", "name": "crm", "status": "crm", "plan": "billing"})},
        relationships={},
    )
    return CatalogEvaluation(
        catalog=catalog,
        evaluated_at=_NOW,
        sources={name: SourceRuntimeState(source_name=name, status=SourceRuntimeStatus.VALID, inspection=SourceInspection(source_name=name, source_kind=source.kind, inspected_at=_NOW), validation=SourceValidationReport(source_name=name, inspected_at=_NOW)) for name, source in catalog.sources.items()},
    )
