"""Deterministic, explainable scoring for Phase-5 memory governance.

This module is deliberately DB-free: it turns a memory record plus contextual
signals into scores and human-readable reasons. Persistence is the caller's job,
which keeps the algorithm unit-testable and lets `score_memories` run in
dry-run mode without side effects.

Formula (initial configuration, NOT validated against real usage data):
    I = 0.35*R + 0.25*F + 0.20*C + 0.20*S
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from app.governance_rules import load_rules

GOV_ACTIVE_DEFAULT = "active"
GOV_STATUS_PENDING_REVIEW = "pending_review"

# SQLite CURRENT_TIMESTAMP yields 'YYYY-MM-DD HH:MM:SS' in UTC; ISO-8601 with a
# trailing 'Z' or an offset also appears when rows are written by other tools.
_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d",
)


def parse_ts(value) -> datetime | None:
    """Parse a stored timestamp into an aware UTC datetime. Never raises."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clamp(value, low: float = 0.0, high: float = 100.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return low
    if math.isnan(v) or math.isinf(v):
        return low
    return max(low, min(high, v))


def _days_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 86400.0


def score_relevance(memory: dict, context: dict, rules: dict) -> tuple[float, list[str]]:
    """R: usefulness for future tasks, derived from real retrieval signals."""
    reasons: list[str] = []
    signals: list[float] = []

    semantic_hits = context.get("semantic_hit_count")
    if semantic_hits is not None:
        n = max(0, int(semantic_hits))
        value = clamp(100.0 * math.log1p(n) / math.log1p(5.0))
        signals.append(value)
        reasons.append(f"语义检索命中 {n} 次 → R 分量 {value:.1f}")

    best_similarity = context.get("best_similarity")
    if best_similarity is not None:
        value = clamp(float(best_similarity) * 100.0)
        signals.append(value)
        reasons.append(f"最高语义相似度 {float(best_similarity):.3f} → R 分量 {value:.1f}")

    link_count = context.get("link_count")
    if link_count is not None:
        n = max(0, int(link_count))
        value = clamp(100.0 * math.log1p(n) / math.log1p(8.0))
        signals.append(value)
        reasons.append(f"显式关联 {n} 条 → R 分量 {value:.1f}")

    referenced = bool(context.get("referenced_by_others"))
    if referenced:
        signals.append(85.0)
        reasons.append("被其他记忆引用（supersedes/related 入向链接） → R 分量 85.0")

    query_terms = context.get("query_terms") or []
    if query_terms:
        haystack = " ".join([
            str(memory.get("content") or ""),
            str(memory.get("category") or ""),
            " ".join(context.get("tags") or []),
        ]).lower()
        hits = sum(1 for t in query_terms if str(t).lower() in haystack)
        if hits:
            value = clamp(100.0 * hits / max(1, len(query_terms)))
            signals.append(value)
            reasons.append(f"与近期查询词重合 {hits}/{len(query_terms)} → R 分量 {value:.1f}")

    if not signals:
        reasons.append("无检索信号（新记忆或从未被召回） → R 使用安全默认值 50.0")
        return 50.0, reasons
    return round(sum(signals) / len(signals), 2), reasons


def score_frequency(memory: dict, context: dict, rules: dict) -> tuple[float, list[str]]:
    """F: usage frequency, log-normalised so it saturates instead of exploding.

    The ratio `log1p(access_count) / log1p(saturation)` reaches 1.0 exactly at
    the saturation point and keeps growing afterwards, so the score is capped at
    100 and every increment past saturation yields no gain at all.  To keep the
    ranking meaningful beyond that point we add a small decelerating overflow
    term and clamp the total, which keeps the function monotonic increasing,
    bounded by 100 and never reaching it.
    """
    reasons: list[str] = []
    try:
        access_count = int(memory.get("access_count") or 0)
    except (TypeError, ValueError):
        access_count = 0
    access_count = max(0, access_count)
    saturation = max(1.0, float(rules.get("frequency_saturation_accesses", 50.0)))
    # The log ratio hits 100 exactly at the saturation point; past that point the
    # soft ceiling keeps F strictly increasing but asymptotic to 100, so a busy
    # memory can never inflate its importance without bound.
    span = max(1.0, float(rules.get("frequency_overflow_headroom", 10.0)))
    value = _soft_ceiling(100.0 * math.log1p(access_count) / math.log1p(saturation), span=span)
    reasons.append(
        f"access_count={access_count}，log1p 归一化（饱和点 {saturation:.0f} 次，"
        f"接近饱和后软压缩） → F={value:.2f}"
    )
    if access_count == 0:
        reasons.append("从未被访问，F 取下限 0.0（不惩罚历史记忆，仅作为分量之一）")
    return round(value, 2), reasons


def _soft_ceiling(raw: float, limit: float = 100.0, span: float = 10.0) -> float:
    """Map an unbounded score into [0, limit) while preserving order.

    A hard clamp collapses every value above the limit onto the same number,
    which silently destroys the ranking between sources (`verified` and
    `user-stated` both becoming 100). This is continuous at the knee
    (`limit - span`), strictly increasing everywhere, and asymptotic to `limit`,
    so scores stay comparable and never hit the ceiling.
    """
    knee = limit - span
    if raw <= knee:
        return max(0.0, raw)
    excess = raw - knee
    return knee + span * (excess / (excess + span))


def score_credibility(memory: dict, context: dict, rules: dict) -> tuple[float, list[str]]:
    """C: source credibility. Explicitly stated facts outrank unsupported inference."""
    reasons: list[str] = []
    source = str(memory.get("source") or "").strip().lower()
    base = float(rules.get("credibility_default", 60.0))
    matched = None
    for prefix, score in rules.get("credibility_by_source_prefix", []):
        if source.startswith(prefix):
            base, matched = float(score), prefix
            break
    if matched:
        reasons.append(f"来源 '{source}' 匹配显式陈述前缀 '{matched}' → 基准 C={base:.1f}")
    else:
        reasons.append(
            f"来源 '{source or '(空)'}' 未匹配任何已知前缀（视为无证据推断） → 基准 C={base:.1f}"
        )

    try:
        confidence = float(memory.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    weight = float(rules.get("credibility_confidence_weight", 0.25))
    # confidence 0.5 => no shift; below 0.5 lowers, above raises.
    shift = (confidence - 0.5) * 2.0 * 100.0 * weight
    reasons.append(f"confidence={confidence:.2f}（0-1）贡献 {shift:+.1f} 分")
    value = _soft_ceiling(base + shift)
    if value < base + shift - 1e-9:
        reasons.append(
            f"总分 {base + shift:.1f} 超出量程，已软压缩至 {value:.2f}"
            f"（保留来源之间的相对排序）"
        )
    return round(value, 2), reasons


def score_stability(memory: dict, context: dict, rules: dict) -> tuple[float, list[str]]:
    """S: factual stability -- how likely the fact stays true over time."""
    reasons: list[str] = []
    value = float(rules.get("stability_base", 65.0))
    reasons.append(f"稳定性基准 S={value:.1f}")

    category = str(memory.get("category") or "").strip().lower()
    stable = {c.lower() for c in rules.get("stable_categories", set())}
    volatile = {c.lower() for c in rules.get("volatile_categories", set())}
    if category in stable:
        bonus = float(rules.get("stability_stable_bonus", 25.0))
        value += bonus
        reasons.append(f"category='{category}' 属稳定类别 → {bonus:+.1f}")
    elif category in volatile:
        penalty = float(rules.get("stability_volatile_penalty", 25.0))
        value -= penalty
        reasons.append(f"category='{category}' 属易变类别 → {-penalty:+.1f}")

    expires_at = parse_ts(memory.get("expires_at"))
    if expires_at is not None:
        penalty = float(rules.get("stability_expiry_penalty", 20.0))
        days_left = _days_between(_now(), expires_at) or 0.0
        if days_left < 30:
            value -= penalty
            reasons.append(f"已声明有效期且剩余 {days_left:.0f} 天 → {-penalty:+.1f}")
        else:
            reasons.append(f"已声明有效期且剩余 {days_left:.0f} 天，暂不扣分")

    if context.get("is_superseded"):
        penalty = float(rules.get("stability_superseded_penalty", 30.0))
        value -= penalty
        reasons.append(f"该记忆已被更新的记忆替代 → {-penalty:+.1f}")
    if context.get("supersedes_older"):
        bonus = float(rules.get("stability_superseded_by_bonus", 15.0))
        value += bonus
        reasons.append(f"该记忆是当前生效版本（替代了旧记忆） → {bonus:+.1f}")

    update_count = context.get("update_count")
    if update_count:
        n = max(0, int(update_count))
        penalty = clamp(20.0 * math.log1p(n) / math.log1p(5.0), 0.0, 20.0)
        value -= penalty
        reasons.append(f"历史上被修改 {n} 次 → {-penalty:+.1f}")

    return round(clamp(value), 2), reasons


def score_freshness(memory: dict, context: dict, rules: dict, now: datetime | None = None) -> tuple[float, list[str]]:
    """Freshness decays with a half-life but has a floor:
    a long-unaccessed memory is stale, NOT invalid."""
    reasons: list[str] = []
    now = now or _now()
    half_life = max(1.0, float(rules.get("freshness_half_life_days", 120.0)))
    floor = clamp(float(rules.get("freshness_min", 20.0)))

    last_accessed = parse_ts(memory.get("last_accessed_at"))
    reference = last_accessed or parse_ts(memory.get("updated_at")) or parse_ts(memory.get("created_at"))
    if reference is None:
        reasons.append("无可用时间戳 → 时效性取安全默认值 70.0")
        return 70.0, reasons

    days = _days_between(reference, now)
    if days is None or days < 0:
        days = 0.0
    decayed = 100.0 * (0.5 ** (days / half_life))
    value = clamp(max(decayed, floor))
    origin = "最近访问时间" if last_accessed else "更新时间"
    reasons.append(
        f"距{origin} {days:.1f} 天，半衰期 {half_life:.0f} 天 → 衰减后 {decayed:.1f}，下限 {floor:.1f}"
    )
    return round(value, 2), reasons


def score_memory(memory: dict, context: dict | None = None,
                 rules: dict | None = None, now: datetime | None = None) -> dict:
    """Score one memory. Returns components, total and reasons. Writes nothing."""
    context = context or {}
    rules = rules or load_rules()
    now = now or _now()
    weights = rules.get("importance_weights", {})

    r, r_reasons = score_relevance(memory, context, rules)
    f, f_reasons = score_frequency(memory, context, rules)
    c, c_reasons = score_credibility(memory, context, rules)
    s, s_reasons = score_stability(memory, context, rules)
    freshness, fresh_reasons = score_freshness(memory, context, rules, now=now)

    w_r = float(weights.get("relevance", 0.35))
    w_f = float(weights.get("frequency", 0.25))
    w_c = float(weights.get("credibility", 0.20))
    w_s = float(weights.get("stability", 0.20))
    total_weight = w_r + w_f + w_c + w_s
    if total_weight <= 0:
        w_r, w_f, w_c, w_s, total_weight = 0.35, 0.25, 0.20, 0.20, 1.0

    importance = clamp((w_r * r + w_f * f + w_c * c + w_s * s) / total_weight)

    return {
        "memory_id": memory.get("id"),
        "relevance": r,
        "frequency": f,
        "credibility": c,
        "stability": s,
        "freshness": freshness,
        "importance": round(importance, 2),
        "confidence_score": round(c, 2),
        "weights": {"relevance": w_r, "frequency": w_f, "credibility": w_c, "stability": w_s},
        "formula": "I = 0.35R + 0.25F + 0.20C + 0.20S (initial configuration, not data-validated)",
        "rule_version": rules.get("rule_version"),
        "evaluated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "reasons": {
            "relevance": r_reasons,
            "frequency": f_reasons,
            "credibility": c_reasons,
            "stability": s_reasons,
            "freshness": fresh_reasons,
        },
    }


def archive_candidate(memory: dict, scores: dict, rules: dict | None = None) -> tuple[bool, list[str]]:
    """Decide whether a memory may enter the archive-candidate set.

    Rules: protected memories never auto-archive; freshness alone is never
    enough (a long-unaccessed memory is stale, not invalid).
    """
    rules = rules or load_rules()
    reasons: list[str] = []
    if memory.get("protected") in (1, True, "1", "true"):
        reasons.append("记忆受保护（protected=1），永不进入自动归档候选")
        return False, reasons
    if str(memory.get("memory_status") or GOV_ACTIVE_DEFAULT) == GOV_STATUS_PENDING_REVIEW:
        reasons.append("记忆处于待审核状态，需先人工裁决")
        return False, reasons

    importance = float(scores.get("importance", 50.0))
    freshness = float(scores.get("freshness", 70.0))
    imp_threshold = float(rules.get("archive_importance_threshold", 30.0))
    fresh_threshold = float(rules.get("archive_freshness_threshold", 25.0))
    observation = int(rules.get("observation_period_days", 14))

    if importance > imp_threshold:
        reasons.append(f"重要性 {importance:.1f} > 阈值 {imp_threshold:.1f}，不满足低分条件")
        return False, reasons
    if freshness > fresh_threshold:
        reasons.append(
            f"时效性 {freshness:.1f} > 阈值 {fresh_threshold:.1f}；"
            f"仅因长期未访问不足以判定失效（需观察期 {observation} 天）"
        )
        return False, reasons
    reasons.append(
        f"重要性 {importance:.1f} ≤ {imp_threshold:.1f} 且时效性 {freshness:.1f} ≤ {fresh_threshold:.1f}"
        f"；需连续观察 {observation} 天后才可进入归档审批"
    )
    return True, reasons
