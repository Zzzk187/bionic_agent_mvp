"""Phase 6 + 7 memory lifecycle, types, unified recall, feedback and consolidation.

Design rules (must hold for every change in this file):
  * Purely additive on top of Phases 1-5. No column drops, no table rebuilds,
    no rewrite of existing rows, no change to Phase 1-4 tool semantics.
  * `memories.status` stays the single retrieval filter (active/archived);
    `memories.memory_status` is the governance VIEW field
    (active/pending_review/superseded/archived). The two are NEVER merged.
  * Recall never returns archived, pending_review or superseded memories unless
    explicitly asked (via the original, unchanged tools).
  * Consolidation only GENERATES candidates; turning a candidate into a real
    memory change requires explicit approval + execution and is fully audited
    and reversible. Unverified inference is never promoted to a fact.
  * All multi-table mutations run inside `db.transaction()` so a failure leaves
    no half-written state.
"""
from __future__ import annotations

import json
import math
import re
from typing import Optional

from app import db
from app.embeddings import EmbeddingUnavailable
from app import governance_service as gov
from app import governance_rules as rules
from app import governance_scoring as scoring
from app import governance_conflicts as conflicts
from app import activation_spreading as spreading

# Governance columns are stripped from recall output so the fused retrieval
# stays compatible with the Phase 1-4 response shape. Mirrors mcp_server._memory_out.
_GOV_COLUMNS = {
    "importance_score", "confidence_score", "freshness_score", "access_count",
    "last_accessed_at", "last_evaluated_at", "memory_status", "protected",
}


def _clean(row: dict) -> dict:
    result = dict(row)
    if "tags_json" in result:
        try:
            result["tags"] = json.loads(result.pop("tags_json") or "[]")
        except (TypeError, ValueError):
            result["tags"] = []
            result.pop("tags_json", None)
    for k in _GOV_COLUMNS:
        result.pop(k, None)
    return result

# --------------------------------------------------------------------------
# Versioned configuration (deterministic, not data-validated weights)
# --------------------------------------------------------------------------
LIFECYCLE_RULE_VERSION = "phase6-lifecycle-v1"

# Recall fusion weights (sum-normalised at query time). These are stable rules,
# not adaptive weights: the task forbids adaptive ranking unless enough feedback
# exists AND it is validated; we ship stable rules so recall is reproducible.
RECALL_WEIGHTS = {
    "semantic": 0.38,
    "keyword": 0.17,
    "importance": 0.16,
    "confidence": 0.10,
    "freshness": 0.07,
    "frequency": 0.07,
    "activation": 0.05,
}

# Memory-type taxonomy. `unknown` means the row has no type yet (legacy data).
MEMORY_TYPES = ("episodic", "semantic", "preference", "procedural", "goal", "reflection", "unknown")

# Allowed relation vocabulary for the richer graph (reuses existing semantics).
ALLOWED_RELATIONS = (
    "related_to", "supports", "contradicts", "supersedes", "derived_from",
    "part_of", "occurred_before", "caused_by", "similar_to",
)
# Directed relations that must not form a cycle.
DIRECTED_RELATIONS = ("derived_from", "part_of", "occurred_before", "caused_by", "supersedes")

# Legal lifecycle transitions. Key = from-state, value = set of to-states.
# States map onto (status, memory_status) pairs; see LIFECYCLE_STATE_OF.
LEGAL_TRANSITIONS = {
    "retrievable": {"pending_review", "archived", "superseded"},
    "pending_review": {"retrievable", "archived"},
    "superseded": {"archived"},
    "archived": {"retrievable"},
}

LIFECYCLE_STATES = ("retrievable", "pending_review", "superseded", "archived")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _json(x, default="{}"):
    if x is None or x == "":
        return default
    if isinstance(x, (dict, list)):
        return json.dumps(x, ensure_ascii=False)
    return x


def _json_loads(x, default=None):
    if x is None or x == "":
        return default if default is not None else {}
    try:
        return json.loads(x)
    except (TypeError, ValueError):
        return default if default is not None else {}


# --------------------------------------------------------------------------
# Lifecycle state mapping (status/memory_status -> lifecycle state)
# --------------------------------------------------------------------------
def lifecycle_state_of(row) -> str:
    """Map a memory row to one of the four lifecycle states.

    NOTE: sqlite3.Row does NOT support `"key" in row` (the `in` operator tests
    values, not keys), so we read keys via row.keys() once.
    """
    keys = set(row.keys())
    status = (row["status"] if "status" in keys else None) or "active"
    mstatus = (row["memory_status"] if "memory_status" in keys else None) or status
    # memory_status (the governance view) takes precedence over the raw
    # retrieval status, so a superseded/archived row keeps its real state.
    if mstatus == "superseded":
        return "superseded"
    if mstatus == "pending_review":
        return "pending_review"
    if status == "archived":
        return "archived"
    return "retrievable"


def lifecycle_transitions(row) -> list[str]:
    return sorted(LEGAL_TRANSITIONS.get(lifecycle_state_of(row), set()))


# --------------------------------------------------------------------------
# Memory-type classification (deterministic; never elevates inference to fact)
# --------------------------------------------------------------------------
_TYPE_HINTS = [
    ("preference", re.compile(r"\b(prefer|preference|like|dislike|want|need|wish|would rather|习惯|偏好|喜欢|讨厌|不想)\b", re.I)),
    ("procedural", re.compile(r"\b(how to|step|procedure|workflow|run|execute|command|script|recipe|流程|步骤|如何|怎么|命令)\b", re.I)),
    ("goal", re.compile(r"\b(goal|objective|plan|target|aim|milestone|目标|计划|任务|打算)\b", re.I)),
    ("reflection", re.compile(r"\b(lesson|learned|reflection|retrospective|insight|mistake|复盘|教训|反思|学到的)\b", re.I)),
    ("episodic", re.compile(r"\b(yesterday|last time|happened|meeting|on monday|we discussed|昨天|上次|发生|会议|我们讨论)\b", re.I)),
]


def classify_memory_type(content: str, category: str = "", source: str = "") -> dict:
    """Deterministic, explainable type guess. Never returns a forced bucket.

    Returns {memory_type, confidence, reasons, needs_review}. Low-confidence or
    ambiguous classifications set needs_review=True so a human can adjudicate;
    the data is never lost and the memory stays fully searchable.
    """
    text = f"{category} {content} {source}".strip()
    hits = []
    for t, pat in _TYPE_HINTS:
        if pat.search(text):
            hits.append(t)
    # Category is a stronger signal than free text.
    cat = (category or "").lower().strip()
    if cat in MEMORY_TYPES and cat != "unknown":
        if cat not in hits:
            hits.insert(0, cat)
    reasons = [f"matched '{h}' signals" for h in hits]
    if not hits:
        return {
            "memory_type": "unknown",
            "confidence": 0.0,
            "reasons": ["no type signal matched; legacy/untyped memory kept as 'unknown'"],
            "needs_review": False,
        }
    # Multiple distinct type hints => ambiguous.
    distinct = sorted(set(hits))
    if len(distinct) > 1:
        return {
            "memory_type": distinct[0],
            "confidence": 0.4,
            "reasons": reasons + [f"ambiguous: also matched {distinct[1:]}"],
            "needs_review": True,
        }
    conf = 0.85 if cat == distinct[0] else 0.6
    return {
        "memory_type": distinct[0],
        "confidence": conf,
        "reasons": reasons,
        "needs_review": conf < 0.6,
    }


# --------------------------------------------------------------------------
# Unified recall / retrieval orchestration
# --------------------------------------------------------------------------
def _tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    # latin words + CJK unigrams (cheap, dependency-free)
    toks = set(re.findall(r"[a-z0-9]+", text))
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    toks.update(cjk)
    return toks


def _keyword_score(query: str, content: str) -> float:
    q = _tokenize(query)
    c = _tokenize(content)
    if not q:
        return 0.0
    inter = q & c
    if not inter:
        return 0.0
    return len(inter) / math.sqrt(len(q) * max(1, len(c)))


def _soft_freq(access_count: int) -> float:
    n = max(0, int(access_count or 0))
    # mirrors governance_scoring soft ceiling to avoid inflation.
    raw = 100.0 * math.log1p(n) / math.log1p(rules.FREQUENCY_SATURATION_ACCESSES)
    knee = 90.0
    if raw <= knee:
        return raw
    excess = raw - knee
    return knee + rules.FREQUENCY_OVERFLOW_HEADROOM * (excess / (excess + rules.FREQUENCY_OVERFLOW_HEADROOM))


def _keyword_fallback(query: str, limit: int = 10, category: str = "") -> list:
    """Return active, non-excluded memories ranked by keyword overlap only.

    Used as the immediate-degradation path when the embedding model is still
    loading, so a semantic search / recall never blocks on a model download.
    Never returns archived, pending_review or superseded memories.
    """
    with db.connect() as conn:
        sql = ("SELECT * FROM memories WHERE status='active' "
               "AND memory_status NOT IN ('pending_review', 'superseded')")
        args: list = []
        if category.strip():
            sql += " AND category=?"
            args.append(category.strip()[:100])
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    scored = [(dict(r), _keyword_score(query, dict(r).get("content", ""))) for r in rows]
    scored = [(d, s) for d, s in scored if s > 0.0]
    scored.sort(key=lambda x: (x[1], x[0].get("updated_at") or "", x[0]["id"]), reverse=True)
    return [d for d, _ in scored[:max(1, min(int(limit), 50))]]


def _semantic_scores(conn, query: str) -> dict[int, float]:
    """Return {memory_id: cosine similarity} for active, indexed memories.

    Degrades to an empty map (caller falls back to keyword) only while the
    embedding model is actively downloading on first use, so a semantic search
    never blocks on the network fetch and trips the MCP client timeout. If the
    model is already cached/loaded, embed_query returns in ~1s and semantic
    ranking applies normally.
    """
    out: dict[int, float] = {}
    if not query.strip():
        return out
    from app import embeddings
    if embeddings.model_state().get("status") == "pending":
        # A first-use download is in progress in the background worker; do not
        # block this request — degrade to keyword and let the caller retry.
        return out
    try:
        qv = embeddings.embed_query(query)
    except EmbeddingUnavailable:
        return out
    except Exception:
        return out
    rows = conn.execute(
        """SELECT m.id, e.vector_json, e.content_hash, m.content
             FROM memories m JOIN memory_embeddings e ON e.memory_id=m.id
            WHERE m.status='active' AND m.memory_status NOT IN ('pending_review', 'superseded')
              AND e.model_name=?""",
        (embeddings.DEFAULT_MODEL,),
    ).fetchall()
    for r in rows:
        if r["content_hash"] != embeddings.content_hash(r["content"]):
            continue
        try:
            vec = json.loads(r["vector_json"])
        except (TypeError, ValueError):
            continue
        out[r["id"]] = embeddings.cosine_similarity(qv, vec)
    return out


def recall_memories(query: str = "", top_k: int = 5, category: str = "",
                    memory_type: str = "", include_related: bool = False,
                    explain: bool = False, conn=None) -> list[dict]:
    """Fused, explainable, reproducible retrieval.

    Combines semantic + keyword + governance scores (importance/confidence/
    freshness/frequency) and optional relation boosting. Never returns
    archived, pending_review or superseded memories. Stable ordering: tie-break
    by (updated_at desc, id desc).
    """
    top_k = max(1, min(int(top_k), 50))

    def _run(c):
        sql = ("SELECT * FROM memories WHERE status='active' "
               "AND memory_status NOT IN ('pending_review', 'superseded')")
        args: list = []
        if category.strip():
            sql += " AND category=?"
            args.append(category.strip()[:100])
        if memory_type.strip() and memory_type.strip() != "unknown":
            sql += " AND memory_type=?"
            args.append(memory_type.strip())
        sql += " ORDER BY id"
        rows = c.execute(sql, args).fetchall()

        sem = _semantic_scores(c, query)
        seeds = [d["id"] for d in rows
                 if (sem.get(d["id"]) is not None and sem.get(d["id"]) >= 0.45)
                 or _keyword_score(query, dict(d).get("content", "")) >= 0.3]
        spread = spreading.activation_for_seed_query(c, seeds) if seeds else {}
        scored = []
        for row in rows:
            d = dict(row)
            kw = _keyword_score(query, d.get("content", ""))
            sm = sem.get(d["id"])
            imp = float(d.get("importance_score") or 0.0)
            conf = float(d.get("confidence_score") or 0.0)
            fr = float(d.get("freshness_score") or 0.0)
            freq = _soft_freq(d.get("access_count"))
            act = spread.get(d["id"], {}).get("activation", 0.0)
            parts = {
                "semantic": (sm if sm is not None else 0.0),
                "keyword": kw,
                "importance": imp / 100.0,
                "confidence": conf / 100.0,
                "freshness": fr / 100.0,
                "frequency": freq / 100.0,
                "activation": act,
            }
            total = sum(parts[k] * w for k, w in RECALL_WEIGHTS.items())
            rec = _clean(d)
            rec["_score"] = round(total, 6)
            if explain:
                rec["_explain"] = {k: round(v, 4) for k, v in parts.items()}
                rec["_weights"] = RECALL_WEIGHTS
                rec["_semantic_available"] = sm is not None
                rec["_activation"] = {
                    "amount": round(act, 4),
                    "contributions": spread.get(d["id"], {}).get("contributions", []),
                }
            scored.append(rec)
        # Stable sort: highest score first, then recency, then id.
        scored.sort(key=lambda x: (x["_score"], x.get("updated_at") or "", x["id"]),
                    reverse=True)
        result = scored[:top_k]
        if include_related:
            ids = [r["id"] for r in result]
            related = _related_subgraph(c, ids, max_depth=1)
            for r in result:
                r["_related"] = related.get(r["id"], [])
        return result

    if conn is not None:
        return _run(conn)
    with db.connect() as conn:
        return _run(conn)


# --------------------------------------------------------------------------
# Relation graph
# --------------------------------------------------------------------------
def _related_subgraph(conn, seed_ids: list[int], max_depth: int = 1) -> dict[int, list[dict]]:
    """BFS over memory_links up to max_depth (cycle-safe via visited set)."""
    out: dict[int, list[dict]] = {sid: [] for sid in seed_ids}
    visited = set(seed_ids)
    frontier = list(seed_ids)
    depth = 0
    while frontier and depth < max_depth:
        nxt = []
        for mid in frontier:
            links = conn.execute(
                "SELECT * FROM memory_links WHERE source_memory_id=? OR target_memory_id=? ORDER BY id",
                (mid, mid),
            ).fetchall()
            for lk in links:
                l = dict(lk)
                other = l["target_memory_id"] if l["source_memory_id"] == mid else l["source_memory_id"]
                direction = "out" if l["source_memory_id"] == mid else "in"
                node = {"memory_id": other, "relation": l["relation"],
                        "direction": direction, "note": l["note"], "depth": depth + 1}
                if other not in visited:
                    visited.add(other)
                    nxt.append(other)
                out.setdefault(mid, []).append(node)
        frontier = nxt
        depth += 1
    return out


def get_related_memories(memory_id: int, max_depth: int = 2, relation: str = "",
                         max_nodes: int = 30) -> dict:
    """Return the relation subgraph around a memory (depth/cycle limited)."""
    max_depth = max(1, min(int(max_depth), 3))
    max_nodes = max(1, min(int(max_nodes), 200))
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "memory not found"}
        visited = {memory_id}
        frontier = [memory_id]
        nodes: list[dict] = []
        edges: list[dict] = []
        depth = 0
        while frontier and depth < max_depth and len(nodes) < max_nodes:
            nxt = []
            for mid in frontier:
                links = conn.execute(
                    "SELECT * FROM memory_links WHERE source_memory_id=? OR target_memory_id=? ORDER BY id",
                    (mid, mid),
                ).fetchall()
                for lk in links:
                    l = dict(lk)
                    if relation and l["relation"] != relation:
                        continue
                    other = l["target_memory_id"] if l["source_memory_id"] == mid else l["source_memory_id"]
                    direction = "out" if l["source_memory_id"] == mid else "in"
                    edges.append({"from": mid, "to": other, "relation": l["relation"],
                                  "direction": direction, "note": l["note"]})
                    if other not in visited:
                        visited.add(other)
                        nxt.append(other)
                        if len(nodes) < max_nodes:
                            nr = conn.execute(
                                "SELECT id, content, category, memory_status, status FROM memories WHERE id=?",
                                (other,)).fetchone()
                            nodes.append(dict(nr) if nr else {"id": other})
            frontier = nxt
            depth += 1
        return {"ok": True, "memory_id": memory_id, "depth": max_depth,
                "nodes": nodes, "edges": edges, "node_count": len(nodes)}


def create_memory_relation(source_memory_id: int, target_memory_id: int,
                           relation: str = "related_to", note: str = "",
                           approver: str = "user") -> dict:
    """Create a typed relation with dedup + cycle prevention for directed edges."""
    relation = (relation or "related_to").strip()
    if relation not in ALLOWED_RELATIONS:
        return {"ok": False, "message": f"unknown relation '{relation}'; allowed: {ALLOWED_RELATIONS}"}
    if source_memory_id == target_memory_id:
        return {"ok": False, "message": "self-loop relations are not allowed"}
    with db.transaction() as conn:
        s = conn.execute("SELECT id FROM memories WHERE id=?", (source_memory_id,)).fetchone()
        t = conn.execute("SELECT id FROM memories WHERE id=?", (target_memory_id,)).fetchone()
        if not s or not t:
            return {"ok": False, "message": "source or target memory not found"}
        dup = conn.execute(
            "SELECT id FROM memory_links WHERE source_memory_id=? AND target_memory_id=? AND relation=?",
            (source_memory_id, target_memory_id, relation)).fetchone()
        if dup:
            return {"ok": True, "message": "relation already exists", "link_id": dup["id"],
                    "already_existed": True}
        if relation in DIRECTED_RELATIONS:
            # Prevent creating a cycle: target must not already reach source.
            reach = _reachable(conn, target_memory_id, relation, {source_memory_id})
            if source_memory_id in reach:
                return {"ok": False, "message": f"would create a cycle for directed relation '{relation}'"}
        cur = conn.execute(
            "INSERT INTO memory_links(source_memory_id, target_memory_id, relation, note, created_at) "
            "VALUES(?,?,?,?,?)",
            (source_memory_id, target_memory_id, relation, note or "", _now()))
        link_id = cur.lastrowid
        conn.execute(
            "INSERT INTO events(task_id, event_type, detail) VALUES(?,?,?)",
            (None, "memory.relation.created",
             json.dumps({"source": source_memory_id, "target": target_memory_id,
                         "relation": relation, "by": approver}, ensure_ascii=False)))
    return {"ok": True, "link_id": link_id, "already_existed": False}


def _reachable(conn, start: int, relation: str, stop: set[int]) -> set[int]:
    seen = set()
    frontier = [start]
    while frontier:
        nxt = []
        for mid in frontier:
            links = conn.execute(
                "SELECT target_memory_id FROM memory_links WHERE source_memory_id=? AND relation=?",
                (mid, relation)).fetchall()
            for r in links:
                o = r["target_memory_id"]
                if o in stop:
                    seen.add(o)
                    continue
                if o not in seen:
                    seen.add(o)
                    nxt.append(o)
        frontier = nxt
    return seen


def list_memory_relations(memory_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "memory not found"}
        links = conn.execute(
            "SELECT * FROM memory_links WHERE source_memory_id=? OR target_memory_id=? ORDER BY id",
            (memory_id, memory_id)).fetchall()
        out = []
        for lk in links:
            l = dict(lk)
            l["direction"] = "out" if l["source_memory_id"] == memory_id else "in"
            out.append(l)
        return {"ok": True, "memory_id": memory_id, "relations": out, "count": len(out)}


# --------------------------------------------------------------------------
# Memory detail view (governance + feedback + relations + lifecycle)
# --------------------------------------------------------------------------
def get_memory_details(memory_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "memory not found"}
        d = dict(row)
        gov_status = gov.get_governance_status(memory_id)
        fb = conn.execute("SELECT * FROM memory_feedback WHERE memory_id=?", (memory_id,)).fetchone()
        rel = list_memory_relations(memory_id)
        from app import mcp_server as _mcp  # local import to avoid cycle at module load
        base = _mcp._memory_out(d)
        detail = {
            "ok": True,
            "memory": base,
            "lifecycle_state": lifecycle_state_of(d),
            "allowed_transitions": lifecycle_transitions(d),
            "governance": gov_status,
            "feedback": dict(fb) if fb else None,
            "relations": rel.get("relations", []),
        }
        return detail


# --------------------------------------------------------------------------
# Usage feedback (traceable, no score inflation)
# --------------------------------------------------------------------------
FEEDBACK_TYPES = ("retrieved", "used", "helpful", "not_relevant", "corrected")


def record_memory_feedback(memory_id: int, feedback_type: str, note: str = "",
                           approver: str = "user") -> dict:
    if feedback_type not in FEEDBACK_TYPES:
        return {"ok": False, "message": f"invalid feedback_type '{feedback_type}'; allowed: {FEEDBACK_TYPES}"}
    col = f"{feedback_type}_count"
    # Only last_retrieved_at / last_used_at are stored as dedicated columns
    # (the rest share last_feedback_at); per-type last_* history is in history_json.
    last_col = "last_retrieved_at" if feedback_type == "retrieved" else (
        "last_used_at" if feedback_type == "used" else None)
    now = _now()
    with db.transaction() as conn:
        row = conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "memory not found"}
        if last_col:
            conn.execute(
                f"INSERT INTO memory_feedback(memory_id, {col}, {last_col}, last_feedback_at, history_json) "
                "VALUES(?,1,?,?,?) "
                "ON CONFLICT(memory_id) DO UPDATE SET "
                f"{col}={col}+1, {last_col}=excluded.{last_col}, "
                "last_feedback_at=excluded.last_feedback_at, "
                "history_json=excluded.history_json",
                (memory_id, now, now, json.dumps([{"t": feedback_type, "at": now}], ensure_ascii=False)))
        else:
            conn.execute(
                f"INSERT INTO memory_feedback(memory_id, {col}, last_feedback_at, history_json) "
                "VALUES(?,1,?,?) "
                "ON CONFLICT(memory_id) DO UPDATE SET "
                f"{col}={col}+1, last_feedback_at=excluded.last_feedback_at, "
                "history_json=excluded.history_json",
                (memory_id, now, json.dumps([{"t": feedback_type, "at": now}], ensure_ascii=False)))
        # Cap history to last 50 entries to avoid unbounded growth.
        fb = conn.execute("SELECT history_json FROM memory_feedback WHERE memory_id=?", (memory_id,)).fetchone()
        hist = _json_loads(fb["history_json"], []) if fb else []
        if len(hist) > 50:
            hist = hist[-50:]
            conn.execute("UPDATE memory_feedback SET history_json=? WHERE memory_id=?",
                         (json.dumps(hist, ensure_ascii=False), memory_id))
        conn.execute(
            "INSERT INTO events(task_id, event_type, detail) VALUES(?,?,?)",
            (None, "memory.feedback",
             json.dumps({"memory_id": memory_id, "type": feedback_type, "by": approver}, ensure_ascii=False)))
    return {"ok": True, "memory_id": memory_id, "feedback_type": feedback_type}


def get_memory_usage_stats(memory_id: int = 0) -> dict:
    with db.connect() as conn:
        if memory_id:
            fb = conn.execute("SELECT * FROM memory_feedback WHERE memory_id=?", (memory_id,)).fetchone()
            return {"ok": True, "memory_id": memory_id, "stats": dict(fb) if fb else {}}
        rows = conn.execute(
            "SELECT m.id, m.content, m.category, f.retrieved_count, f.used_count, "
            "f.helpful_count, f.not_relevant_count, f.corrected_count "
            "FROM memories m LEFT JOIN memory_feedback f ON f.memory_id=m.id "
            "WHERE m.status='active' ORDER BY (f.used_count+f.helpful_count) DESC, m.id"
        ).fetchall()
        return {"ok": True, "count": len(rows),
                "usage": [dict(r) for r in rows]}


# --------------------------------------------------------------------------
# Duplicate / near-duplicate discovery (reuses Phase 5 conflict detection)
# --------------------------------------------------------------------------
def find_duplicate_memories(memory_ids: Optional[list[int]] = None, category: str = "",
                           candidate_count: int = 8) -> dict:
    det = gov.detect_conflicts(memory_ids=memory_ids, category=category,
                                    candidate_count=candidate_count, persist=False)
    findings = det.get("findings", []) if isinstance(det, dict) else det
    dups = [f for f in findings if isinstance(f, dict)
            and f.get("conflict_type") in ("duplicate", "correction")]
    return {"ok": True, "duplicate_count": len(dups), "duplicates": dups}


# --------------------------------------------------------------------------
# Consolidation candidates (generate only; never applied silently)
# --------------------------------------------------------------------------
CONSOL_KINDS = (
    "merge_duplicate", "extract_semantic", "extract_procedural",
    "stale_fact", "missing_source", "conflict_review", "archive_review",
)


def consolidate_memory_candidates(persist: bool = False, limit: int = 50,
                                  category: str = "", memory_type: str = "",
                                  approver: str = "user") -> dict:
    """Scan active memories and emit consolidation CANDIDATES only.

    persist=False  -> preview, no database write (safe default).
    persist=True   -> insert rows into consolidation_candidates (status 'pending').
    Nothing here mutates an existing memory; applying a candidate is a separate,
    approved, audited, reversible step (run_memory_consolidation).
    """
    limit = max(1, min(int(limit), 500))
    rule = rules.load_rules()
    with db.connect() as conn:
        sql = ("SELECT * FROM memories WHERE status='active' "
               "AND memory_status NOT IN ('pending_review', 'superseded')")
        args: list = []
        if category.strip():
            sql += " AND category=?"
            args.append(category.strip()[:100])
        if memory_type.strip() and memory_type.strip() != "unknown":
            sql += " AND memory_type=?"
            args.append(memory_type.strip())
        sql += " ORDER BY id"
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]

        candidates: list[dict] = []
        # 1) duplicates from existing conflict findings
        det = gov.detect_conflicts(category=category, candidate_count=rule.get("candidate_count", 8),
                                   persist=False)
        findings = det.get("findings", []) if isinstance(det, dict) else det
        for f in findings:
            if f.get("conflict_type") in ("duplicate", "correction"):
                candidates.append({
                    "kind": "merge_duplicate",
                    "title": f"合并重复记忆 {f.get('memory_id')} / {f.get('candidate_memory_id')}",
                    "detail": f.get("reason", ""),
                    "source_memory_ids": [f.get("memory_id"), f.get("candidate_memory_id")],
                    "confidence": f.get("confidence", 0.0),
                    "rule_version": f.get("rule_version", rule["rule_version"]),
                })
            elif f.get("conflict_type") in ("possible_conflict", "undetermined"):
                candidates.append({
                    "kind": "conflict_review",
                    "title": f"冲突待裁决 {f.get('memory_id')} / {f.get('candidate_memory_id')}",
                    "detail": f.get("reason", ""),
                    "source_memory_ids": [f.get("memory_id"), f.get("candidate_memory_id")],
                    "confidence": f.get("confidence", 0.0),
                    "rule_version": f.get("rule_version", rule["rule_version"]),
                })
        # 2) per-memory heuristics
        for d in rows:
            mid = d["id"]
            mtype = d.get("memory_type") or "unknown"
            imp = float(d.get("importance_score") or 0.0)
            fr = float(d.get("freshness_score") or 0.0)
            acc = int(d.get("access_count") or 0)
            src = (d.get("source") or "").lower()
            protected = int(d.get("protected") or 0)
            # archive review (never for protected / preference)
            is_pref = mtype == "preference"
            if (not protected and not is_pref and imp <= rule["archive_importance_threshold"]
                    and fr <= rule["archive_freshness_threshold"]):
                candidates.append({
                    "kind": "archive_review",
                    "title": f"归档候选 {mid}",
                    "detail": f"importance={imp} freshness={fr}",
                    "source_memory_ids": [mid],
                    "confidence": 0.5,
                    "rule_version": rule["rule_version"],
                })
            # stale fact (low freshness, low access, not protected)
            if not protected and fr <= rule["archive_freshness_threshold"] and acc == 0:
                candidates.append({
                    "kind": "stale_fact",
                    "title": f"可能过时事实 {mid}",
                    "detail": f"freshness={fr}, access_count={acc}",
                    "source_memory_ids": [mid],
                    "confidence": 0.4,
                    "rule_version": rule["rule_version"],
                })
            # missing source
            if src in ("inferred", "guess", ""):
                candidates.append({
                    "kind": "missing_source",
                    "title": f"缺来源记忆 {mid}",
                    "detail": f"source='{src or 'empty'}'",
                    "source_memory_ids": [mid],
                    "confidence": 0.3,
                    "rule_version": rule["rule_version"],
                })
            # extract procedural from how-to content
            if mtype == "episodic" and re.search(r"\b(how to|step|流程|步骤|命令|command)\b", d.get("content", ""), re.I):
                candidates.append({
                    "kind": "extract_procedural",
                    "title": f"可从情景提取程序性经验 {mid}",
                    "detail": d.get("content", "")[:120],
                    "source_memory_ids": [mid],
                    "confidence": 0.45,
                    "rule_version": rule["rule_version"],
                })
        candidates = candidates[:limit]
        if persist:
            with db.transaction() as tconn:
                for c in candidates:
                    tconn.execute(
                        "INSERT INTO consolidation_candidates(kind,status,title,detail,source_memory_ids,"
                        "confidence,rule_version,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (c["kind"], "pending", c["title"], c["detail"],
                         json.dumps(c["source_memory_ids"], ensure_ascii=False),
                         c["confidence"], c["rule_version"], _now()))
                tconn.execute(
                    "INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
                    (None, "consolidation.candidates",
                     json.dumps({"count": len(candidates), "by": approver}, ensure_ascii=False)))
            return {"ok": True, "persisted": True, "candidate_count": len(candidates),
                    "candidates": candidates}
        return {"ok": True, "persisted": False, "candidate_count": len(candidates),
                "candidates": candidates}


def run_memory_consolidation(candidate_ids: Optional[list[int]] = None, dry_run: bool = False,
                             approver: str = "user", rule_version: str = "") -> dict:
    """Apply approved consolidation candidates.

    Each action is wrapped in its own transaction and recorded in
    governance_actions so it is auditable and reversible. Failure of one
    candidate never leaves a half-written transformation for the others.
    """
    if candidate_ids is not None and not isinstance(candidate_ids, (list, tuple)):
        return {"ok": False, "message": "candidate_ids must be a list of ints"}
    # Fetch pending candidates first (read-only connection).
    with db.connect() as rconn:
        sql = "SELECT * FROM consolidation_candidates WHERE status='pending'"
        args: list = []
        if candidate_ids:
            placeholders = ",".join("?" * len(candidate_ids))
            sql += f" AND id IN ({placeholders})"
            args.extend(int(x) for x in candidate_ids)
        sql += " ORDER BY id"
        cands = [dict(c) for c in rconn.execute(sql, args).fetchall()]
    if dry_run:
        return {"ok": True, "dry_run": True, "planned": len(cands), "candidates": cands}
    results = []
    for c in cands:
        # Each candidate is its own transaction: gov archive functions open their
        # own transaction, so we must reuse a single connection here rather than
        # nesting. The candidate-status UPDATE shares that transaction.
        try:
            with db.transaction() as conn:
                res = _apply_candidate(conn, c, approver, rule_version or c["rule_version"])
                if res.get("ok"):
                    conn.execute("UPDATE consolidation_candidates SET status='applied', resolved_at=?, "
                                 "resolved_by=?, notes_json=? WHERE id=?",
                                 (_now(), approver, json.dumps(res, ensure_ascii=False), c["id"]))
                else:
                    conn.execute("UPDATE consolidation_candidates SET status='skipped', resolved_at=?, "
                                 "resolved_by=?, notes_json=? WHERE id=?",
                                 (_now(), approver, json.dumps(res, ensure_ascii=False), c["id"]))
                results.append(res)
        except Exception as exc:  # one failure must not abort the batch
            try:
                with db.connect() as fconn:
                    fconn.execute("UPDATE consolidation_candidates SET status='skipped', resolved_at=?, "
                                  "resolved_by=?, notes_json=? WHERE id=?",
                                  (_now(), approver, json.dumps({"error": str(exc)}, ensure_ascii=False), c["id"]))
            except Exception:
                pass
            results.append({"ok": False, "candidate_id": c["id"], "error": str(exc)})
    return {"ok": True, "dry_run": False, "applied": len([r for r in results if r.get("ok")]),
            "skipped": len([r for r in results if not r.get("ok")]), "results": results}


def _apply_candidate(conn, cand: dict, approver: str, rule_version: str) -> dict:
    kind = cand["kind"]
    ids = _json_loads(cand["source_memory_ids"], [])
    if kind == "archive_review" and ids:
        mid = ids[0]
        before, err = gov._archive_memory(conn, mid, f"consolidation:{kind}",
                                          gov.RULE_VERSION, approver)
        if err:
            return {"ok": False, "candidate_id": cand["id"], "error": err}
        return {"ok": True, "candidate_id": cand["id"], "action": "archive", "memory_id": mid}
    if kind == "merge_duplicate" and len(ids) >= 2:
        # Keep the higher-importance one; archive the loser (reuse governance archive).
        rows = [dict(r) for r in conn.execute(
            "SELECT id, importance_score, access_count, updated_at FROM memories WHERE id IN (?,?)",
            (ids[0], ids[1])).fetchall()]
        if len(rows) < 2:
            return {"ok": False, "candidate_id": cand["id"], "error": "source memories missing"}
        loser = min(rows, key=lambda r: (float(r["importance_score"] or 0), float(r["access_count"] or 0)))
        before, err = gov._archive_memory(conn, loser["id"], f"consolidation:{kind}",
                                          gov.RULE_VERSION, approver)
        if err:
            return {"ok": False, "candidate_id": cand["id"], "error": err}
        return {"ok": True, "candidate_id": cand["id"], "action": "archive_duplicate",
                "archived_memory_id": loser["id"]}
    if kind in ("extract_procedural", "extract_semantic") and ids:
        # Create a NEW derived memory, linked via derived_from; never overwrite source.
        src = conn.execute("SELECT * FROM memories WHERE id=?", (ids[0],)).fetchone()
        if not src:
            return {"ok": False, "candidate_id": cand["id"], "error": "source memory missing"}
        s = dict(src)
        new_type = "procedural" if kind == "extract_procedural" else "semantic"
        content = f"[derived from #{s['id']}] {s['content']}"[:4000]
        cur = conn.execute(
            "INSERT INTO memories(content,category,source,confidence,status,memory_status,memory_type) "
            "VALUES(?,?,?,?,?,?,?)",
            (content, s.get("category", "general"), "consolidation-derived",
             float(s.get("confidence") or 0.7), "active", "active", new_type))
        new_id = cur.lastrowid
        conn.execute(
            "INSERT INTO memory_links(source_memory_id,target_memory_id,relation,note,created_at) "
            "VALUES(?,?,?,?,?)",
            (new_id, ids[0], "derived_from", f"consolidation:{kind}", _now()))
        conn.execute(
            "INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
            (None, "consolidation.applied",
             json.dumps({"kind": kind, "source": ids[0], "derived": new_id, "by": approver}, ensure_ascii=False)))
        return {"ok": True, "candidate_id": cand["id"], "action": "create_derived",
                "new_memory_id": new_id, "derived_from": ids[0]}
    # conservative default: do not auto-apply review-only candidates (conflict_review,
    # stale_fact, missing_source) without explicit human handling.
    return {"ok": False, "candidate_id": cand["id"],
            "error": f"kind '{kind}' requires explicit human review; not auto-applied"}


def list_consolidation_candidates(status: str = "", limit: int = 50) -> dict:
    with db.connect() as conn:
        sql = "SELECT * FROM consolidation_candidates"
        args: list = []
        if status.strip():
            sql += " WHERE status=?"
            args.append(status.strip())
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
        return {"ok": True, "count": len(rows), "candidates": [dict(r) for r in rows]}
