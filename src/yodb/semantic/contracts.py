"""Provider-neutral contracts for executing a ``SemanticFilter``.

``SemanticFilter(field, proposition)`` asks whether a proposition is *true of a
record*.  Two roles cooperate, and neither is the meaning of the filter:

* an :class:`EmbeddingProvider` turns the proposition into a vector so a source
  can produce a bounded *candidate shortlist* (retrieval only narrows work);
* a :class:`PropositionVerifier` decides truth for each candidate (the
  authoritative step; a vector index is never semantic truth).

Providers are injected, like database adapters.  Nothing here names a vendor,
model, or index, and no logical query ever does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
import math
from typing import Protocol, runtime_checkable

Vector = tuple[float, ...]

# Confidence assumed for a positive verdict from a verifier that reports none.
DEFAULT_POSITIVE_CONFIDENCE = 1.0


class SemanticPlanKind(str, Enum):
    """The physical strategies for one semantic condition."""

    VERIFY_ALL = "verify_all"                        # Plan A: verify every candidate
    VECTOR_SHORTLIST = "vector_shortlist"            # Plan B: shortlist, then verify


@dataclass(frozen=True)
class ProviderInfo:
    """Who produced a decision, recorded on results for provenance."""

    provider: str
    model: str
    version: str

    def __post_init__(self) -> None:
        if not (self.provider.strip() and self.model.strip() and self.version.strip()):
            raise ValueError("provider, model and version must not be blank")


@dataclass(frozen=True)
class EmbeddingRequest:
    texts: tuple[str, ...]
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.texts or any(not text.strip() for text in self.texts):
            raise ValueError("an embedding request needs at least one non-blank text")


@dataclass(frozen=True)
class EmbeddingResult:
    """One vector per requested text, in request order."""

    vectors: tuple[Vector, ...]
    info: ProviderInfo
    dimensions: int

    def __post_init__(self) -> None:
        if self.dimensions <= 0:
            raise ValueError("dimensions must be positive")
        for vector in self.vectors:
            if len(vector) != self.dimensions or not all(math.isfinite(x) for x in vector):
                raise ValueError("every vector must be finite and match the declared dimensions")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embeds text into the vector space that stored embeddings live in.

    ``info.model`` and ``dimensions`` must equal the catalog ``EmbeddingBinding``
    being searched; a mismatch is a planning error, never a silent comparison
    across spaces.
    """

    @property
    def info(self) -> ProviderInfo: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult: ...


@dataclass(frozen=True)
class VerificationCandidate:
    """One record's text to judge; ``logical_id`` ties the verdict back."""

    logical_id: object
    text: str


@dataclass(frozen=True)
class VerificationVerdict:
    """Whether the proposition holds for one candidate.

    ``confidence`` (0..1) is the verifier's belief in *its own verdict*, or
    ``None`` if it does not report one.
    """

    logical_id: object
    holds: bool
    confidence: float | None = None

    def __post_init__(self) -> None:
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be within [0, 1]")


@dataclass(frozen=True)
class VerificationUsage:
    """Measured work for one verification call (feeds telemetry and budgets)."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    latency_ms: float = 0.0

    def __post_init__(self) -> None:
        if min(self.model_calls, self.input_tokens, self.output_tokens) < 0 or self.cost < 0 or self.latency_ms < 0:
            raise ValueError("usage values must not be negative")

    def __add__(self, other: "VerificationUsage") -> "VerificationUsage":
        return VerificationUsage(
            self.model_calls + other.model_calls,
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cost + other.cost,
            self.latency_ms + other.latency_ms,
        )


@dataclass(frozen=True)
class VerificationRequest:
    proposition: str
    candidates: tuple[VerificationCandidate, ...]
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.proposition.strip():
            raise ValueError("a proposition must not be blank")
        if not self.candidates:
            raise ValueError("a verification request needs at least one candidate")
        ids = [candidate.logical_id for candidate in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate logical IDs must be unique")


@dataclass(frozen=True)
class VerificationResult:
    """Exactly one verdict per requested candidate, matched by ``logical_id``."""

    verdicts: tuple[VerificationVerdict, ...]
    usage: VerificationUsage
    info: ProviderInfo

    def require_complete_for(self, request: VerificationRequest) -> None:
        """Raise if the verifier skipped, duplicated, or invented a candidate."""

        expected = {candidate.logical_id for candidate in request.candidates}
        returned = [verdict.logical_id for verdict in self.verdicts]
        if len(returned) != len(set(returned)) or set(returned) != expected:
            raise ValueError("a verifier must return exactly one verdict per requested candidate")


@runtime_checkable
class PropositionVerifier(Protocol):
    """Decides truth of a proposition for a bounded batch of candidates."""

    @property
    def info(self) -> ProviderInfo: ...

    @property
    def maximum_batch_size(self) -> int: ...

    def verify(self, request: VerificationRequest) -> VerificationResult: ...


def passes_quality(verdict: VerificationVerdict, minimum_quality: float | None) -> bool:
    """Whether a record satisfies the semantic condition at the caller's quality bar.

    A record qualifies only on a positive verdict.  With ``minimum_quality`` set,
    that verdict's confidence must reach it; a positive verdict that reports no
    confidence counts as ``DEFAULT_POSITIVE_CONFIDENCE``.  A negative verdict
    never qualifies, however confident, so a high bar can only shrink results.
    """

    if not verdict.holds:
        return False
    if minimum_quality is None:
        return True
    confidence = DEFAULT_POSITIVE_CONFIDENCE if verdict.confidence is None else verdict.confidence
    return confidence >= minimum_quality


@dataclass(frozen=True)
class SemanticRecordMetadata:
    """Per-record provenance of a semantic decision, returned beside the row."""

    plan: SemanticPlanKind
    holds: bool
    confidence: float | None
    info: ProviderInfo


@dataclass(frozen=True)
class SemanticExecutionStats:
    """Actual work done for one semantic condition (compared with estimates)."""

    plan: SemanticPlanKind
    candidates_considered: int
    shortlisted: int | None          # None for VERIFY_ALL (no retrieval step)
    verified: int
    qualified: int
    usage: VerificationUsage
    embedding_model_calls: int = 0

    def __post_init__(self) -> None:
        if min(self.candidates_considered, self.verified, self.qualified) < 0:
            raise ValueError("counts must not be negative")
        if self.verified > self.candidates_considered or self.qualified > self.verified:
            raise ValueError("qualified <= verified <= candidates_considered must hold")
        if self.plan is SemanticPlanKind.VERIFY_ALL and self.shortlisted is not None:
            raise ValueError("VERIFY_ALL has no shortlist")
        if self.plan is SemanticPlanKind.VECTOR_SHORTLIST and self.shortlisted is None:
            raise ValueError("VECTOR_SHORTLIST must report its shortlist size")


@dataclass(frozen=True)
class SemanticQueryReport:
    """What a semantic query did and, per returned record, why it qualified."""

    stats: SemanticExecutionStats
    records: dict[object, SemanticRecordMetadata]


@dataclass(frozen=True)
class SemanticRuntime:
    """The injected providers used to execute semantic conditions.

    ``embedder`` is optional: without it only the verify-everything plan runs.
    ``verification_batch_size`` caps batches below the verifier's own maximum
    (smaller batches waste fewer calls when a page fills early).
    """

    verifier: PropositionVerifier
    embedder: EmbeddingProvider | None = None
    verification_batch_size: int | None = None

    def __post_init__(self) -> None:
        if self.verification_batch_size is not None and self.verification_batch_size <= 0:
            raise ValueError("verification_batch_size must be positive")

    @property
    def batch_size(self) -> int:
        cap = self.verifier.maximum_batch_size
        return cap if self.verification_batch_size is None else min(cap, self.verification_batch_size)


def batches(candidates: Sequence[VerificationCandidate], size: int) -> tuple[tuple[VerificationCandidate, ...], ...]:
    """Split candidates into verifier-sized batches, preserving order."""

    if size <= 0:
        raise ValueError("batch size must be positive")
    return tuple(tuple(candidates[start : start + size]) for start in range(0, len(candidates), size))
