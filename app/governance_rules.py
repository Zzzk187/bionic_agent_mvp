"""Central, versioned configuration for the Phase-5 memory governance system.

All thresholds and weights live here so that the scoring engine stays a pure
function of (memory, rules) and so that every governance action can record the
exact rule version that produced it.

NOTE: the default weights below are an INITIAL CONFIGURATION only. They have NOT
been validated against real usage data.
"""
from __future__ import annotations

import json
import os

RULE_VERSION = os.getenv("BIONIC_GOVERNANCE_RULE_VERSION", "phase5-gov-v1")

# I = 0.35R + 0.25F + 0.20C + 0.20S   (R, F, C, S each normalised to 0-100)
IMPORTANCE_WEIGHTS = {
    "relevance": 0.35,
    "frequency": 0.25,
    "credibility": 0.20,
    "stability": 0.20,
}

# Frequency normalisation. Log scaling prevents a frequently queried memory
# from growing without bound (access_count -> infinity must not -> 100 instantly).
FREQUENCY_SATURATION_ACCESSES = 50.0
# Extra head-room available above the saturation point, approached
# asymptotically so that F stays strictly increasing but never reaches 100.
FREQUENCY_OVERFLOW_HEADROOM = 10.0

# Source credibility: explicitly stated facts outrank unsupported inference.
CREDIBILITY_BY_SOURCE_PREFIX = [
    ("verified", 95.0),
    ("user-confirmed", 92.0),
    ("user-stated", 88.0),
    ("tool", 75.0),
    ("observed", 70.0),
    ("inferred", 45.0),
    ("guess", 30.0),
]
CREDIBILITY_DEFAULT = 60.0          # no recognised prefix
CREDIBILITY_CONFIDENCE_WEIGHT = 0.25  # confidence (0-1) can move the score up/down

# Stability: categories that describe durable facts resist decay.
STABLE_CATEGORIES = {"profile", "preference", "fact", "identity", "environment"}
VOLATILE_CATEGORIES = {"temp", "temporary", "session", "cache", "todo", "status"}
STABILITY_BASE = 65.0
STABILITY_STABLE_BONUS = 25.0
STABILITY_VOLATILE_PENALTY = 25.0
STABILITY_EXPIRY_PENALTY = 20.0      # has an expires_at that is near/ past
STABILITY_SUPERSEDED_PENALTY = 30.0   # this memory has itself been superseded
STABILITY_SUPERSEDED_BY_BONUS = 15.0   # it corrects an older memory

# Freshness: half-life in days. We deliberately do NOT treat "old" as invalid.
FRESHNESS_HALF_LIFE_DAYS = 120.0
FRESHNESS_MIN = 20.0                  # never decays below this floor

DEFAULT_SCORES = {
    "importance_score": 50.0,
    "confidence_score": 70.0,
    "freshness_score": 70.0,
}

# Duplicate / conflict detection.
CONFLICT_CANDIDATE_COUNT = int(os.getenv("BIONIC_GOVERNANCE_CANDIDATES", "8"))  # Top 5-10
CONFLICT_CANDIDATE_MIN = 5
CONFLICT_CANDIDATE_MAX = 10
SIMILARITY_DUPLICATE = 0.92
SIMILARITY_RELATED = 0.75
SIMILARITY_MIN = 0.60

# Conflict classification vocabulary (fixed, machine readable).
CONFLICT_DUPLICATE = "duplicate"
CONFLICT_SUPPLEMENT = "supplement"
CONFLICT_POSSIBLE = "possible_conflict"
CONFLICT_CORRECTION = "correction"
CONFLICT_TEMPORAL = "temporal_change"
CONFLICT_SCOPE = "different_scope"
CONFLICT_UNDETERMINED = "undetermined"

# Categories where two distinct values are expected to coexist (preferences,
# tastes, profile attributes) rather than contradict each other.
COEXISTING_CATEGORIES = {"preference", "preferences", "profile", "taste",
                         "habit", "style", "interest"}

CONFLICT_TYPES = (
    CONFLICT_DUPLICATE,
    CONFLICT_SUPPLEMENT,
    CONFLICT_POSSIBLE,
    CONFLICT_CORRECTION,
    CONFLICT_TEMPORAL,
    CONFLICT_SCOPE,
    CONFLICT_UNDETERMINED,
)

# Lexical signals; kept explicit and auditable rather than model-driven.
CORRECTION_SIGNALS = (
    "不再", "改成", "改为", "以前是", "以前", "现在是", "现在", "纠正",
    "更正", "错了", "不对", "其实", "实际上", "应该是", "更新为",
    "not anymore", "instead of", "used to be", "corrected", "correction",
)
TEMPORAL_SIGNALS = (
    "去年", "今年", "明年", "以前", "过去", "之前", "现在", "当前", "最近",
    "上周", "本月", "当月", "明年", "那时候", "当时",
    "last year", "this year", "previously", "currently", "now",
)
NEGATION_SIGNALS = ("不", "没", "无", "别", "拒绝", "禁止", "讨厌", "不想",
                    "not ", "no ", "never ", "hate", "dislike")

# Forgetting / archiving policy (progressive, conservative defaults).
ARCHIVE_IMPORTANCE_THRESHOLD = 30.0     # low-score threshold, 0-100
ARCHIVE_FRESHNESS_THRESHOLD = 25.0
OBSERVATION_PERIOD_DAYS = 14
AUTO_ARCHIVE_ENABLED = False            # default OFF
PHYSICAL_DELETE_ENABLED = False         # default FORBIDDEN

# Memory governance view status. This is a VIEW over `memories.status`, which
# remains the single authoritative retrieval filter (active vs archived).
GOV_STATUS_ACTIVE = "active"
GOV_STATUS_ARCHIVED = "archived"
GOV_STATUS_SUPERSEDED = "superseded"
GOV_STATUS_PENDING_REVIEW = "pending_review"
GOVERNANCE_STATUSES = (
    GOV_STATUS_ACTIVE,
    GOV_STATUS_ARCHIVED,
    GOV_STATUS_SUPERSEDED,
    GOV_STATUS_PENDING_REVIEW,
)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def load_rules(overrides: dict | None = None) -> dict:
    """Build the effective rule set. Environment variables win over defaults;
    explicit `overrides` (from a caller) win over environment variables."""
    rules = {
        "rule_version": RULE_VERSION,
        "importance_weights": dict(IMPORTANCE_WEIGHTS),
        "frequency_saturation_accesses": _env_float(
            "BIONIC_GOV_FREQ_SATURATION", FREQUENCY_SATURATION_ACCESSES),
        "credibility_by_source_prefix": [tuple(x) for x in CREDIBILITY_BY_SOURCE_PREFIX],
        "credibility_default": _env_float("BIONIC_GOV_CRED_DEFAULT", CREDIBILITY_DEFAULT),
        "credibility_confidence_weight": _env_float(
            "BIONIC_GOV_CRED_CONF_WEIGHT", CREDIBILITY_CONFIDENCE_WEIGHT),
        "stable_categories": set(STABLE_CATEGORIES),
        "volatile_categories": set(VOLATILE_CATEGORIES),
        "stability_base": _env_float("BIONIC_GOV_STABILITY_BASE", STABILITY_BASE),
        "stability_stable_bonus": _env_float("BIONIC_GOV_STABILITY_BONUS", STABILITY_STABLE_BONUS),
        "stability_volatile_penalty": _env_float(
            "BIONIC_GOV_VOLATILE_PENALTY", STABILITY_VOLATILE_PENALTY),
        "stability_expiry_penalty": _env_float(
            "BIONIC_GOV_EXPIRY_PENALTY", STABILITY_EXPIRY_PENALTY),
        "stability_superseded_penalty": _env_float(
            "BIONIC_GOV_SUPERSEDED_PENALTY", STABILITY_SUPERSEDED_PENALTY),
        "stability_superseded_by_bonus": _env_float(
            "BIONIC_GOV_SUPERSEDED_BY_BONUS", STABILITY_SUPERSEDED_BY_BONUS),
        "freshness_half_life_days": _env_float(
            "BIONIC_GOV_FRESHNESS_HALFLIFE", FRESHNESS_HALF_LIFE_DAYS),
        "freshness_min": _env_float("BIONIC_GOV_FRESHNESS_MIN", FRESHNESS_MIN),
        "similarity_duplicate": _env_float("BIONIC_GOV_SIM_DUPLICATE", SIMILARITY_DUPLICATE),
        "similarity_related": _env_float("BIONIC_GOV_SIM_RELATED", SIMILARITY_RELATED),
        "similarity_min": _env_float("BIONIC_GOV_SIM_MIN", SIMILARITY_MIN),
        "candidate_count": max(CONFLICT_CANDIDATE_MIN,
                               min(_env_int("BIONIC_GOVERNANCE_CANDIDATES", CONFLICT_CANDIDATE_COUNT),
                                   CONFLICT_CANDIDATE_MAX)),
        "archive_importance_threshold": _env_float(
            "BIONIC_GOV_ARCHIVE_IMPORTANCE", ARCHIVE_IMPORTANCE_THRESHOLD),
        "archive_freshness_threshold": _env_float(
            "BIONIC_GOV_ARCHIVE_FRESHNESS", ARCHIVE_FRESHNESS_THRESHOLD),
        "observation_period_days": _env_int(
            "BIONIC_GOV_OBSERVE_DAYS", OBSERVATION_PERIOD_DAYS),
        "auto_archive_enabled": _env_bool("BIONIC_GOV_AUTO_ARCHIVE", AUTO_ARCHIVE_ENABLED),
        "physical_delete_enabled": False,  # hard constraint: never default-on
        # Lexical signals for classification (kept in the rule set so that every
        # verdict can be traced back to the exact vocabulary that produced it).
        "correction_signals": tuple(CORRECTION_SIGNALS),
        "temporal_signals": tuple(TEMPORAL_SIGNALS),
        "negation_signals": tuple(NEGATION_SIGNALS),
        "coexisting_categories": set(COEXISTING_CATEGORIES),
    }
    if overrides:
        for key, value in overrides.items():
            if key == "rule_version":
                continue
            if key == "importance_weights" and isinstance(value, dict):
                rules["importance_weights"].update(value)
            else:
                rules[key] = value
        # Re-clamp candidate count if overridden.
        rules["candidate_count"] = max(CONFLICT_CANDIDATE_MIN,
                                       min(int(rules["candidate_count"]), CONFLICT_CANDIDATE_MAX))
    return rules
