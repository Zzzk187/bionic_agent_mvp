"""Deterministic duplicate / conflict classification for Phase-5 governance.

Design rules (from the Phase-5 task book, section 7):

* Semantic similarity alone is NOT a conflict.
* Two different preferences can both be true at the same time.
* Facts from different times, projects or scopes must not be merged silently.
* An explicit correction creates/updates a NEW version and keeps the history
  (supersede relation); it never silently overwrites the old memory.
* Anything uncertain goes to the review queue instead of being auto-resolved.
* No LLM is involved: every verdict is reproducible and carries reasons.

This module is a pure function of (memory A, memory B, similarity, rules).
"""
from __future__ import annotations

import re

# Categories where two distinct values are expected to coexist (preferences,
# tastes, profile attributes) rather than contradict each other.
COEXISTING_CATEGORIES = {"preference", "preferences", "profile", "taste", "habit", "style"}

_LATIN_TOKEN = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def _tokens(text: str) -> set[str]:
    """CJK character bigrams + latin words. Dependency-free tokenisation."""
    text = normalize_text(text)
    if not text:
        return set()
    tokens: set[str] = set()
    for word in _LATIN_TOKEN.findall(text):
        if len(word) > 1:
            tokens.add(word)
    cjk = "".join(_CJK.findall(text))
    if len(cjk) == 1:
        tokens.add(cjk)
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i:i + 2])
    return tokens


def lexical_similarity(a: str, b: str) -> float:
    """Jaccard overlap used as a fallback when no vector is stored."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return round(inter / union, 6) if union else 0.0


def has_signal(text: str, signals) -> list[str]:
    low = normalize_text(text)
    return [s for s in signals if normalize_text(s) in low]


def _temporal_markers(text: str, rules: dict) -> list[str]:
    return has_signal(text, rules.get("temporal_signals", ()))


def _negation(text: str, rules: dict) -> list[str]:
    return has_signal(text, rules.get("negation_signals", ()))


def _tags_of(row: dict) -> set[str]:
    import json
    try:
        tags = json.loads(row.get("tags_json") or "[]")
    except (TypeError, ValueError):
        tags = []
    if isinstance(tags, list):
        return {str(t).strip().lower() for t in tags if str(t).strip()}
    return set()


def classify_pair(row_a: dict, row_b: dict, similarity: float, rules: dict) -> dict | None:
    """Classify the relationship between two memories.

    Returns None when the pair is not related enough to be worth reporting.
    """
    sim = float(similarity or 0.0)
    min_sim = float(rules.get("similarity_min", 0.60))
    if sim < min_sim:
        return None

    rel_sim = float(rules.get("similarity_related", 0.75))
    dup_sim = float(rules.get("similarity_duplicate", 0.92))

    content_a = row_a.get("content") or ""
    content_b = row_b.get("content") or ""
    cat_a = (row_a.get("category") or "").strip().lower()
    cat_b = (row_b.get("category") or "").strip().lower()

    correction_b = has_signal(content_b, rules.get("correction_signals", ()))
    correction_a = has_signal(content_a, rules.get("correction_signals", ()))
    neg_a = _negation(content_a, rules)
    neg_b = _negation(content_b, rules)
    tempor_a = set(_temporal_markers(content_a, rules))
    tempor_b = set(_temporal_markers(content_b, rules))
    tags_a, tags_b = _tags_of(row_a), _tags_of(row_b)

    # ---- explicit correction wins over every other interpretation ----------
    # The newer memory (b, by created_at/id) explicitly rewrites the older one.
    if correction_b and sim >= rel_sim:
        return _verdict(
            rules, "correction", sim, 0.55 + 0.35 * sim,
            f"候选记忆含明确纠正信号 {correction_b[:3]}，与旧记忆相似度 {sim:.2f}",
            "supersede",
            "以候选记忆为新版本：归档旧记忆并建立 supersedes 关系，保留历史（不覆盖原内容）",
        )

    # ---- different project / scope: must not be merged --------------------
    scope_split = bool(cat_a and cat_b and cat_a != cat_b)
    disjoint_tags = bool(tags_a and tags_b and not (tags_a & tags_b))
    if (scope_split or disjoint_tags) and sim >= min_sim and not (neg_a and not neg_b or neg_b and not neg_a):
        detail = f"category {cat_a or '-'} vs {cat_b or '-'}" if scope_split else f"标签无交集 {sorted(tags_a)[:3]} vs {sorted(tags_b)[:3]}"
        return _verdict(
            rules, "different_scope", sim, 0.6,
            f"虽然相似度 {sim:.2f}，但属于不同范围（{detail}），不同项目/上下文的事实不得合并",
            "keep_both", "两条都保留，不做合并",
        )

    # ---- time-shifted facts ----------------------------------------------
    if tempor_a and tempor_b and tempor_a != tempor_b:
        return _verdict(
            rules, "temporal_change", sim, 0.6,
            f"双方时间标记不同（{sorted(tempor_a)[:3]} vs {sorted(tempor_b)[:3]}），"
            f"可能是同一事实在不同时间的取值",
            "keep_both_time_note", "两条都保留，标注各自生效时间，不合并",
        )

    # ---- one side negates the other: uncertain, needs a human -------------
    if bool(neg_a) != bool(neg_b) and sim >= rel_sim:
        return _verdict(
            rules, "possible_conflict", sim, 0.5,
            f"一方含否定表述（{sorted(neg_a or neg_b)[:3]}）而另一方没有，"
            f"相似度 {sim:.2f}，语义相似不等于冲突，需要人工裁决",
            "review", "进入待审核队列，由人工裁决后再执行",
        )

    # ---- preferences coexist ---------------------------------------------
    # Only when the two actually say something different. Two byte-identical
    # preferences are a duplicate, not two coexisting tastes.
    coexisting = rules.get("coexisting_categories") or COEXISTING_CATEGORIES
    if (cat_a in coexisting and cat_b in coexisting
            and sim >= rel_sim and sim < dup_sim):
        return _verdict(
            rules, "supplement", sim, 0.7,
            f"两条同属偏好/画像类（{cat_a}），两种不同偏好可以同时成立，相似度 {sim:.2f}",
            "keep_both", "两条都保留",
        )

    # ---- near identical ---------------------------------------------------
    if sim >= dup_sim:
        return _verdict(
            rules, "duplicate", sim, min(0.95, 0.5 + sim / 2),
            f"相似度 {sim:.2f} 超过重复阈值 {dup_sim:.2f}，且未检测到否定或纠正信号",
            "archive_duplicate",
            "保留评分较高/较新的一条，归档另一条（归档可逆，不物理删除，需审批后执行）",
        )

    # ---- related but not identical ---------------------------------------
    if sim >= rel_sim:
        return _verdict(
            rules, "supplement", sim, 0.5,
            f"相似度 {sim:.2f} 位于相关区间，内容互补且无矛盾信号",
            "keep_both", "两条都保留",
        )

    # ---- above the floor but undecidable ---------------------------------
    return _verdict(
        rules, "undetermined", sim, 0.3,
        f"相似度 {sim:.2f} 仅略高于阈值 {min_sim:.2f}，证据不足以判定",
        "review", "证据不足，进入待审核队列，不做任何自动修改",
    )


def _verdict(rules: dict, ctype: str, similarity: float, confidence: float,
             reason: str, recommendation: str, action_hint: str) -> dict:
    return {
        "conflict_type": ctype,
        "similarity": round(float(similarity), 6),
        "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
        "reason": reason,
        "recommendation": recommendation,
        "recommended_action": action_hint,
        "rule_version": rules.get("rule_version", ""),
    }


def needs_review(verdict: dict) -> bool:
    """Uncertain verdicts go to the review queue instead of being applied."""
    return verdict.get("conflict_type") in ("possible_conflict", "undetermined")
