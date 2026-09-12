"""YoDb-owned logical identifiers."""

from __future__ import annotations

import re
import secrets
import time


_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ID_PATTERN = re.compile(r"^ydb_[0-9A-HJKMNP-TV-Z]{26}$")


def new_id() -> str:
    """Return an opaque, time-sortable YoDb ID with a ULID-style payload."""
    timestamp_ms = time.time_ns() // 1_000_000
    if timestamp_ms >= 1 << 48:
        raise RuntimeError("System timestamp exceeds the supported ID range.")
    value = (timestamp_ms << 80) | int.from_bytes(secrets.token_bytes(10))
    encoded = "".join(_CROCKFORD32[(value >> shift) & 31] for shift in range(125, -1, -5))
    return f"ydb_{encoded}"


def is_yodb_id(value: str) -> bool:
    return bool(_ID_PATTERN.fullmatch(value))
