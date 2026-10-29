"""Phase 7: activation spreading over the memory relation graph.

Goal (from the project plan): when a memory is *activated* (retrieved, used,
marked helpful, or explicitly re-activated by the user), that activation should
spread to its neighbours in the relation graph so that closely related memories
rise in relevance — but WITHOUT silently mutating stored scores, and WITHOUT
ever pushing a memory into archive candidacy.

Design rules (must hold for every change here):
  * Pure + read-mostly. Spreading is computed on demand over the graph; it never
    writes to `memories`, never changes `importance_score`/`memory_status`.
  * Deterministic: same (graph, seeds, params) -> same output. No randomness.
  * Explainable: every activation contribution names its seed + depth + decay.
  * Bounded: activation is in [0, 1]; it is folded into relevance R with a soft
    ceiling so it can never inflate a memory's importance without bound.
  * Non-destructive: the only write this module performs is an *audit event*
    (`memory.activated`) when a human explicitly re-activates a memory. That is
    a log line, not a state change.
  * Protection-aware: protected memories may still be recalled/spread (recall is
    not destructive); spreading is purely a ranking signal.
"""
from __future__ import annotations

import json
import math
from typing import Optional

from app import db
from app import governance_rules as rules

# Relations across which activation spreads. `contradicts` is deliberately
# excluded: a contradictory memory is NOT a relevance ally. All other relations
# are treated as undirected for spreading (a -> b and b -> a both count).
_SPREAD_RELATIONS = (
    "related_to", "supports", "supersedes", "derived_from",
    "part_of", "similar_to", "occurred_before", "caused_by",
)

DEFAULT_DECAY = 0.5          # activation multiplier per graph hop
DEFAULT_MAX_DEPTH = 2        # how many hops activation may travel
DEFAULT_WINDOW_DAYS = 30     # recency window for "recently activated" seeds


# --------------------------------------------------------------------------
# Graph construction
# --------------------------------------------------------------------------
def build_activation_graph(conn) -> dict[int, list[tuple[int, str]]]:
    """Adjacency list {memory_id: [(neighbour_id, relation), ...]}.

    Uses the existing `memory_links` table plus the `supersedes_id` column so
    the graph is exactly the one already used by Phase 6 relations — no new
    table, no new link type.
    """
    graph: dict[int, list[tuple[int, str]]] = {}
    links = conn.execute(
        "SELECT source_memory_id, target_memory_id, relation FROM memory_links"
    ).fetchall()
    for lk in links:
        rel = (lk["relation"] or "related_to")
        if rel not in _SPREAD_RELATIONS:
            continue
        a, b = int(lk["source_memory_id"]), int(lk["target_memory_id"])
        graph.setdefault(a, []).append((b, rel))
        graph.setdefault(b, []).append((a, rel))
    # supersedes_id: the NEW memory supersedes the OLD one; activation of the
    # new version should spread to the old (and vice-versa for context).
    rows = conn.execute(
        "SELECT id, supersedes_id FROM memories WHERE supersedes_id IS NOT NULL"
    ).fetchall()
    for r in rows:
        new_id, old_id = int(r["id"]), int(r["supersedes_id"])
        graph.setdefault(new_id, []).append((old_id, "supersedes"))
        graph.setdefault(old_id, []).append((new_id, "supersedes"))
    return graph


# --------------------------------------------------------------------------
# Seed activation from a memory's own signals
# --------------------------------------------------------------------------
def seed_activation_from_row(row: dict, window_days: int = DEFAULT_WINDOW_DAYS) -> float:
    """Derive a 0..1 activation seed from a memory's real usage signals.

    Higher when the memory has been used/helpful or recently accessed. This is a
    *relevance* signal, not a freshness signal: a memory that keeps being useful
    stays relevant. Safe defaults for missing data (legacy rows) -> 0.0 so they
    don't get a free boost; explicit seeds (see spread_activation) default to 1.
    """
    from app.governance_scoring import parse_ts
    try:
        access_count = max(0, int(row.get("access_count") or 0))
    except (TypeError, ValueError):
        access_count = 0
    # access frequency component (log-normalised, saturates at 20 accesses)
    freq = min(1.0, math_log1p(access_count) / math_log1p(20.0))
    # recency component
    last = parse_ts(row.get("last_accessed_at")) or parse_ts(row.get("updated_at"))
    recency = 0.0
    if last is not None:
        from datetime import datetime, timezone
        days = (datetime.now(timezone.utc) - last).total_seconds() / 86400.0
        if days < window_days:
            recency = 1.0 - (days / max(1.0, window_days))
    # supersedes_older => it is the current live version => a relevance ally
    is_live = bool(row.get("supersedes_id"))
    base = 0.4 * freq + 0.6 * recency
    if is_live:
        base = min(1.0, base + 0.2)
    return round(min(1.0, max(0.0, base)), 4)


def _recently_activated_seeds(conn, window_days: int = DEFAULT_WINDOW_DAYS,
                              limit: int = 200) -> dict[int, float]:
    """Seeds from explicit `memory.activated` events in the audit trail.

    These are human-driven re-activations; they count as full-strength seeds so
    a memory the user just said "this is relevant again" spreads to its graph.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days))
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT detail FROM events WHERE event_type='memory.activated' "
        "AND created_at >= ? ORDER BY id DESC LIMIT ?",
        (cutoff_str, int(limit)),
    ).fetchall()
    seeds: dict[int, float] = {}
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except (TypeError, ValueError):
            continue
        mid = d.get("memory_id")
        if mid is None:
            continue
        seeds[int(mid)] = 1.0  # explicit human activation = full seed
    return seeds


# --------------------------------------------------------------------------
# Core spreading
# --------------------------------------------------------------------------
def spread_activation(graph: dict[int, list[tuple[int, str]]],
                      seeds: dict[int, float],
                      decay: float = DEFAULT_DECAY,
                      max_depth: int = DEFAULT_MAX_DEPTH,
                      exclude: Optional[set[int]] = None) -> dict[int, dict]:
    """BFS spreading from `seeds` ({memory_id: activation 0..1}) over `graph`.

    Returns {memory_id: {activation, contributions:[{seed,depth,relation,amount}]}}.
    Contributions are recorded so the result is fully explainable. A node's
    activation is the max over all (seed, path) arrivals, which keeps the value
    bounded in [0,1] and avoids double-counting from multiple paths.
    """
    exclude = exclude or set()
    result: dict[int, dict] = {}
    # frontier holds (node, incoming_activation, depth, seed_id, relation)
    frontier: list[tuple[int, float, int, int, str]] = []
    for sid, act in seeds.items():
        if sid in exclude:
            continue
        if act <= 0:
            continue
        result.setdefault(sid, {"activation": 0.0, "contributions": []})
        if act > result[sid]["activation"]:
            result[sid]["activation"] = act
            result[sid]["contributions"] = [{
                "seed": sid, "depth": 0, "relation": "seed", "amount": round(act, 4)
            }]
        frontier.append((sid, act, 0, sid, "seed"))

    visited_depth: dict[int, int] = {sid: 0 for sid in seeds}
    while frontier:
        nxt = []
        for (node, act, depth, seed_id, rel) in frontier:
            if depth >= max_depth:
                continue
            child_act = act * decay
            if child_act < 0.01:
                continue
            for (neighbour, edge_rel) in graph.get(node, []):
                if neighbour in exclude:
                    continue
                rec = result.setdefault(neighbour, {"activation": 0.0, "contributions": []})
                if child_act > rec["activation"]:
                    rec["activation"] = round(child_act, 4)
                    rec["contributions"] = [{
                        "seed": seed_id, "depth": depth + 1,
                        "relation": edge_rel, "amount": round(child_act, 4)
                    }]
                else:
                    # record the extra path only if it is the same or better
                    if any(c["seed"] == seed_id and c["depth"] == depth + 1
                           for c in rec["contributions"]):
                        continue
                    rec["contributions"].append({
                        "seed": seed_id, "depth": depth + 1,
                        "relation": edge_rel, "amount": round(child_act, 4)
                    })
                # allow re-expansion from a better/deeper arrival up to max_depth
                new_depth = depth + 1
                if neighbour not in visited_depth or new_depth < visited_depth[neighbour]:
                    visited_depth[neighbour] = new_depth
                    nxt.append((neighbour, child_act, new_depth, seed_id, edge_rel))
        frontier = nxt

    # round + sort contributions for stable, readable output
    for rec in result.values():
        rec["contributions"].sort(key=lambda c: (c["depth"], -(c["amount"] or 0)))
        rec["activation"] = round(min(1.0, rec["activation"]), 4)
    return result


# --------------------------------------------------------------------------
# High-level: graph + seeds in one read
# --------------------------------------------------------------------------
def compute_activation(memory_ids: Optional[list[int]] = None,
                       decay: float = DEFAULT_DECAY,
                       max_depth: int = DEFAULT_MAX_DEPTH,
                       include_recent_events: bool = True,
                       window_days: int = DEFAULT_WINDOW_DAYS,
                       conn=None) -> dict:
    """Build the graph, gather seeds, and return the spread result.

    Seeds = explicit recent activations (if requested) + the provided
    `memory_ids` (each as a full-strength seed of 1.0, e.g. the memory the user
    just retrieved). If `memory_ids` is None and include_recent_events is False,
    returns an empty result (nothing to spread from).
    """
    def _run(c):
        graph = build_activation_graph(c)
        seeds: dict[int, float] = {}
        if include_recent_events:
            seeds.update(_recently_activated_seeds(c, window_days))
        if memory_ids:
            for mid in memory_ids:
                seeds[int(mid)] = 1.0
        if not seeds:
            return {"ok": True, "activation": {}, "seeds": 0,
                    "graph_edges": sum(len(v) for v in graph.values()) // 2}
        spread = spread_activation(graph, seeds, decay=decay, max_depth=max_depth)
        return {"ok": True, "activation": spread, "seeds": len(seeds),
                "graph_edges": sum(len(v) for v in graph.values()) // 2}
    if conn is not None:
        return _run(conn)
    with db.connect() as conn:
        return _run(conn)


def activation_for_seed_query(conn, query_top_ids: list[int],
                             decay: float = DEFAULT_DECAY,
                             max_depth: int = DEFAULT_MAX_DEPTH) -> dict[int, dict]:
    """Spread activation from the memories just recalled for a query.

    Used by unified recall: if the query recalled id A (relevance high), A's
    neighbours get a relevance boost. Returns {memory_id: spread_record}.
    """
    if not query_top_ids:
        return {}
    spread = compute_activation(memory_ids=query_top_ids, decay=decay,
                               max_depth=max_depth, include_recent_events=True,
                               conn=conn)
    return spread.get("activation", {})


# --------------------------------------------------------------------------
# Explicit human activation (audit-only write)
# --------------------------------------------------------------------------
def record_activation(memory_id: int, reason: str = "", approver: str = "user") -> dict:
    """Log a human-driven re-activation. Purely an audit event; no state change.

    This exists so that a user can say "this memory is relevant again" and have
    it seed the spreading graph for the next window. It never modifies
    importance_score, status, or memory_status, and it never archives anything.
    """
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
                (None, "memory.activated",
                 json.dumps({"memory_id": int(memory_id), "reason": reason[:500],
                             "by": approver[:100]}, ensure_ascii=False)))
        return {"ok": True, "memory_id": int(memory_id),
                "message": "已记录激活事件（仅审计，不改变记忆状态）"}
    except Exception as exc:
        return {"ok": False, "memory_id": int(memory_id), "message": f"激活记录失败：{exc}"}


# small local log1p to avoid importing math twice at module top (keeps header clean)
def math_log1p(x: float) -> float:
    return math.log1p(x)
