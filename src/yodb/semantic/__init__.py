"""The semantic filter: provider contracts, planning, execution, and the one extension that plugs them in.

``SemanticExtension`` is the interface to the engine; everything else here is
what it is built from.
"""

from .contracts import (
    DEFAULT_POSITIVE_CONFIDENCE,
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResult,
    PropositionVerifier,
    ProviderCost,
    ProviderInfo,
    SemanticExecutionStats,
    SemanticPlanKind,
    SemanticQueryReport,
    SemanticRecordMetadata,
    SemanticRuntime,
    Vector,
    VerificationCandidate,
    VerificationRequest,
    VerificationResult,
    VerificationUsage,
    VerificationVerdict,
    batches,
    passes_quality,
)
from .execution import execute as execute_semantic_verify
from .extension import SemanticExtension
from .planning import (
    PowerLawRecall,
    RecallModel,
    SemanticCosts,
    SemanticDecision,
    SemanticOperator,
    SemanticOptions,
    SemanticPlanPreference,
    SemanticPolicy,
    SemanticVerify,
    semantic_variants,
    shortlist_ladder,
)

__all__ = [
    "PowerLawRecall",
    "RecallModel",
    "SemanticCosts",
    "SemanticDecision",
    "SemanticExtension",
    "SemanticOperator",
    "SemanticOptions",
    "SemanticPlanPreference",
    "SemanticPolicy",
    "SemanticVerify",
    "execute_semantic_verify",
    "semantic_variants",
    "shortlist_ladder",
    "DEFAULT_POSITIVE_CONFIDENCE",
    "EmbeddingProvider",
    "EmbeddingRequest",
    "EmbeddingResult",
    "PropositionVerifier",
    "ProviderCost",
    "ProviderInfo",
    "SemanticExecutionStats",
    "SemanticPlanKind",
    "SemanticQueryReport",
    "SemanticRecordMetadata",
    "SemanticRuntime",
    "Vector",
    "VerificationCandidate",
    "VerificationRequest",
    "VerificationResult",
    "VerificationUsage",
    "VerificationVerdict",
    "batches",
    "passes_quality",
]
