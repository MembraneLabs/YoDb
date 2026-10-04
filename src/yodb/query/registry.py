"""The default set of extension terms a query may contain."""

from __future__ import annotations

from .extensions import TermRegistry
from .semantic import SEMANTIC_TERM

DEFAULT_TERMS = TermRegistry([SEMANTIC_TERM])
