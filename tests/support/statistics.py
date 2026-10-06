"""Shared test fixtures: statistics."""

from __future__ import annotations


class MapProvider:
    """Statistics keyed by the source resource (what a real provider reads from a catalog)."""

    def __init__(self, by_resource):
        self.by_resource = by_resource
        self.calls = 0

    def statistics(self, source):
        self.calls += 1
        return self.by_resource.get(source.resource)
