"""PROJECT: expose only the requested public fields (sources already read only what is needed)."""

from __future__ import annotations

from ...operators import OperatorKind
from ...query.models import BoundQuery
from ...query.resolution import ResolvedField
from ..contracts import (
    PhysicalNode,
    PlanProperties,
    RemoteScan,
    ResultCompleteness,
    ResultProject,
    ResultShape,
    coordinator_location,
)


class ProjectOperator:
    kind = OperatorKind.PROJECT

    def build(self, node: PhysicalNode, query: BoundQuery, scans: tuple[RemoteScan, ...]) -> ResultProject:
        projection = self.projection(query, scans)
        return ResultProject(
            input=node,
            projection=projection,
            properties=PlanProperties(
                output_fields=projection,
                logical_id=_field_named("id", projection),
                ids_are_unique=True,
                ordering=node.properties.ordering,
                location=coordinator_location(),
                completeness=ResultCompleteness.EXACT,
                result_shape=ResultShape.RECORDS,
                catalog_fingerprint=query.catalog_fingerprint,
            ),
        )

    @staticmethod
    def projection(query: BoundQuery, scans: tuple[RemoteScan, ...]) -> tuple[ResolvedField, ...]:
        # Retain the identity-source representation for logical ``id`` (and the
        # first declared owner for every other field) instead of letting a
        # contributor's physical ID mapping overwrite the logical property.
        fields: dict[str, ResolvedField] = {}
        for scan in scans:
            for field in scan.projection:
                fields.setdefault(field.field.name, field)
        return tuple(fields[field.name] for field in query.select)


def _field_named(name: str, fields: tuple[ResolvedField, ...]) -> ResolvedField:
    for field in fields:
        if field.field.name == name:
            return field
    raise AssertionError(f"Required logical field '{name}' is absent from plan projection")
