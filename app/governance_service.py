"""Persistence layer for Phase-5 memory governance.

Everything that touches the database for governance lives here; the scoring
engine stays pure. All multi-table single operations run inside `transaction()`
so a partial failure leaves nothing behind.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app import db
from app.governance_rules import load_rules, RULE_VERSION, GOV_STATUS_ACTIVE, GOV_STATUS_ARCHIVED, \
    GOV_STATUS_SUPERSEDED, GOV_STATUS_PENDING_REVIEW
from app.governance_scoring import score_memory, score_freshness, archive_candidate, clamp
from app.governance_conflicts import classify_pair, lexical_similarity, needs_review

try:  # embeddings are optional; lexical fallback keeps detection usable
    from app import embeddings as emb
except Exception:  # pragma: no cover
    emb = None

APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"

EXEC_PENDING = "pending"
EXEC_SUCCESS = "success"
EXEC_FAILED = "failed"
EXEC_SKIPPED = "skipped"

ACTION_SCORE = "score"
ACTION_ARCHIVE = "archive"
ACTION_RESTORE = "restore"
ACTION_PROTECT = "protect"
ACTION_UNPROTECT = "unprotect"
ACTION_MARK_REVIEW = "mark_review"
ACTION_SUPERSEDE = "supersede"
ACTION_UPDATE_VECTOR = "update_vector"

# Actions that change memory content/status and therefore require approval.
DESTRUCTIVE_ACTIONS = {ACTION_ARCHIVE, ACTION_SUPERSEDE, ACTION_RESTORE,
                       ACTION_PROTECT, ACTION_UNPROTECT, ACTION_MARK_REVIEW}

REVERSIBLE_ACTIONS = {ACTION_ARCHIVE, ACTION_RESTORE, ACTION_PROTECT,
                      ACTION_UNPROTECT, ACTION_MARK_REVIEW, ACTION_SCORE}


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _rows(sql: str, args=()) -> list[dict]:
    with db.connect() as conn:
        return [db.rowdict(r) for r in conn.execute(sql, args).fetchall()]


def _one(sql: str, args=()):
    with db.connect() as conn:
        return db.rowdict(conn.execute(sql, args).fetchone())


def _event(conn, event_type: str, detail: str, task_id=None):
    conn.execute(
        "INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
        (task_id, event_type, detail[:2000]),
    )


# --------------------------------------------------------------------------
# Memory access helpers
# --------------------------------------------------------------------------

def get_memory_row(memory_id: int, conn=None) -> dict | None:
    sql = "SELECT * FROM memories WHERE id=?"
    if conn is not None:
        return db.rowdict(conn.execute(sql, (int(memory_id),)).fetchone())
    return _one(sql, (int(memory_id),))


def touch_access(memory_ids: list[int]) -> None:
    """Record that memories were retrieved (drives the frequency component)."""
    if not memory_ids:
        return
    now = _now_str()
    with db.transaction() as conn:
        for mid in memory_ids:
            conn.execute(
                "UPDATE memories SET access_count = COALESCE(access_count,0) + 1, "
                "last_accessed_at = ? WHERE id = ?",
                (now, int(mid)),
            )


def build_scoring_context(conn, memory_id: int) -> dict:
    """Collect real signals for scoring: links, updates, retrieval history."""
    outgoing = conn.execute(
        "SELECT relation, COUNT(*) AS n FROM memory_links WHERE source_memory_id=? GROUP BY relation",
        (int(memory_id),),
    ).fetchall()
    incoming = conn.execute(
        "SELECT relation, COUNT(*) AS n FROM memory_links WHERE target_memory_id=? GROUP BY relation",
        (int(memory_id),),
    ).fetchall()
    link_count = sum(r["n"] for r in outgoing) + sum(r["n"] for r in incoming)
    relations_out = {r["relation"] for r in outgoing}
    relations_in = {r["relation"] for r in incoming}
    try:
        update_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='memory.updated' AND detail LIKE ?",
            (f"memory_id={int(memory_id)}%",),
        ).fetchone()[0]
    except Exception:
        update_count = 0
    return {
        "link_count": link_count,
        "is_superseded": "supersedes" in relations_in,
        "supersedes_older": "supersedes" in relations_out,
        "update_count": update_count,
        "tags": _safe_tags(conn, memory_id),
    }


def _safe_tags(conn, memory_id: int) -> list[str]:
    row = conn.execute("SELECT tags_json FROM memories WHERE id=?", (int(memory_id),)).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["tags_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return []


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def score_memories(memory_ids: list[int] | None = None, limit: int = 50,
                   category: str = "", persist: bool = True) -> dict:
    """Score memories. `persist=False` computes without writing (dry-run)."""
    rules = load_rules()
    limit = max(1, min(int(limit), 500))
    sql = "SELECT * FROM memories WHERE status='active'"
    args: list = []
    if category.strip():
        sql += " AND category=?"
        args.append(category.strip()[:100])
    if memory_ids:
        ids = [int(i) for i in memory_ids][:200]
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        args.extend(ids)
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    args.append(limit)

    results = []
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        rows = [db.rowdict(r) for r in conn.execute(sql, args).fetchall()]
        for row in rows:
            ctx = build_scoring_context(conn, row["id"])
            scored = score_memory(row, ctx, rules, now=now)
            results.append(scored)
        if persist:
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            for row, scored in zip(rows, results):
                conn.execute(
                    "UPDATE memories SET importance_score=?, confidence_score=?, freshness_score=?, "
                    "last_evaluated_at=? WHERE id=?",
                    (scored["importance"], scored["confidence_score"],
                     scored["freshness"], now_str, row["id"]),
                )
            _event(conn, "governance.scored",
                   f"count={len(rows)}; rule_version={rules['rule_version']}; persisted=1")
    return {
        "ok": True,
        "rule_version": rules["rule_version"],
        "scored": len(results),
        "persisted": bool(persist),
        "results": results,
    }


def get_governance_status(memory_id: int) -> dict:
    """Governance view of one memory: scores, protection, criteria, audit trail."""
    rules = load_rules()
    row = _one("SELECT * FROM memories WHERE id=?", (int(memory_id),))
    if not row:
        return {"ok": False, "message": "Memory not found", "memory_id": int(memory_id)}
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        ctx = build_scoring_context(conn, row["id"])
        links = [db.rowdict(r) for r in conn.execute(
            """SELECT l.id,l.relation,l.note,l.created_at,
                      CASE WHEN l.source_memory_id=? THEN 'outgoing' ELSE 'incoming' END AS direction,
                      CASE WHEN l.source_memory_id=? THEN l.target_memory_id ELSE l.source_memory_id END AS other_id
               FROM memory_links l WHERE l.source_memory_id=? OR l.target_memory_id=?
               ORDER BY l.id DESC LIMIT 50""",
            (row["id"], row["id"], row["id"], row["id"]),
        ).fetchall()]
        conflicts = [db.rowdict(r) for r in conn.execute(
            "SELECT * FROM memory_conflicts WHERE memory_id=? OR candidate_memory_id=? "
            "ORDER BY id DESC LIMIT 50", (row["id"], row["id"]),
        ).fetchall()]
        actions = [db.rowdict(r) for r in conn.execute(
            "SELECT * FROM governance_actions WHERE memory_id=? ORDER BY id DESC LIMIT 50",
            (row["id"],),
        ).fetchall()]
        has_vector = conn.execute(
            "SELECT 1 FROM memory_embeddings WHERE memory_id=?", (row["id"],)
        ).fetchone() is not None

    scored = score_memory(row, ctx, rules, now=now)
    is_candidate, candidate_reasons = archive_candidate(row, scored, rules)
    _f, fresh_reasons = score_freshness(row, ctx, rules, now=now)

    return {
        "ok": True,
        "memory_id": row["id"],
        "content_preview": (row.get("content") or "")[:200],
        "storage_status": row.get("status"),
        "memory_status": row.get("memory_status"),
        "category": row.get("category"),
        "source": row.get("source"),
        "protected": bool(row.get("protected")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "access_count": row.get("access_count") or 0,
        "last_accessed_at": row.get("last_accessed_at"),
        "last_evaluated_at": row.get("last_evaluated_at"),
        "stored_scores": {
            "importance_score": row.get("importance_score"),
            "confidence_score": row.get("confidence_score"),
            "freshness_score": row.get("freshness_score"),
        },
        "computed_scores": {
            "relevance": scored["relevance"],
            "frequency": scored["frequency"],
            "credibility": scored["credibility"],
            "stability": scored["stability"],
            "freshness": scored["freshness"],
            "importance": scored["importance"],
        },
        "weights": scored["weights"],
        "reasons": scored["reasons"],
        "archive_policy": {
            "is_archive_candidate": is_candidate,
            "reasons": candidate_reasons,
            "auto_archive_enabled": bool(rules.get("auto_archive_enabled")),
            "physical_delete_enabled": False,
            "importance_threshold": rules.get("archive_importance_threshold"),
            "freshness_threshold": rules.get("archive_freshness_threshold"),
            "observation_period_days": rules.get("observation_period_days"),
        },
        "vector_indexed": has_vector,
        "links": links,
        "conflicts": conflicts,
        "recent_actions": actions,
        "rule_version": rules["rule_version"],
    }


# --------------------------------------------------------------------------
# Duplicate / conflict detection
# --------------------------------------------------------------------------

RESOLUTION_PENDING = "pending"
RESOLUTION_DUPLICATE = "duplicate_confirmed"
RESOLUTION_NOT_CONFLICT = "not_conflict"
RESOLUTION_SUPERSEDED = "superseded"
RESOLUTION_KEPT_BOTH = "kept_both"


def _active_rows(conn, category: str = "") -> list[dict]:
    sql = "SELECT * FROM memories WHERE status='active'"
    args: list = []
    if category.strip():
        sql += " AND category=?"
        args.append(category.strip()[:100])
    sql += " ORDER BY id"
    return [db.rowdict(r) for r in conn.execute(sql, args).fetchall()]


def _similarity_matrix(conn, rows: list[dict], rules: dict) -> dict[tuple[int, int], float]:
    """Similarity between every active pair.

    Uses stored vectors when available (the real semantic model), otherwise a
    lexical fallback. A pair whose similarity is below `similarity_min` is
    dropped so the matrix stays small.
    """
    sims: dict[tuple[int, int], float] = {}
    min_sim = float(rules.get("similarity_min", 0.60))
    vectors: dict[int, list[float]] = {}
    if emb is not None:
        for row in rows:
            rec = conn.execute(
                "SELECT vector_json, content_hash FROM memory_embeddings WHERE memory_id=?",
                (row["id"],),
            ).fetchone()
            if not rec:
                continue
            if rec["content_hash"] != emb.content_hash(row["content"] or ""):
                continue  # stale vector -> fall back to lexical for this row
            try:
                vectors[row["id"]] = json.loads(rec["vector_json"])
            except (TypeError, json.JSONDecodeError):
                continue
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            va, vb = vectors.get(a["id"]), vectors.get(b["id"])
            if va is not None and vb is not None and emb is not None:
                sim = emb.cosine_similarity(va, vb)
            else:
                sim = lexical_similarity(a["content"] or "", b["content"] or "")
            if sim >= min_sim:
                sims[(a["id"], b["id"])] = sim
    return sims


def detect_conflicts(memory_ids: list[int] | None = None, category: str = "",
                     candidate_count: int | None = None, persist: bool = True) -> dict:
    """Find duplicates / conflicts among active memories.

    `persist=False` computes without writing anything (dry-run).
    """
    rules = load_rules()
    if candidate_count is not None:
        rules["candidate_count"] = max(
            5, min(int(candidate_count), 10))
    top_n = int(rules.get("candidate_count", 8))

    with db.connect() as conn:
        rows = _active_rows(conn, category)
        if memory_ids:
            wanted = {int(i) for i in memory_ids}
            rows = [r for r in rows if r["id"] in wanted]
        sims = _similarity_matrix(conn, rows, rules)

    by_id = {r["id"]: r for r in rows}
    # Keep only the top-N most similar candidates per memory (5-10, configurable).
    neighbours: dict[int, list[tuple[int, float]]] = {}
    for (a, b), sim in sims.items():
        neighbours.setdefault(a, []).append((b, sim))
        neighbours.setdefault(b, []).append((a, sim))
    for k in neighbours:
        neighbours[k].sort(key=lambda x: x[1], reverse=True)
        neighbours[k] = neighbours[k][:top_n]

    findings = []
    seen: set[tuple[int, int]] = set()
    for a_id, cands in sorted(neighbours.items()):
        for b_id, sim in cands:
            key = (min(a_id, b_id), max(a_id, b_id))
            if key in seen:
                continue
            seen.add(key)
            # Older (lower id) is the incumbent; the newer one is the candidate.
            old_id, new_id = sorted(key)
            verdict = classify_pair(by_id[old_id], by_id[new_id], sim, rules)
            if verdict is None:
                continue
            findings.append({
                "memory_id": old_id,
                "candidate_memory_id": new_id,
                "content_preview": (by_id[old_id].get("content") or "")[:120],
                "candidate_preview": (by_id[new_id].get("content") or "")[:120],
                **verdict,
                "needs_review": needs_review(verdict),
            })

    written = 0
    if persist and findings:
        written = _persist_conflicts(findings, None, rules)

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["conflict_type"]] = counts.get(f["conflict_type"], 0) + 1

    return {
        "ok": True,
        "rule_version": rules["rule_version"],
        "scanned": len(rows),
        "pairs_compared": len(sims),
        "candidate_count": top_n,
        "persisted": bool(persist),
        "written": written,
        "counts_by_type": counts,
        "needs_review": sum(1 for f in findings if f["needs_review"]),
        "findings": findings,
    }


def _persist_conflicts(findings: list[dict], run_id: int | None, rules: dict) -> int:
    """Insert/refresh conflict rows. Idempotent via the UNIQUE constraint."""
    written = 0
    with db.transaction() as conn:
        for f in findings:
            conn.execute(
                """INSERT INTO memory_conflicts
                   (run_id,memory_id,candidate_memory_id,conflict_type,similarity,
                    confidence,reason,recommendation,resolution,rule_version)
                   VALUES(?,?,?,?,?,?,?,?, 'pending', ?)
                   ON CONFLICT(memory_id,candidate_memory_id,conflict_type) DO UPDATE SET
                     similarity=excluded.similarity,
                     confidence=excluded.confidence,
                     reason=excluded.reason,
                     recommendation=excluded.recommendation,
                     run_id=excluded.run_id,
                     rule_version=excluded.rule_version,
                     created_at=CURRENT_TIMESTAMP
                   WHERE memory_conflicts.resolution='pending'""",
                (run_id, f["memory_id"], f["candidate_memory_id"], f["conflict_type"],
                 f["similarity"], f["confidence"], f["reason"],
                 f["recommendation"], rules["rule_version"]),
            )
            written += 1
        _event(conn, "governance.conflicts_detected",
               f"count={len(findings)}; rule_version={rules['rule_version']}")
    return written


def list_conflicts(memory_id: int | None = None, resolution: str = "",
                   limit: int = 50) -> dict:
    limit = max(1, min(int(limit), 200))
    sql = "SELECT * FROM memory_conflicts"
    where, args = [], []
    if memory_id is not None:
        where.append("(memory_id=? OR candidate_memory_id=?)")
        args.extend([int(memory_id), int(memory_id)])
    if resolution.strip():
        where.append("resolution=?")
        args.append(resolution.strip()[:40])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = _rows(sql, args)
    return {"ok": True, "count": len(rows), "conflicts": rows}


def review_conflict(conflict_id: int, resolution: str, note: str = "",
                    resolved_by: str = "user") -> dict:
    """Human adjudication of one conflict.

    Allowed resolutions are fixed server-side; `duplicate_confirmed` archives the
    loser (reversibly, never deleted) and `superseded` records a new version while
    keeping the old one. Nothing here merges content silently.
    """
    allowed = {RESOLUTION_DUPLICATE, RESOLUTION_NOT_CONFLICT,
               RESOLUTION_SUPERSEDED, RESOLUTION_KEPT_BOTH, RESOLUTION_PENDING}
    resolution = (resolution or "").strip()
    if resolution not in allowed:
        return {"ok": False, "message": f"未知的裁决结果：{resolution}",
                "allowed": sorted(allowed)}

    row = _one("SELECT * FROM memory_conflicts WHERE id=?", (int(conflict_id),))
    if not row:
        return {"ok": False, "message": "Conflict not found", "conflict_id": int(conflict_id)}

    result: dict = {"ok": True, "conflict_id": int(conflict_id),
                    "resolution": resolution, "applied": False}
    now = _now_str()

    try:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE memory_conflicts SET resolution=?, resolved_by=?, resolved_at=?, "
                "reason=CASE WHEN ? THEN reason || ' | 裁决：' || ? ELSE reason END "
                "WHERE id=?",
                (resolution, resolved_by[:100], now, bool(note.strip()), note[:500],
                 int(conflict_id)),
            )
            _event(conn, "governance.conflict_reviewed",
                   f"conflict_id={conflict_id}; resolution={resolution}; by={resolved_by[:50]}")

            if resolution == RESOLUTION_DUPLICATE:
                loser = _pick_duplicate_loser(conn, row["memory_id"], row["candidate_memory_id"])
                if loser is None:
                    result["message"] = "两条记忆均受保护或不存在，未执行归档"
                else:
                    before = db.rowdict(conn.execute(
                        "SELECT id,status,memory_status,importance_score FROM memories WHERE id=?",
                        (loser,)).fetchone())
                    if before and before.get("protected"):
                        result["message"] = f"记忆 {loser} 受保护，跳过归档"
                    else:
                        conn.execute(
                            "UPDATE memories SET status='archived', memory_status='archived', "
                            "updated_at=CURRENT_TIMESTAMP WHERE id=?", (loser,))
                        conn.execute(
                            """INSERT INTO governance_actions
                               (action_type,memory_id,target_memory_id,old_value_json,new_value_json,
                                reason,rule_version,approval_status,execution_status,executed_at,
                                rollback_info_json)
                               VALUES('archive',?,?,?,?,?,?, 'approved','success',?,?)""",
                            (loser, row["memory_id"], _json(before),
                             _json({"status": "archived", "memory_status": "archived"}),
                             f"人工裁决为完全重复（conflict_id={conflict_id}）",
                             row.get("rule_version") or "", now,
                             _json({"reverse_action": "restore", "previous": before})),
                        )
                        _event(conn, "governance.archived",
                               f"memory_id={loser}; reason=duplicate_confirmed; "
                               f"conflict_id={conflict_id}")
                        result["applied"] = True
                        result["archived_memory_id"] = loser
            elif resolution == RESOLUTION_NOT_CONFLICT or resolution == RESOLUTION_KEPT_BOTH:
                result["message"] = "判定为不冲突/共存，两条记忆均保留"
    except Exception as exc:
        return {"ok": False, "message": f"裁决执行失败，已回滚：{exc}",
                "conflict_id": int(conflict_id)}
    return result


def _pick_duplicate_loser(conn, id_a: int, id_b: int) -> int | None:
    """Pick which duplicate to archive: the lower-scored / older one.

    A protected memory is never picked; if both are protected, None is returned.
    """
    rows = {r["id"]: db.rowdict(r) for r in conn.execute(
        "SELECT id, protected, importance_score, access_count, updated_at FROM memories "
        "WHERE id IN (?,?)", (int(id_a), int(id_b))).fetchall()}
    a, b = rows.get(int(id_a)), rows.get(int(id_b))
    if not a or not b:
        return None
    if a.get("protected") and b.get("protected"):
        return None
    if a.get("protected"):
        return int(id_b)
    if b.get("protected"):
        return int(id_a)

    def rank(r: dict) -> tuple:
        return (float(r.get("importance_score") or 0.0),
                int(r.get("access_count") or 0),
                str(r.get("updated_at") or ""))

    return int(id_a) if rank(a) <= rank(b) else int(id_b)


def supersede_from_conflict(conflict_id: int, new_content: str = "",
                            source: str = "user-confirmed", confidence: float = 0.95,
                            note: str = "") -> dict:
    """Apply an explicit correction: create the new version, keep the old one.

    Reuses the existing `supersedes_id` + `memory_links(supersedes)` semantics so
    Phase 1-4 behaviour (search with include_archived, link queries) is unchanged.
    """
    row = _one("SELECT * FROM memory_conflicts WHERE id=?", (int(conflict_id),))
    if not row:
        return {"ok": False, "message": "Conflict not found", "conflict_id": int(conflict_id)}
    old_id = int(row["memory_id"])
    candidate_id = int(row["candidate_memory_id"])
    content = (new_content or "").strip()
    if not content:
        cand = _one("SELECT content FROM memories WHERE id=?", (candidate_id,))
        content = (cand or {}).get("content") or ""
    if not content.strip():
        return {"ok": False, "message": "没有可用于替代的内容"}
    confidence = clamp(float(confidence), 0.0, 1.0)

    now = _now_str()
    try:
        with db.transaction() as conn:
            old = db.rowdict(conn.execute(
                "SELECT * FROM memories WHERE id=?", (old_id,)).fetchone())
            if not old:
                return {"ok": False, "message": "Old memory not found", "memory_id": old_id}
            if old.get("protected"):
                return {"ok": False, "message": f"记忆 {old_id} 受保护，不能替代",
                        "memory_id": old_id}
            cur = conn.execute(
                """INSERT INTO memories(content,category,source,confidence,status,tags_json,
                                        supersedes_id,memory_status)
                   VALUES(?,?,?,?, 'active', ?, ?, 'active')""",
                (content[:10000], old.get("category") or "", source[:200], confidence,
                 old.get("tags_json") or "[]", old_id),
            )
            new_id = cur.lastrowid
            conn.execute(
                "UPDATE memories SET status='archived', memory_status='superseded', "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?", (old_id,))
            conn.execute(
                "INSERT INTO memory_links(source_memory_id,target_memory_id,relation,note) "
                "VALUES(?,?, 'supersedes', ?)",
                (new_id, old_id, note[:1000]),
            )
            conn.execute(
                "UPDATE memory_conflicts SET resolution='superseded', resolved_by='governance', "
                "resolved_at=? WHERE id=?", (now, int(conflict_id)))
            conn.execute(
                """INSERT INTO governance_actions
                   (action_type,memory_id,target_memory_id,old_value_json,new_value_json,
                    reason,rule_version,approval_status,execution_status,executed_at,
                    rollback_info_json)
                   VALUES('supersede',?,?,?,?,?,?, 'approved','success',?,?)""",
                (old_id, new_id,
                 _json({"id": old_id, "status": old.get("status"),
                        "memory_status": old.get("memory_status")}),
                 _json({"new_id": new_id, "status": "active"}),
                 f"明确纠正（conflict_id={conflict_id}）",
                 row.get("rule_version") or "", now,
                 _json({"reverse_action": "restore", "archived_id": old_id,
                        "created_id": new_id})),
            )
            _event(conn, "memory.superseded",
                   f"old_id={old_id}; new_id={new_id}; source=conflict:{conflict_id}")
    except Exception as exc:
        return {"ok": False, "message": f"纠正执行失败，已回滚：{exc}",
                "conflict_id": int(conflict_id)}
    return {"ok": True, "conflict_id": int(conflict_id), "old_memory_id": old_id,
            "new_memory_id": new_id, "archived_old": True,
            "message": "已创建新版本并归档旧版本，历史与 supersedes 关系保留"}


def set_protected(memory_id: int, protected: bool, reason: str = "") -> dict:
    """Toggle protection. Server-side operation; never trusts caller privileges."""
    row = _one("SELECT * FROM memories WHERE id=?", (int(memory_id),))
    if not row:
        return {"ok": False, "message": "Memory not found", "memory_id": int(memory_id)}
    old = bool(row.get("protected"))
    new = bool(protected)
    if old == new:
        return {"ok": True, "memory_id": int(memory_id), "protected": new,
                "changed": False, "message": "已是目标状态，无需修改"}
    action_type = ACTION_PROTECT if new else ACTION_UNPROTECT
    rules = load_rules()
    try:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE memories SET protected=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (1 if new else 0, int(memory_id)),
            )
            record_action(conn, action_type, int(memory_id),
                          old_value={"protected": 1 if old else 0},
                          new_value={"protected": 1 if new else 0},
                          reason=reason or ("保护记忆" if new else "解除保护"),
                          rule_version=rules["rule_version"],
                          rollback_info={"previous_protected": 1 if old else 0})
            _event(conn, "governance.protected" if new else "governance.unprotected",
                   f"memory_id={memory_id}; reason={reason[:300]}")
    except Exception as exc:
        return {"ok": False, "message": f"保护状态更新失败：{exc}", "memory_id": int(memory_id)}
    return {"ok": True, "memory_id": int(memory_id), "protected": new, "changed": True}


# --------------------------------------------------------------------------
# Governance archive / restore / rollback (Phase 5.3)
# --------------------------------------------------------------------------

def record_action(conn, action_type: str, memory_id: int, old_value: dict,
                  new_value: dict, reason: str, rule_version: str,
                  target_memory_id: int | None = None, rollback_info: dict | None = None,
                  approval_status: str = APPROVAL_APPROVED,
                  execution_status: str = EXEC_SUCCESS, executed_at: str | None = None) -> int:
    """Insert one governance action row. Returns the new row id."""
    conn.execute(
        """INSERT INTO governance_actions
           (action_type,memory_id,target_memory_id,old_value_json,new_value_json,
            reason,rule_version,approval_status,execution_status,executed_at,rollback_info_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (action_type, int(memory_id), target_memory_id, _json(old_value),
         _json(new_value), reason[:500], rule_version, approval_status,
         execution_status, executed_at or _now_str(), _json(rollback_info or {})),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _archive_memory(conn, memory_id: int, reason: str, rule_version: str,
                    approver: str) -> tuple[dict | None, str | None]:
    """Internal: soft-archive a memory inside an open transaction.

    Refuses non-active or protected memories. Returns (before_dict, error) so the
    caller can roll the whole unit back by raising.
    """
    before = db.rowdict(conn.execute(
        "SELECT id,status,memory_status,protected FROM memories WHERE id=?",
        (int(memory_id),)).fetchone())
    if not before:
        return None, "Memory not found"
    if before.get("status") != "active":
        return None, "记忆不是活跃状态，无法归档"
    if before.get("protected"):
        return None, "记忆受保护，不能归档（请先解除保护）"
    conn.execute(
        "UPDATE memories SET status='archived', memory_status='archived', "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(memory_id),))
    record_action(conn, ACTION_ARCHIVE, int(memory_id),
                  old_value={"status": before["status"],
                             "memory_status": before.get("memory_status")},
                  new_value={"status": "archived", "memory_status": "archived"},
                  reason=reason or "治理归档", rule_version=rule_version,
                  rollback_info={"reverse_action": "restore",
                                 "previous_status": before["status"],
                                 "previous_memory_status": before.get("memory_status")})
    _event(conn, "governance.archived",
           f"memory_id={memory_id}; reason={reason[:200]}; approver={approver[:50]}")
    return before, None


def archive_memory_governance(memory_id: int, reason: str = "",
                             approver: str = "user") -> dict:
    """Controlled, audited, reversible archive. Never archives protected memories."""
    row = _one("SELECT * FROM memories WHERE id=?", (int(memory_id),))
    if not row:
        return {"ok": False, "message": "Memory not found", "memory_id": int(memory_id)}
    rules = load_rules()
    try:
        with db.transaction() as conn:
            before, err = _archive_memory(conn, memory_id, reason,
                                          rules["rule_version"], approver)
            if err:
                raise RuntimeError(err)
        return {"ok": True, "memory_id": int(memory_id), "status": "archived",
                "protected": bool(before.get("protected"))}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "memory_id": int(memory_id)}


def _restore_memory(conn, memory_id: int, reason: str, rule_version: str,
                    approver: str) -> tuple[dict | None, str | None]:
    """Internal: restore an archived memory inside an open transaction."""
    before = db.rowdict(conn.execute(
        "SELECT id,status,memory_status,protected FROM memories WHERE id=?",
        (int(memory_id),)).fetchone())
    if not before:
        return None, "Memory not found"
    if before.get("status") != "archived":
        return None, "记忆不是归档状态，无法恢复"
    conn.execute(
        "UPDATE memories SET status='active', memory_status='active', "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(memory_id),))
    record_action(conn, ACTION_RESTORE, int(memory_id),
                  old_value={"status": before["status"],
                             "memory_status": before.get("memory_status")},
                  new_value={"status": "active", "memory_status": "active"},
                  reason=reason or "治理恢复", rule_version=rule_version,
                  rollback_info={"reverse_action": "archive",
                                 "previous_status": before["status"],
                                 "previous_memory_status": before.get("memory_status")})
    _event(conn, "governance.restored",
           f"memory_id={memory_id}; reason={reason[:200]}; approver={approver[:50]}")
    return before, None


def restore_memory_governance(memory_id: int, reason: str = "",
                             approver: str = "user") -> dict:
    """Controlled, audited, reversible restore of an archived memory."""
    row = _one("SELECT * FROM memories WHERE id=?", (int(memory_id),))
    if not row:
        return {"ok": False, "message": "Memory not found", "memory_id": int(memory_id)}
    rules = load_rules()
    try:
        with db.transaction() as conn:
            before, err = _restore_memory(conn, memory_id, reason,
                                          rules["rule_version"], approver)
            if err:
                raise RuntimeError(err)
        return {"ok": True, "memory_id": int(memory_id), "status": "active"}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "memory_id": int(memory_id)}


def get_governance_action(action_id: int) -> dict:
    row = _one("SELECT * FROM governance_actions WHERE id=?", (int(action_id),))
    if not row:
        return {"ok": False, "message": "Action not found", "action_id": int(action_id)}
    row["ok"] = True
    return row


def rollback_governance_action(action_id: int, reason: str = "") -> dict:
    """Reverse a previously executed governance action.

    Reversible types: archive, restore, protect, unprotect, mark_review, supersede.
    Irreversible outcomes are reported, never silently faked. Idempotent: rolling
    back an already-rolled-back action returns ok=True, changed=False.
    """
    row = _one("SELECT * FROM governance_actions WHERE id=?", (int(action_id),))
    if not row:
        return {"ok": False, "message": "Action not found", "action_id": int(action_id)}
    if row.get("execution_status") != EXEC_SUCCESS:
        return {"ok": False, "message": "该动作未成功执行，无需回滚",
                "action_id": int(action_id), "execution_status": row.get("execution_status")}
    if row.get("rolled_back_at"):
        return {"ok": True, "action_id": int(action_id), "changed": False,
                "message": "该动作已回滚，无需重复操作"}
    action_type = row.get("action_type")
    try:
        info = json.loads(row.get("rollback_info_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        info = {}
    now = _now_str()
    try:
        with db.transaction() as conn:
            if action_type == ACTION_ARCHIVE:
                conn.execute(
                    "UPDATE memories SET status=?, memory_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (info.get("previous_status", "active"),
                     info.get("previous_memory_status", "active"),
                     int(row["memory_id"])))
            elif action_type == ACTION_RESTORE:
                conn.execute(
                    "UPDATE memories SET status=?, memory_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (info.get("previous_status", "archived"),
                     info.get("previous_memory_status", "archived"),
                     int(row["memory_id"])))
            elif action_type == ACTION_PROTECT:
                prev = info.get("previous_protected", 0)
                conn.execute(
                    "UPDATE memories SET protected=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (1 if prev else 0, int(row["memory_id"])))
            elif action_type == ACTION_UNPROTECT:
                prev = info.get("previous_protected", 1)
                conn.execute(
                    "UPDATE memories SET protected=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (1 if prev else 0, int(row["memory_id"])))
            elif action_type == ACTION_MARK_REVIEW:
                # 6.1: restore the EXACT pre-mark state, never assume 'active'.
                # Older actions may lack the field -> backward-compatible default.
                prev_ms = info.get("previous_memory_status")
                if prev_ms is None:
                    prev_ms = "active"
                conn.execute(
                    "UPDATE memories SET memory_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (prev_ms, int(row["memory_id"])))
            elif action_type == ACTION_SUPERSEDE:
                # 6.2: full, idempotent rollback of an explicit correction.
                old_id = int(row["memory_id"])
                new_id = row.get("target_memory_id")
                # Restore the OLD memory to its exact pre-supersede state.
                try:
                    prev = json.loads(row.get("old_value_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    prev = {}
                prev_status = prev.get("status", "active")
                prev_mstatus = prev.get("memory_status", "active")
                conn.execute(
                    "UPDATE memories SET status=?, memory_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (prev_status, prev_mstatus, old_id))
                if new_id:
                    new = db.rowdict(conn.execute(
                        "SELECT id,protected FROM memories WHERE id=?",
                        (int(new_id),)).fetchone())
                    # Reverse the new version (unless it became protected).
                    if new and not new.get("protected"):
                        conn.execute(
                            "UPDATE memories SET status='archived', memory_status='archived', "
                            "updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(new_id),))
                    # 6.2: remove the supersedes relation this action created.
                    conn.execute(
                        "DELETE FROM memory_links WHERE source_memory_id=? AND target_memory_id=? "
                        "AND relation='supersedes'",
                        (int(new_id), old_id))
                    # 6.2: reset the conflict adjudication so it can be re-adjudicated.
                    # resolved_by is NOT NULL in the schema, so reset to '' (not NULL).
                    conn.execute(
                        "UPDATE memory_conflicts SET resolution='pending', resolved_by='', "
                        "resolved_at=NULL WHERE ((memory_id=? AND candidate_memory_id=?) OR "
                        "(memory_id=? AND candidate_memory_id=?)) AND resolution='superseded'",
                        (old_id, new_id, new_id, old_id))
            elif action_type == ACTION_SCORE:
                pass  # scores are recomputed on demand; nothing physically to reverse
            else:
                return {"ok": False, "message": f"动作类型 {action_type} 不支持回滚",
                        "action_id": int(action_id)}
            conn.execute(
                "UPDATE governance_actions SET rolled_back_at=? WHERE id=?",
                (now, int(action_id)))
            _event(conn, "governance.action_rolled_back",
                   f"action_id={action_id}; type={action_type}; reason={reason[:200]}")
    except Exception as exc:
        return {"ok": False, "message": f"回滚失败，已回滚事务：{exc}",
                "action_id": int(action_id)}
    return {"ok": True, "action_id": int(action_id), "type": action_type,
            "changed": True, "message": "已回滚该治理动作"}


def _mark_review(conn, memory_id: int, reason: str, rule_version: str,
                 approver: str) -> tuple[dict | None, str | None]:
    """Internal: set a memory to pending_review inside an open transaction."""
    before = db.rowdict(conn.execute(
        "SELECT id,status,memory_status,protected FROM memories WHERE id=?",
        (int(memory_id),)).fetchone())
    if not before:
        return None, "Memory not found"
    if before.get("status") != "active":
        return None, "记忆不是活跃状态，无法标记为待审核"
    conn.execute(
        "UPDATE memories SET memory_status='pending_review', updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (int(memory_id),))
    record_action(conn, ACTION_MARK_REVIEW, int(memory_id),
                  old_value={"memory_status": before.get("memory_status")},
                  new_value={"memory_status": "pending_review"},
                  reason=reason or "标记为待审核", rule_version=rule_version,
                  rollback_info={"reverse_action": "mark_review_revert",
                                 "previous_memory_status": before.get("memory_status")})
    _event(conn, "governance.marked_review",
           f"memory_id={memory_id}; reason={reason[:200]}; approver={approver[:50]}")
    return before, None


def set_review_status(memory_id: int, reason: str = "", approver: str = "user") -> dict:
    """Mark a memory as pending_review (awaiting human adjudication)."""
    row = _one("SELECT * FROM memories WHERE id=?", (int(memory_id),))
    if not row:
        return {"ok": False, "message": "Memory not found", "memory_id": int(memory_id)}
    rules = load_rules()
    try:
        with db.transaction() as conn:
            before, err = _mark_review(conn, memory_id, reason,
                                       rules["rule_version"], approver)
            if err:
                raise RuntimeError(err)
        return {"ok": True, "memory_id": int(memory_id), "memory_status": "pending_review"}
    except Exception as exc:
        return {"ok": False, "message": str(exc), "memory_id": int(memory_id)}
