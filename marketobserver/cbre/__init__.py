"""Crowd Behavioral Resonance Engine (CBRE) core package."""

from .alignment import AlignedReaction, AlignmentConfig, align_forward
from .calibration import ProbabilityCalibrator
from .audit import FeatureAuditResult, FeatureGroups, audit_features, split_feature_groups
from .baseline import BaselineEngine, BaselineStats
from .core import HybridDecision, ReplayEngine, ResonanceScorer
from .engine import CBREEngine
from .evaluation import FoldResult, walk_forward
from .impact import MarketImpact, SignalLifecycle, measure_impact
from .ingestion import IngestionPipeline, IngestionStats
from .explainability import explain_signal
from .features import WindowAccumulator
from .filters import NoiseFilter
from .model import LightweightResonanceModel
from .regime import RegimeResult, detect_regime
from .schemas import FeatureVector, MarketSnapshot, MessageEvent, ResonanceSignal
from .security import AccessPolicy, RateLimiter, validate_asset_key
from .sources import DEFAULT_SOURCE_POLICY, SourcePolicy
from .validation import StatisticalValidator, ValidationResult

__all__ = [
    "AccessPolicy", "AlignedReaction", "AlignmentConfig", "BaselineEngine", "BaselineStats", "CBREEngine", "DEFAULT_SOURCE_POLICY", "FeatureAuditResult", "FeatureGroups", "FeatureVector", "FoldResult", "HybridDecision", "IngestionPipeline", "IngestionStats", "LightweightResonanceModel", "MarketImpact", "ProbabilityCalibrator",
    "MarketSnapshot", "MessageEvent", "NoiseFilter", "ReplayEngine", "ResonanceScorer",
    "ResonanceSignal", "RegimeResult", "SignalLifecycle", "SourcePolicy", "StatisticalValidator", "ValidationResult", "WindowAccumulator", "align_forward", "audit_features", "detect_regime", "explain_signal", "measure_impact", "RateLimiter", "split_feature_groups", "validate_asset_key", "walk_forward",
]