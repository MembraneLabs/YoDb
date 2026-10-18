"""Canonical, non-secret fingerprints for logical queries and catalog snapshots."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Any

from ..catalog import Catalog


def catalog_fingerprint(catalog: Catalog) -> str:
    """Hash the active catalog definition, including mappings and relationships.

    It covers what the catalog says, not the format version its files declare, so
    upgrading YoDb or relabelling a file leaves it unchanged.

    This digest stays inside YoDb/cursor payloads. It is not a public catalog
    export and must never be used to disclose the source definitions it covers.
    """

    return _digest(catalog.model_dump(mode="json", by_alias=True))


def query_fingerprint(payload: dict[str, Any]) -> str:
    """Hash canonical logical query meaning, excluding cursors and page size."""

    return _digest(payload)


def canonical_data(value: Any) -> Any:
    """Convert supported immutable query values to stable JSON-compatible data."""

    if is_dataclass(value):
        return canonical_data(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): canonical_data(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [canonical_data(item) for item in value]
    return value


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        canonical_data(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
