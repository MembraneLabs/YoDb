"""Binding a field name to a public field of the root dataset."""

from __future__ import annotations

from ..catalog import Visibility
from ..errors import ErrorCode
from .models import BoundDataset, BoundField
from .shape import fail


def bind_public_field(root: BoundDataset, field_name: str, location: str) -> BoundField:
    field = root.spec.fields.get(field_name)
    if field is None:
        fail(ErrorCode.FIELD_NOT_FOUND, f"Unknown field '{field_name}' on dataset '{root.name}'.", location)
    if field.visibility is not Visibility.PUBLIC:
        fail(
            ErrorCode.FIELD_NOT_ACCESSIBLE,
            f"Field '{field_name}' on dataset '{root.name}' is not publicly queryable.",
            location,
        )
    return BoundField(dataset_name=root.name, scope=root.scope, name=field_name, spec=field)
