"""Module boundaries: the core knows nothing about the semantic filter, and the catalog stays honest."""

from __future__ import annotations

import subprocess
import sys
import unittest

from yodb.operators import OPERATORS, OperatorKind
from yodb.planning import POSTGRES_CAPABILITIES, operator_support
from yodb.semantic import SemanticCosts, SemanticOptions, SemanticPlanKind, semantic_variants


class CoreIndependenceTests(unittest.TestCase):
    def test_the_core_packages_load_without_the_semantic_package(self) -> None:
        for package in ("yodb.query", "yodb.planning", "yodb.execution"):
            with self.subTest(package=package):
                code = f"import sys, {package}; sys.exit(1 if 'yodb.semantic' in sys.modules else 0)"
                result = subprocess.run([sys.executable, "-c", code], env={"PYTHONPATH": ":".join(sys.path)}, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())


class CatalogHonestyTests(unittest.TestCase):
    def test_the_catalogs_semantic_strategies_are_the_ones_planning_offers(self) -> None:
        catalog = set(OPERATORS.get(OperatorKind.SEMANTIC_FILTER).strategies)
        offered = {kind.value for kind in SemanticPlanKind}
        self.assertEqual(catalog, offered)
        options = SemanticOptions(True, 20, 100, 1_000, 10, 0.8)
        payload_kinds = {variant.payload.kind.value for variant in semantic_variants(options, SemanticCosts())}
        self.assertEqual(payload_kinds, offered)

    def test_every_implemented_operator_has_a_support_view_for_a_source(self) -> None:
        view = {item.kind: item for item in operator_support(POSTGRES_CAPABILITIES)}
        for spec in OPERATORS:
            if spec.implemented:
                self.assertNotEqual(view[spec.kind].detail, "not implemented yet", spec.kind)
        self.assertEqual(view[OperatorKind.SEMANTIC_FILTER].strategies, ("verify_all", "vector_shortlist"))


if __name__ == "__main__":
    unittest.main()
