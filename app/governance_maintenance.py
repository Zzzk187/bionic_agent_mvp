"""Phase 5.4 — autonomous memory maintenance loop.

This module orchestrates the end-to-end governance cycle without polluting the
Phase 1-4 task-state machinery:

    read candidates -> score -> recall similar -> duplicate/conflict detection
    -> generate governance actions -> preview / request approval / apply
    -> audit -> (vector sync is implicit: active search already filters on
    `status`, so archiving/sharing the vector is enough) -> report.

All multi-row operations run inside `db.transaction()` so a partial failure
leaves nothing half-written. Re-runs are idempotent: an already-archived memory
is never a candidate again, and a pending action for the same intent is not
re-created.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app import db
from app.governance_rules import load_rules
from app.governance_scoring import score_memory, archive_candidate
from app.governance_conflicts import classify_pair, needs_review, lexical_similarity
from app.governance_service import (
    APPROVAL_PENDING, APPROVAL_APPROVED, APPROVAL_REJECTED,
    EXEC_PENDING, EXEC_SUCCESS, EXEC_FAILED, EXEC_SKIPPED,
    ACTION_ARCHIVE, ACTION_RESTORE, ACTION_PROTECT, ACTION_UNPROTECT,
    ACTION_MARK_REVIEW, ACTION_SUPERSEDE, ACTION_SCORE,
    _now_str, _json, _one, _rows, _event,
    detect_conflicts, _active_rows, _similarity_matrix,
)

try:
    from app import embeddings as emb
except Exception:  # pragma: no cover
    emb = None


def _parse_ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _days_since(value, now):
    ts = _parse_ts(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() / 86400.0


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def _pending_action_exists(conn, action_type: str, memory_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM governance_actions WHERE action_type=? AND memory_id=? "
        "AND execution_status IN ('pending','success') AND rolled_back_at IS NULL "
        "LIMIT 1",
        (action_type, int(memory_id)),
    ).fetchone()
    return row is not None


def _build_plan(conn, rules: dict, limit: int, category: str,
               persist_conflicts: bool) -> tuple[list[dict], dict]:
    """Compute candidate governance actions for active memories.

    Returns (plan_items, scan_stats). `plan_items` are plain dicts ready to be
    persisted as `governance_actions` (approval_status=pending) or just previewed.
    No database writes happen here except, optionally, conflict detection.
    """
    now = datetime.now(timezone.utc)
    rows = _active_rows(conn, category)
    if limit and limit > 0:
        rows = rows[:limit]

    # Score everything up front (pure function, no DB writes).
    scored = {}
    for row in rows:
        from app.governance_service import build_scoring_context
        ctx = build_scoring_context(conn, row["id"])
        scored[row["id"]] = score_memory(row, ctx, rules, now=now)

    # Duplicate / conflict detection (persisted only when requested).
    conflicts_written = 0
    conflict_findings = []
    if persist_conflicts:
        det = detect_conflicts(persist=True, category=category)
        conflicts_written = det.get("written", 0)
        conflict_findings = det.get("findings", [])
    else:
        # Dry run: compute without writing.
        sims = _similarity_matrix(conn, rows, rules)
        by_id = {r["id"]: r for r in rows}
        neighbours: dict[int, list[tuple[int, float]]] = {}
        for (a, b), sim in sims.items():
            neighbours.setdefault(a, []).append((b, sim))
            neighbours.setdefault(b, []).append((a, sim))
        top_n = int(rules.get("candidate_count", 8))
        for k in neighbours:
            neighbours[k].sort(key=lambda x: x[1], reverse=True)
            neighbours[k] = neighbours[k][:top_n]
        seen: set[tuple[int, int]] = set()
        for a_id, cands in sorted(neighbours.items()):
            for b_id, sim in cands:
                key = (min(a_id, b_id), max(a_id, b_id))
                if key in seen:
                    continue
                seen.add(key)
                old_id, new_id = sorted(key)
                verdict = classify_pair(by_id[old_id], by_id[new_id], sim, rules)
                if verdict is None:
                    continue
                conflict_findings.append({
                    "memory_id": old_id, "candidate_memory_id": new_id,
                    "similarity": sim, "conflict_type": verdict["conflict_type"],
                    "needs_review": needs_review(verdict),
                    **verdict,
                })

    plan: list[dict] = []
    archive_candidates = 0
    review_candidates = 0
    reasons_by_id: dict[int, list[str]] = {}

    for row in rows:
        mid = row["id"]
        sc = scored[mid]
        candidate, cand_reasons = archive_candidate(row, sc, rules)
        # Progressive forgetting guard: still under observation window -> skip.
        obs_days = int(rules.get("observation_period_days", 14))
        last_eval = row.get("last_evaluated_at")
        age_eval = _days_since(last_eval, now)
        under_observation = (age_eval is not None and age_eval < obs_days)
        if candidate and not under_observation and not row.get("protected") \
                and row.get("memory_status") != "pending_review":
            archive_candidates += 1
            reasons_by_id[mid] = cand_reasons
            plan.append({
                "action_type": ACTION_ARCHIVE,
                "memory_id": mid,
                "target_memory_id": None,
                "old_value": {"status": row.get("status"),
                              "memory_status": row.get("memory_status")},
                "new_value": {"status": "archived", "memory_status": "archived"},
                "reason": "低分且低时效，进入归档候选（需审批）: " +
                          "; ".join(cand_reasons)[:400],
                "rollback_info": {"reverse_action": "restore",
                                  "previous_status": row.get("status"),
                                  "previous_memory_status": row.get("memory_status")},
            })

    for f in conflict_findings:
        ftype = f.get("conflict_type")
        # Surface every finding that needs a human decision: explicit review
        # items, plus duplicate/correction findings (route to review_conflict or
        # supersede_from_conflict — never silently overwritten).
        if not (f.get("needs_review") or ftype in ("duplicate", "correction")):
            continue
        mid = f["candidate_memory_id"]
        cur = _one("SELECT memory_status FROM memories WHERE id=?", (int(mid),))
        prev_ms = cur.get("memory_status") if cur else None
        review_candidates += 1
        plan.append({
            "action_type": ACTION_MARK_REVIEW,
            "memory_id": int(mid),
            "target_memory_id": f["memory_id"],
            "old_value": {"memory_status": prev_ms},
            "new_value": {"memory_status": "pending_review"},
            "reason": f"冲突检测建议人工裁决（{ftype}，相似度 {round(f.get('similarity',0),3)}）",
            "rollback_info": {"reverse_action": "mark_review_revert",
                              "previous_memory_status": prev_ms},
        })

    stats = {
        "scanned": len(rows),
        "scored": len(scored),
        "conflicts_considered": len(conflict_findings),
        "conflicts_needing_review": sum(1 for f in conflict_findings if f.get("needs_review")),
        "conflicts_written": conflicts_written,
        "archive_candidates": archive_candidates,
        "review_candidates": review_candidates,
    }
    return plan, stats


def _persist_plan(conn, plan: list[dict], run_id: int | None, rules: dict) -> int:
    """Insert pending governance actions. Idempotent per intent."""
    written = 0
    now = _now_str()
    for item in plan:
        atype = item["action_type"]
        mid = int(item["memory_id"])
        if _pending_action_exists(conn, atype, mid):
            continue
        conn.execute(
            """INSERT INTO governance_actions
               (run_id,action_type,memory_id,target_memory_id,old_value_json,new_value_json,
                reason,rule_version,approval_status,execution_status,executed_at,rollback_info_json)
               VALUES(?,?,?,?,?,?,?,?, 'pending','pending',NULL,?)""",
            (run_id, atype, mid, item.get("target_memory_id"),
             _json(item.get("old_value", {})), _json(item.get("new_value", {})),
             item.get("reason", "")[:500], rules["rule_version"],
             _json(item.get("rollback_info", {}))),
        )
        written += 1
    return written


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def _execute_action(conn, action: dict, approver: str, rules: dict) -> dict:
    """Apply one governance action inside the caller's transaction.

    Updates the SAME action row (status, timestamps, rollback info) rather than
    creating a second action, so rollback_governance_action keeps working.
    Returns a status dict; never raises for expected rejections.
    """
    now = _now_str()
    mid = int(action["memory_id"])
    atype = action["action_type"]

    def _finish(status, error="", new_value=None, rollback_info=None):
        conn.execute(
            """UPDATE governance_actions
               SET approval_status=?, execution_status=?, executed_at=?,
                   error_message=?, new_value_json=COALESCE(?, new_value_json),
                   rollback_info_json=COALESCE(?, rollback_info_json)
               WHERE id=?""",
            (APPROVAL_APPROVED, status, now if status == EXEC_SUCCESS else None,
             error[:500], _json(new_value) if new_value is not None else None,
             _json(rollback_info) if rollback_info is not None else None,
             int(action["id"])),
        )

    if atype == ACTION_ARCHIVE:
        before = db.rowdict(conn.execute(
            "SELECT id,status,memory_status,protected FROM memories WHERE id=?",
            (mid,)).fetchone())
        if not before:
            _finish(EXEC_FAILED, "记忆不存在"); return {"ok": False, "error": "记忆不存在"}
        if before.get("status") != "active":
            _finish(EXEC_SKIPPED, "记忆不是活跃状态"); return {"ok": False, "error": "非活跃"}
        if before.get("protected"):
            _finish(EXEC_SKIPPED, "记忆受保护"); return {"ok": False, "error": "受保护"}
        conn.execute(
            "UPDATE memories SET status='archived', memory_status='archived', "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?", (mid,))
        _event(conn, "governance.archived",
               f"memory_id={mid}; reason={action.get('reason','')[:200]}; "
               f"approver={approver[:50]}; auto=maintenance")
        _finish(EXEC_SUCCESS, "",
                {"status": "archived", "memory_status": "archived"},
                action.get("rollback_info"))
        return {"ok": True, "memory_id": mid, "status": "archived"}

    if atype == ACTION_MARK_REVIEW:
        before = db.rowdict(conn.execute(
            "SELECT id,status,memory_status FROM memories WHERE id=?",
            (mid,)).fetchone())
        if not before:
            _finish(EXEC_FAILED, "记忆不存在"); return {"ok": False, "error": "记忆不存在"}
        prev = before.get("memory_status")
        conn.execute(
            "UPDATE memories SET memory_status='pending_review', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (mid,))
        _event(conn, "governance.marked_review",
               f"memory_id={mid}; reason={action.get('reason','')[:200]}; approver={approver[:50]}")
        rb = {"reverse_action": "mark_review_revert", "previous_memory_status": prev}
        _finish(EXEC_SUCCESS, "", {"memory_status": "pending_review"}, rb)
        return {"ok": True, "memory_id": mid, "memory_status": "pending_review"}

    _finish(EXEC_FAILED, f"动作类型 {atype} 不支持自动执行")
    return {"ok": False, "error": f"unsupported {atype}"}


# --------------------------------------------------------------------------
# Public orchestration
# --------------------------------------------------------------------------

def run_memory_maintenance(mode: str = "dry_run", limit: int = 50,
                          category: str = "", approver: str = "user") -> dict:
    """Run one maintenance cycle.

    mode:
      - "dry_run": score + detect, return the plan, write nothing.
      - "review":   score + detect (persisted) + create PENDING actions, wait
                    for human approval.
      - "apply_approved": execute every action whose approval_status='approved'.
    """
    valid = {"dry_run", "review", "apply_approved"}
    if mode not in valid:
        return {"ok": False, "message": f"未知模式：{mode}（可选 {sorted(valid)}）"}
    rules = load_rules()
    limit = max(0, min(int(limit), 500))

    if mode == "apply_approved":
        return _apply_approved(approver=approver, limit=limit, category=category,
                               rules=rules)

    # dry_run / review: build the plan.
    run_id = None
    plan_items = []
    stats = {}
    if mode == "review":
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO governance_runs(mode,status,rule_version,started_at) "
                "VALUES(?, 'running', ?, ?)",
                (mode, rules["rule_version"], _now_str()))
            run_id = cur.lastrowid
    try:
        with db.connect() as conn:
            if mode == "review":
                # Persist scores so archive_candidate / future runs see them.
                from app.governance_service import score_memories
                score_memories(persist=True, limit=limit or 500,
                               category=category)
            plan_items, stats = _build_plan(
                conn, rules, limit, category, persist_conflicts=(mode == "review"))
        written = 0
        if mode == "review":
            with db.transaction() as conn:
                written = _persist_plan(conn, plan_items, run_id, rules)
                conn.execute(
                    """UPDATE governance_runs SET status='completed',
                          finished_at=?, candidates_scanned=?, actions_generated=?
                       WHERE id=?""",
                    (_now_str(), stats.get("scanned", 0), written, run_id))
                _event(conn, "governance.maintenance_review",
                       f"run_id={run_id}; mode=review; candidates={written}")
        return {
            "ok": True,
            "mode": mode,
            "run_id": run_id,
            "rule_version": rules["rule_version"],
            "stats": stats,
            "actions_written": written,
            "plan": plan_items if mode == "dry_run" else None,
            "message": ("预览完成，未写入任何动作" if mode == "dry_run"
                        else f"已生成 {written} 条待审批动作（run_id={run_id}）"),
        }
    except Exception as exc:
        if run_id is not None:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE governance_runs SET status='failed', finished_at=? WHERE id=?",
                    (_now_str(), run_id))
        return {"ok": False, "mode": mode, "run_id": run_id, "message": str(exc)}


def plan_memory_cleanup(limit: int = 50, category: str = "",
                       dry_run: bool = True) -> dict:
    """Convenience wrapper: produce a maintenance plan.

    dry_run=True -> same as run_memory_maintenance(mode="dry_run").
    dry_run=False -> same as run_memory_maintenance(mode="review").
    """
    return run_memory_maintenance(
        mode="dry_run" if dry_run else "review",
        limit=limit, category=category)


def _apply_approved(approver: str = "user", limit: int = 50,
                   category: str = "", rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    limit = max(1, min(int(limit), 500))
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO governance_runs(mode,status,rule_version,started_at) "
            "VALUES('apply_approved','running',?,?)",
            (rules["rule_version"], _now_str()))
        run_id = cur.lastrowid
        rows = [db.rowdict(r) for r in conn.execute(
            "SELECT * FROM governance_actions "
            "WHERE approval_status='approved' AND execution_status='pending' "
            "ORDER BY id LIMIT ?", (limit,)).fetchall()]
    applied = 0
    failed = 0
    skipped = 0
    details = []
    for action in rows:
        aid = int(action["id"])
        try:
            with db.transaction() as conn:
                # Re-fetch inside the transaction (could have changed).
                fresh = db.rowdict(conn.execute(
                    "SELECT * FROM governance_actions WHERE id=?", (aid,)).fetchone())
                if not fresh:
                    skipped += 1
                    details.append({"action_id": aid, "ok": False, "error": "not found"})
                    continue
                if fresh["execution_status"] == EXEC_SUCCESS:
                    skipped += 1
                    details.append({"action_id": aid, "ok": True, "skipped": True})
                    continue
                res = _execute_action(conn, fresh, approver, rules)
                if res.get("ok"):
                    applied += 1
                else:
                    failed += 1
                details.append({"action_id": aid, **res})
        except Exception as exc:
            failed += 1
            details.append({"action_id": aid, "ok": False, "error": str(exc)})
    with db.transaction() as conn:
        conn.execute(
            """UPDATE governance_runs SET status='completed', finished_at=?,
                  actions_applied=?, actions_failed=? WHERE id=?""",
            (_now_str(), applied, failed, run_id))
        _event(conn, "governance.maintenance_applied",
               f"run_id={run_id}; applied={applied}; failed={failed}")
    return {
        "ok": True,
        "mode": "apply_approved",
        "run_id": run_id,
        "applied": applied,
        "failed": failed,
        "skipped": skipped,
        "details": details,
        "message": f"已执行 {applied} 条，失败 {failed} 条，跳过 {skipped} 条",
    }


def apply_governance_actions(action_ids: list[int], approver: str = "user") -> dict:
    """Human approval + execution in one step.

    The caller passes the exact action ids to apply; that list IS the approval.
    Each action is approved (approval_status='approved') and then executed.
    Per-action failures are recorded; the rest still proceed.
    """
    rules = load_rules()
    if not action_ids:
        return {"ok": False, "message": "action_ids 为空"}
    cleaned = []
    for i in action_ids:
        try:
            cleaned.append(int(i))
        except (TypeError, ValueError):
            return {"ok": False, "message": f"action_ids 含非整数值：{i!r}"}
    applied = failed = skipped = 0
    details = []
    for aid in cleaned:
        try:
            with db.transaction() as conn:
                fresh = db.rowdict(conn.execute(
                    "SELECT * FROM governance_actions WHERE id=?", (int(aid),)).fetchone())
                if not fresh:
                    skipped += 1
                    details.append({"action_id": aid, "ok": False, "error": "not found"})
                    continue
                if fresh["execution_status"] == EXEC_SUCCESS:
                    skipped += 1
                    details.append({"action_id": aid, "ok": True, "skipped": True})
                    continue
                conn.execute(
                    "UPDATE governance_actions SET approval_status='approved' WHERE id=?",
                    (int(aid),))
                res = _execute_action(conn, fresh, approver, rules)
                if res.get("ok"):
                    applied += 1
                else:
                    failed += 1
                details.append({"action_id": aid, **res})
        except Exception as exc:
            failed += 1
            details.append({"action_id": aid, "ok": False, "error": str(exc)})
    return {
        "ok": True,
        "applied": applied,
        "failed": failed,
        "skipped": skipped,
        "details": details,
        "message": f"已执行 {applied} 条，失败 {failed} 条，跳过 {skipped} 条",
    }


def review_governance_actions(approval_status: str = "", execution_status: str = "",
                             run_id: int | None = None, limit: int = 50) -> dict:
    """List governance actions for human review, filtered by status."""
    limit = max(1, min(int(limit), 200))
    sql = "SELECT * FROM governance_actions"
    where, args = [], []
    if approval_status.strip():
        where.append("approval_status=?")
        args.append(approval_status.strip()[:20])
    if execution_status.strip():
        where.append("execution_status=?")
        args.append(execution_status.strip()[:20])
    if run_id is not None:
        where.append("run_id=?")
        args.append(int(run_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    rows = _rows(sql, args)
    return {"ok": True, "count": len(rows), "actions": rows}


def get_governance_run(run_id: int) -> dict:
    row = _one("SELECT * FROM governance_runs WHERE id=?", (int(run_id),))
    if not row:
        return {"ok": False, "message": "Run not found", "run_id": int(run_id)}
    actions = _rows(
        "SELECT id,action_type,memory_id,target_memory_id,approval_status,"
        "execution_status,reason,rolled_back_at FROM governance_actions "
        "WHERE run_id=? ORDER BY id", (int(run_id),))
    summary = {
        "total": len(actions),
        "pending": sum(1 for a in actions if a["approval_status"] == APPROVAL_PENDING),
        "approved": sum(1 for a in actions if a["approval_status"] == APPROVAL_APPROVED),
        "succeeded": sum(1 for a in actions if a["execution_status"] == EXEC_SUCCESS),
        "failed": sum(1 for a in actions if a["execution_status"] == EXEC_FAILED),
        "rolled_back": sum(1 for a in actions if a.get("rolled_back_at")),
    }
    row["ok"] = True
    row["summary"] = summary
    row["actions"] = actions
    return row


def get_vector_consistency() -> dict:
    """Verify vector-index consistency with current content.

    - Active memories missing a vector -> pending re-index.
    - Active memories whose stored vector hash != current content -> stale.
    - Archived memories must KEEP their vectors (we never delete on archive).
    """
    from app import embeddings as _emb
    with db.connect() as conn:
        rows = [db.rowdict(r) for r in conn.execute(
            "SELECT m.id,m.status,m.content,e.content_hash AS stored_hash,"
            "e.model_name FROM memories m LEFT JOIN memory_embeddings e "
            "ON e.memory_id=m.id ORDER BY m.id").fetchall()]
    active_no_vector = 0
    stale = 0
    archived_with_vector = 0
    archived_no_vector = 0
    for r in rows:
        if r["status"] == "active":
            if r["stored_hash"] is None:
                active_no_vector += 1
            elif _emb is not None and r["stored_hash"] != _emb.content_hash(r["content"] or ""):
                stale += 1
        elif r["status"] == "archived":
            if r["stored_hash"] is not None:
                archived_with_vector += 1
            else:
                archived_no_vector += 1
    return {
        "ok": True,
        "active_without_vector": active_no_vector,
        "active_stale_vector": stale,
        "archived_with_vector_retained": archived_with_vector,
        "archived_without_vector": archived_no_vector,
        "embedding_available": emb is not None,
        "message": "归档记忆的向量应被保留；活跃检索按 status 过滤，无需重算。",
    }
