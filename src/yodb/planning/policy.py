"""Planner-wide limits shared by every planning operator."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlannerPolicy:
    """Hard baseline planner limits; runtime enforces the same scan guards."""

    maximum_rows_per_source: int = 10_000
    # Largest logical-ID set transferred between sources to restrict a later
    # scan (0 disables transfer).  Above this the executor runs a plain scan.
    maximum_transfer_keys: int = 1_000

    def __post_init__(self) -> None:
        if self.maximum_rows_per_source < 1:
            raise ValueError("maximum_rows_per_source must be positive")
        if self.maximum_transfer_keys < 0:
            raise ValueError("maximum_transfer_keys must not be negative")
