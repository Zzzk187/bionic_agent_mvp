import sys
from pathlib import Path
import json
import re
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp.server.fastmcp import FastMCP
from app.db import init_db, connect, rowdict
from app.embeddings import (DEFAULT_MODEL, EmbeddingUnavailable, content_hash,
                            embed_passages, embed_query, cosine_similarity,
                            ensure_warmup, model_state)
from app.governance_rules import load_rules, RULE_VERSION
from app import governance_service as gov
from app import governance_maintenance as maint
from app import memory_lifecycle as life
from app import activation_spreading as spreading

# Two FastMCP instances share the same process/module state but are launched as
# separate MCP servers so the host can mount only the lightweight core instance
# for daily use and attach the heavier governance instance on demand. This keeps
# the per-session fixed tool-list token cost down (core = 21 legacy tools only).
# Both import the identical app.* backend, so there is no duplicated logic.
mcp = FastMCP("bionic-agent")                 # core: the 21 legacy Phase 1-4 tools
mcp_gov = FastMCP("bionic-agent-governance")  # governance / lifecycle / Phase 8 tools

# Preload the embedding model in the background at server startup so the first
# semantic search / recall does not block on a network model download (which
# previously tripped the MCP client timeout). The worker downloads once and the
# instance is cached; see app/embeddings.py.
ensure_warmup(DEFAULT_MODEL)


def _ensure_db():
    init_db()


def _keyword_only_search(query: str, limit: int, category: str) -> list[dict]:
    """Keyword-only fallback used while the embedding model is still loading."""
    from app import memory_lifecycle as _life
    # reuse the keyword path from the fused recall (no semantic component).
    rows = _life._keyword_fallback(query, limit=limit, category=category)
    return [_life._clean(dict(r)) for r in rows]

def _event(event_type: str, detail: str, task_id=None):
    with connect() as conn:
        conn.execute(
            "INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
            (task_id, event_type, detail[:2000])
        )

# Phase-5 governance columns. They are intentionally kept OUT of the Phase 1-4
# return shape so existing tool responses stay byte-compatible; read them via
# `get_memory_governance_status` instead.
GOVERNANCE_COLUMNS = {
    "importance_score", "confidence_score", "freshness_score", "access_count",
    "last_accessed_at", "last_evaluated_at", "memory_status", "protected",
}

def _memory_out(row, include_governance: bool = False):
    result = rowdict(row)
    if result and "tags_json" in result:
        try:
            result["tags"] = json.loads(result.pop("tags_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            result["tags"] = []
            result.pop("tags_json", None)
    if result and not include_governance:
        for key in GOVERNANCE_COLUMNS:
            result.pop(key, None)
    return result

@mcp.tool()
def save_memory(content: str, category: str = "general", source: str = "deepseek-harness",
                confidence: float = 0.8, tags: list[str] | None = None) -> dict:
    """保存一条持久记忆；自动去重检索并写审计，返回新记忆 id。"""
    _ensure_db()
    content = content.strip()
    if not content:
        return {"ok": False, "message": "content must not be empty"}
    confidence = max(0.0, min(float(confidence), 1.0))
    safe_tags = list(dict.fromkeys(str(t).strip()[:50] for t in (tags or []) if str(t).strip()))[:20]
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO memories(content,category,source,confidence,tags_json)
               VALUES(?,?,?,?,?)""",
            (content[:10000], category[:100], source[:200], confidence,
             json.dumps(safe_tags, ensure_ascii=False))
        )
        memory_id = cur.lastrowid
        conn.execute("INSERT INTO events(event_type,detail) VALUES(?,?)",
                     ("memory.created", f"memory_id={memory_id}; category={category[:100]}; source={source[:200]}"))
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
    return _memory_out(row)

@mcp.tool()
def search_memories(query: str = "", limit: int = 20, category: str = "", include_archived: bool = False) -> list[dict]:
    """按关键词/分类/标签检索记忆（非语义）。空查询列出最近记忆。"""
    _ensure_db()
    limit = max(1, min(int(limit), 100))
    statuses = "('active','archived')" if include_archived else "('active')"
    params = []
    clauses = [f"status IN {statuses}", "memory_status != 'pending_review'"]
    if category.strip():
        clauses.append("category = ?")
        params.append(category.strip()[:100])
    query = query.strip()
    if query:
        terms = [t for t in re.split(r"\s+", query) if t][:8]
        # AND matching avoids returning unrelated records; search both content and tags.
        for term in terms:
            clauses.append("(content LIKE ? OR tags_json LIKE ?)")
            params.extend([f"%{term}%", f"%{term}%"])
    sql = "SELECT * FROM memories WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_memory_out(r) for r in rows]

@mcp.tool()
def find_related_memories(query: str, limit: int = 10, category: str = "") -> list[dict]:
    """按词重叠+短语加成对相关记忆排序（轻量，非向量）。"""
    _ensure_db()
    query = query.strip()
    if not query:
        return []
    terms = list(dict.fromkeys(t.lower() for t in re.findall(r"[\w\u4e00-\u9fff]+", query) if len(t) > 1))[:20]
    if not terms:
        return []
    limit = max(1, min(int(limit), 50))
    with connect() as conn:
        if category.strip():
            rows = conn.execute("SELECT * FROM memories WHERE status='active' AND memory_status != 'pending_review' AND category=? ORDER BY updated_at DESC LIMIT 500",
                                (category.strip()[:100],)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM memories WHERE status='active' AND memory_status != 'pending_review' ORDER BY updated_at DESC LIMIT 500").fetchall()
    scored = []
    for row in rows:
        item = _memory_out(row)
        text = (item.get("content", "") + " " + " ".join(item.get("tags", []))).lower()
        hits = sum(1 for term in terms if term in text)
        if hits == 0:
            continue
        score = hits / max(1, len(terms))
        if query.lower() in text:
            score += 0.5
        score += min(float(item.get("confidence", 0.5)), 1.0) * 0.05
        item["relevance_score"] = round(score, 4)
        scored.append(item)
    scored.sort(key=lambda x: (x["relevance_score"], x.get("updated_at", "")), reverse=True)
    return scored[:limit]


@mcp.tool()
def link_memories(source_memory_id: int, target_memory_id: int, relation: str = "related", note: str = "") -> dict:
    """在 memory_links 建立两条记忆的有类型关系。"""
    _ensure_db()
    if int(source_memory_id) == int(target_memory_id):
        return {"ok": False, "message": "A memory cannot link to itself"}
    allowed = {"related", "supports", "contradicts", "supersedes"}
    relation = relation.strip().lower()
    if relation not in allowed:
        return {"ok": False, "message": "relation must be one of: related, supports, contradicts, supersedes"}
    with connect() as conn:
        a = conn.execute("SELECT id FROM memories WHERE id=?", (int(source_memory_id),)).fetchone()
        b = conn.execute("SELECT id FROM memories WHERE id=?", (int(target_memory_id),)).fetchone()
        if not a or not b:
            return {"ok": False, "message": "Both memories must exist"}
        cur = conn.execute(
            "INSERT INTO memory_links(source_memory_id,target_memory_id,relation,note) VALUES(?,?,?,?)",
            (int(source_memory_id), int(target_memory_id), relation, note[:1000]),
        )
        conn.execute("INSERT INTO events(event_type,detail) VALUES(?,?)",
                     ("memory.linked", f"link_id={cur.lastrowid}; source={source_memory_id}; target={target_memory_id}; relation={relation}"))
        return {"ok": True, "link_id": cur.lastrowid, "source_memory_id": int(source_memory_id),
                "target_memory_id": int(target_memory_id), "relation": relation}


@mcp.tool()
def get_memory_links(memory_id: int) -> list[dict]:
    """列出某记忆的全部双向关系。"""
    _ensure_db()
    with connect() as conn:
        exists = conn.execute("SELECT id FROM memories WHERE id=?", (int(memory_id),)).fetchone()
        if not exists:
            return []
        rows = conn.execute(
            """SELECT l.*, a.content AS source_content, b.content AS target_content
               FROM memory_links l
               JOIN memories a ON a.id=l.source_memory_id
               JOIN memories b ON b.id=l.target_memory_id
               WHERE l.source_memory_id=? OR l.target_memory_id=?
               ORDER BY l.id DESC LIMIT 100""",
            (int(memory_id), int(memory_id)),
        ).fetchall()
    return [rowdict(r) for r in rows]


@mcp.tool()
def supersede_memory(old_memory_id: int, new_content: str, category: str = "",
                     source: str = "user-confirmed", confidence: float = 0.95,
                     note: str = "") -> dict:
    """记录新事实并把旧记忆标 superseded/archived（需用户确认）。"""
    _ensure_db()
    new_content = new_content.strip()
    if not new_content:
        return {"ok": False, "message": "new_content must not be empty"}
    confidence = max(0.0, min(float(confidence), 1.0))
    with connect() as conn:
        old = conn.execute("SELECT * FROM memories WHERE id=? AND status='active'", (int(old_memory_id),)).fetchone()
        if not old:
            return {"ok": False, "message": "Active old memory not found", "memory_id": int(old_memory_id)}
        cat = category.strip()[:100] or old["category"]
        cur = conn.execute(
            """INSERT INTO memories(content,category,source,confidence,status,tags_json,supersedes_id)
               VALUES(?,?,?,?, 'active', ?, ?)""",
            (new_content[:10000], cat, source[:200], confidence, old["tags_json"] if "tags_json" in old.keys() else "[]", int(old_memory_id)),
        )
        new_id = cur.lastrowid
        conn.execute("UPDATE memories SET status='archived',updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(old_memory_id),))
        conn.execute(
            "INSERT INTO memory_links(source_memory_id,target_memory_id,relation,note) VALUES(?,?,?,?)",
            (new_id, int(old_memory_id), "supersedes", note[:1000]),
        )
        conn.execute("INSERT INTO events(event_type,detail) VALUES(?,?)",
                     ("memory.superseded", f"old_id={old_memory_id}; new_id={new_id}; note={note[:300]}"))
        row = conn.execute("SELECT * FROM memories WHERE id=?", (new_id,)).fetchone()
    return _memory_out(row)


@mcp.tool()
def get_memory(memory_id: int, include_archived: bool = True) -> dict:
    """按 id 取一条记忆（含来源/置信度/时间戳/标签）。"""
    _ensure_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
    if not row:
        return {"ok": False, "message": "Memory not found", "memory_id": int(memory_id)}
    result = _memory_out(row)
    # Phase 6.3: ordinary retrieval (include_archived=False) excludes anything
    # that is not actively retrievable — that is status='active' AND a non
    # hold-state memory_status. pending_review is a human-adjudication hold and
    # must never surface in ordinary retrieval. Read memory_status from the raw
    # row because _memory_out() scrubs governance columns by default.
    if not include_archived and (
        row["status"] != "active" or (row["memory_status"] or "active") == "pending_review"
    ):
        return {"ok": False, "message": "Memory is not active", "memory_id": int(memory_id)}
    return result

@mcp.tool()
def update_memory(memory_id: int, content: str = "", category: str = "", source: str = "",
                  confidence: float | None = None, tags: list[str] | None = None) -> dict:
    """更新记忆指定字段，省略字段不变；保留 provenance。"""
    _ensure_db()
    updates, values = [], []
    if content.strip():
        updates.append("content=?"); values.append(content.strip()[:10000])
    if category.strip():
        updates.append("category=?"); values.append(category.strip()[:100])
    if source.strip():
        updates.append("source=?"); values.append(source.strip()[:200])
    if confidence is not None:
        updates.append("confidence=?"); values.append(max(0.0, min(float(confidence), 1.0)))
    if tags is not None:
        safe_tags = list(dict.fromkeys(str(t).strip()[:50] for t in tags if str(t).strip()))[:20]
        updates.append("tags_json=?"); values.append(json.dumps(safe_tags, ensure_ascii=False))
    if not updates:
        return {"ok": False, "message": "No fields provided to update"}
    updates.extend(["updated_at=CURRENT_TIMESTAMP"])
    values.append(int(memory_id))
    with connect() as conn:
        cur = conn.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id=? AND status='active'", values)
        if not cur.rowcount:
            return {"ok": False, "message": "Active memory not found", "memory_id": int(memory_id)}
        conn.execute("INSERT INTO events(event_type,detail) VALUES(?,?)",
                     ("memory.updated", f"memory_id={memory_id}; fields={','.join(u.split('=')[0] for u in updates)}"))
        row = conn.execute("SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
    return _memory_out(row)

@mcp.tool()
def archive_memory(memory_id: int) -> dict:
    """软归档记忆（不物理删除）。"""
    _ensure_db()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE memories SET status='archived',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='active'",
            (int(memory_id),)
        )
        if not cur.rowcount:
            return {"ok": False, "message": "Memory not found or already archived"}
        conn.execute("INSERT INTO events(event_type,detail) VALUES(?,?)",
                     ("memory.archived", f"memory_id={memory_id}; source=mcp"))
    return {"ok": True, "memory_id": int(memory_id), "status": "archived"}

@mcp.tool()
def restore_memory(memory_id: int) -> dict:
    """把归档记忆恢复为活跃。"""
    _ensure_db()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE memories SET status='active',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='archived'",
            (int(memory_id),)
        )
        if not cur.rowcount:
            return {"ok": False, "message": "Archived memory not found", "memory_id": int(memory_id)}
        conn.execute("INSERT INTO events(event_type,detail) VALUES(?,?)",
                     ("memory.restored", f"memory_id={memory_id}; source=mcp"))
        row = conn.execute("SELECT * FROM memories WHERE id=?", (int(memory_id),)).fetchone()
    return _memory_out(row)

@mcp.tool()
def create_task(title: str, goal: str) -> dict:
    """创建持久任务（含初始计划），不自动执行。"""
    _ensure_db()
    plan = [
        {"step": 1, "title": "Clarify objective and constraints", "status": "pending"},
        {"step": 2, "title": "Create execution plan", "status": "pending"},
        {"step": 3, "title": "Execute and verify", "status": "pending"},
    ]
    with connect() as conn:
        cur = conn.execute("INSERT INTO tasks(title,goal,plan_json) VALUES(?,?,?)",
                           (title[:200], goal[:10000], json.dumps(plan, ensure_ascii=False)))
        task_id = cur.lastrowid
        conn.execute("INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
                     (task_id, "task.created", title[:200]))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    result = rowdict(row)
    result["plan"] = json.loads(result.pop("plan_json"))
    return result

@mcp.tool()
def list_tasks() -> list[dict]:
    """列出最近任务及状态。"""
    _ensure_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 100").fetchall()
    results = []
    for row in rows:
        d = rowdict(row); d["plan"] = json.loads(d.pop("plan_json")); results.append(d)
    return results

@mcp.tool()
def get_task(task_id: int) -> dict:
    """取任务及其阶段快照。"""
    _ensure_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
        if not row:
            return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
        snaps = conn.execute("SELECT * FROM snapshots WHERE task_id=? ORDER BY stage DESC", (int(task_id),)).fetchall()
    d = rowdict(row); d["plan"] = json.loads(d.pop("plan_json")); d["snapshots"] = [rowdict(s) for s in snaps]
    return d

@mcp.tool()
def save_stage_snapshot(task_id: int, summary: str, state_json: str = "{}") -> dict:
    """仅保存检查点，不改任务状态/不推进阶段。"""
    _ensure_db()
    try:
        state = json.loads(state_json)
    except json.JSONDecodeError:
        return {"ok": False, "message": "state_json must be valid JSON"}
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
        if not task:
            return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
        stage = int(task["current_stage"])
        cur = conn.execute("INSERT INTO snapshots(task_id,stage,summary,state_json) VALUES(?,?,?,?)",
                           (int(task_id), stage, summary[:20000], json.dumps(state, ensure_ascii=False)))
        conn.execute("INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
                     (int(task_id), "task.snapshot_saved", f"snapshot_id={cur.lastrowid}; stage={stage}"))
        snap = conn.execute("SELECT * FROM snapshots WHERE id=?", (cur.lastrowid,)).fetchone()
    return rowdict(snap)

@mcp.tool()
def handoff_task(task_id: int, summary: str, state_json: str = "{}") -> dict:
    """保存交接快照并推进到下一阶段（同一事务）。"""
    _ensure_db()
    try:
        state = json.loads(state_json)
    except json.JSONDecodeError:
        return {"ok": False, "message": "state_json must be valid JSON"}
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
        if not task:
            return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
        stage = int(task["current_stage"])
        cur = conn.execute("INSERT INTO snapshots(task_id,stage,summary,state_json) VALUES(?,?,?,?)",
                           (int(task_id), stage, summary[:20000], json.dumps(state, ensure_ascii=False)))
        conn.execute("UPDATE tasks SET status='handoff',current_stage=current_stage+1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (int(task_id),))
        conn.execute("INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
                     (int(task_id), "task.handoff", f"snapshot_id={cur.lastrowid}; next_stage={stage+1}"))
        snap = conn.execute("SELECT * FROM snapshots WHERE id=?", (cur.lastrowid,)).fetchone()
        trow = conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
    out = {"ok": True, "snapshot": rowdict(snap), "status": "handoff", "next_stage": stage + 1}
    td = rowdict(trow)
    td["plan"] = json.loads(td.pop("plan_json"))
    out["task"] = td
    return out

@mcp.tool()
def resume_task(task_id: int) -> dict:
    """把暂停/交接后的任务重新标为活跃。"""
    _ensure_db()
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),)).fetchone()
        if not task:
            return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
        conn.execute("UPDATE tasks SET status='active',updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(task_id),))
        conn.execute("INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
                     (int(task_id), "task.resumed", f"stage={task['current_stage']}"))
    return {"ok": True, "task_id": int(task_id), "status": "active", "stage": int(task["current_stage"])}

@mcp.tool()
def complete_task(task_id: int, result: str = "") -> dict:
    """标记任务完成并记录结果。"""
    _ensure_db()
    with connect() as conn:
        cur = conn.execute("UPDATE tasks SET status='completed',result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (result[:20000], int(task_id)))
        if not cur.rowcount:
            return {"ok": False, "message": "Task not found", "task_id": int(task_id)}
        conn.execute("INSERT INTO events(task_id,event_type,detail) VALUES(?,?,?)",
                     (int(task_id), "task.completed", result[:500]))
    return {"ok": True, "task_id": int(task_id), "status": "completed"}

@mcp.tool()
def list_events(task_id: int = 0, limit: int = 50) -> list[dict]:
    """查看审计轨迹；task_id=0 表示全部。"""
    _ensure_db()
    limit = max(1, min(int(limit), 200))
    with connect() as conn:
        if int(task_id) > 0:
            rows = conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                                (int(task_id), limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [rowdict(r) for r in rows]

@mcp.tool()
def embedding_status() -> dict:
    """查看嵌入模型与向量索引覆盖（不下载模型）。"""
    _ensure_db()
    from app.embeddings import model_state, is_ready
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        indexed = conn.execute("""SELECT COUNT(*) FROM memory_embeddings e JOIN memories m ON m.id=e.memory_id
               WHERE m.status='active' AND e.model_name=?""", (DEFAULT_MODEL,)).fetchone()[0]
        stale = conn.execute("""SELECT COUNT(*) FROM memories m LEFT JOIN memory_embeddings e ON e.memory_id=m.id
               WHERE m.status='active' AND (e.memory_id IS NULL OR e.model_name!=? OR e.content_hash!=?)""",
            (DEFAULT_MODEL, '')).fetchone()[0]
    st = model_state()
    status = st.get("status")
    note = ("首次生成向量时会下载模型（已在后台预热）；下载期间语义检索会即时降级为关键词检索，"
            "不会阻塞或超时。语义检索使用本地向量，不调用外部 API。")
    if status == "pending":
        note = "模型正在后台下载/加载，暂未完成；此期间语义检索即时降级为关键词检索，不会阻塞。"
    elif status == "failed":
        note = f"模型加载失败：{st.get('error','')}。语义检索将降级为关键词检索；请检查网络/依赖后重试。"
    elif status == "ready":
        note = "模型已就绪；语义检索使用本地向量，不调用外部 API。"
    return {"model": DEFAULT_MODEL, "backend": "FastEmbed / ONNX Runtime", "total_active_memories": total,
            "indexed_active_memories": indexed, "needs_indexing_or_refresh": max(0, total-indexed),
            "model_load_status": status, "model_ready": bool(is_ready()), "note": note}


@mcp_gov.tool()
def warmup_embeddings() -> dict:
    """后台预加载嵌入模型；立即返回当前加载状态。"""
    from app.embeddings import model_state
    ensure_warmup(DEFAULT_MODEL)
    return {"ok": True, "model": DEFAULT_MODEL, **model_state()}

@mcp.tool()
def index_memories(batch_size: int = 32, force_rebuild: bool = False) -> dict:
    """批量建立/刷新向量索引；可重复、安全。"""
    _ensure_db()
    batch_size = max(1, min(int(batch_size), 128))
    with connect() as conn:
        rows = conn.execute("""SELECT m.id,m.content,e.content_hash AS indexed_hash,e.model_name AS indexed_model
               FROM memories m LEFT JOIN memory_embeddings e ON e.memory_id=m.id
               WHERE m.status='active' ORDER BY m.id""").fetchall()
    pending = [r for r in rows if force_rebuild or r['indexed_hash'] != content_hash(r['content']) or r['indexed_model'] != DEFAULT_MODEL]
    indexed_now = 0
    try:
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset:offset+batch_size]
            vectors = embed_passages([r['content'] for r in batch])
            with connect() as conn:
                for row, vector in zip(batch, vectors):
                    conn.execute("""INSERT INTO memory_embeddings(memory_id,model_name,content_hash,dimension,vector_json,updated_at)
                           VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                           ON CONFLICT(memory_id) DO UPDATE SET model_name=excluded.model_name,
                           content_hash=excluded.content_hash,dimension=excluded.dimension,
                           vector_json=excluded.vector_json,updated_at=CURRENT_TIMESTAMP""",
                        (row['id'], DEFAULT_MODEL, content_hash(row['content']), len(vector), json.dumps(vector)))
            indexed_now += len(batch)
    except EmbeddingUnavailable as exc:
        return {"ok": False, "error": str(exc), "indexed_this_run": indexed_now,
                "pending_at_start": len(pending), "hint": "安装依赖并确保首次模型下载可访问后重试。"}
    return {"ok": True, "model": DEFAULT_MODEL, "indexed_this_run": indexed_now,
            "pending_at_start": len(pending), "message": "向量索引已更新；原 memories 数据未修改。"}

@mcp.tool()
def semantic_search_memories(query: str, limit: int = 10, category: str = "",
                             min_score: float = -1.0, auto_index: bool = True) -> list[dict] | dict:
    """按多语言向量语义检索活跃记忆；默认自动补齐新向量。"""
    _ensure_db()
    query = query.strip()
    if not query:
        return []
    limit = max(1, min(int(limit), 50))
    min_score = max(-1.0, min(float(min_score), 1.0))
    if auto_index:
        result = index_memories(batch_size=32, force_rebuild=False)
        if not result.get('ok'):
            return result
    try:
        query_vector = embed_query(query)
    except EmbeddingUnavailable as exc:
        # Model still downloading (first use) or load failed: degrade to keyword
        # immediately instead of blocking on the MCP client timeout.
        from app.embeddings import is_ready
        if not is_ready():
            return {"ok": True, "degraded": True, "reason": "model_not_ready",
                    "message": "嵌入模型仍在后台加载/下载，已降级为关键词检索。可稍后重试或先调用 warmup_embeddings。",
                    "results": _keyword_only_search(query, limit, category)}
        return {"ok": False, "error": str(exc), "hint": "检查 fastembed 依赖和模型下载。"}
    with connect() as conn:
        if category.strip():
            rows = conn.execute("""SELECT m.*,e.vector_json,e.model_name,e.content_hash FROM memories m
                   JOIN memory_embeddings e ON e.memory_id=m.id
                   WHERE m.status='active' AND m.memory_status != 'pending_review' AND m.category=? AND e.model_name=? ORDER BY m.id""",
                (category.strip()[:100], DEFAULT_MODEL)).fetchall()
        else:
            rows = conn.execute("""SELECT m.*,e.vector_json,e.model_name,e.content_hash FROM memories m
                   JOIN memory_embeddings e ON e.memory_id=m.id
                   WHERE m.status='active' AND m.memory_status != 'pending_review' AND e.model_name=? ORDER BY m.id""", (DEFAULT_MODEL,)).fetchall()
    scored = []
    for row in rows:
        if row['content_hash'] != content_hash(row['content']):
            continue
        try:
            vector = json.loads(row['vector_json'])
        except (TypeError, json.JSONDecodeError):
            continue
        score = cosine_similarity(query_vector, vector)
        if score < min_score:
            continue
        item = _memory_out(row)
        for key in ('vector_json', 'model_name', 'content_hash'):
            item.pop(key, None)
        item['similarity'] = round(score, 6)
        scored.append(item)
    scored.sort(key=lambda x: (x['similarity'], x.get('updated_at', '')), reverse=True)
    return scored[:limit]

# ==========================================================================
# Phase 5: autonomous memory governance
# ==========================================================================

@mcp_gov.tool()
def score_memories(memory_ids: list[int] | None = None, limit: int = 50,
                   category: str = "", persist: bool = True) -> dict:
    """对活跃记忆评分（重要性/置信/时效/稳定）及解释。"""
    _ensure_db()
    if memory_ids is not None:
        if not isinstance(memory_ids, list):
            return {"ok": False, "message": "memory_ids must be a list of integers"}
        cleaned = []
        for item in memory_ids:
            try:
                cleaned.append(int(item))
            except (TypeError, ValueError):
                return {"ok": False, "message": f"memory_ids contains non-integer value: {item!r}"}
        memory_ids = cleaned
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        return {"ok": False, "message": "limit must be an integer"}
    return gov.score_memories(memory_ids=memory_ids, limit=limit,
                              category=str(category or ""), persist=bool(persist))


@mcp_gov.tool()
def get_memory_governance_status(memory_id: int) -> dict:
    """某记忆的治理视图：评分、保护、归档候选理由、向量状态。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.get_governance_status(memory_id)


@mcp_gov.tool()
def protect_memory(memory_id: int, protected: bool = True, reason: str = "") -> dict:
    """设置/清除保护标志；受保护记忆不被自动归档/替代/覆盖。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.set_protected(memory_id, bool(protected), str(reason or ""))


# ==========================================================================
# Phase 5.2 — duplicate / conflict detection and human adjudication
# ==========================================================================

@mcp_gov.tool()
def detect_memory_conflicts(memory_ids: list[int] | None = None, category: str = "",
                            candidate_count: int | None = None,
                            persist: bool = True) -> dict:
    """对活跃记忆做重复/冲突检测；返回双方、理由、置信度、推荐动作。"""
    _ensure_db()
    try:
        ids = [int(i) for i in (memory_ids or [])]
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_ids must be a list of integers"}
    return gov.detect_conflicts(memory_ids=ids or None, category=category,
                                 candidate_count=candidate_count, persist=persist)


@mcp_gov.tool()
def list_memory_conflicts(memory_id: int | None = None, resolution: str = "",
                          limit: int = 50) -> dict:
    """列出已记录的重复/冲突发现（可按记忆/状态过滤）。"""
    _ensure_db()
    return gov.list_conflicts(memory_id=memory_id, resolution=resolution, limit=limit)


@mcp_gov.tool()
def review_memory_conflict(conflict_id: int, resolution: str, note: str = "") -> dict:
    """人工裁决一条冲突（duplicate_confirmed/not_conflict/superseded/kept_both/pending）。"""
    _ensure_db()
    try:
        conflict_id = int(conflict_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "conflict_id must be an integer"}
    return gov.review_conflict(conflict_id, resolution=resolution, note=note,
                               resolved_by="user")


@mcp_gov.tool()
def supersede_memory_from_conflict(conflict_id: int, new_content: str = "",
                                   source: str = "user-confirmed",
                                   confidence: float = 0.95, note: str = "") -> dict:
    """应用明确纠正：建新版本、旧记忆 superseded、保留 derived_from。"""
    _ensure_db()
    try:
        conflict_id = int(conflict_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "conflict_id must be an integer"}
    return gov.supersede_from_conflict(conflict_id, new_content=new_content,
                                       source=source, confidence=confidence, note=note)


# ==========================================================================
# Phase 5.3 — controlled archive / restore / protection / rollback
# ==========================================================================

@mcp_gov.tool()
def archive_memory_governance(memory_id: int, reason: str = "",
                             approver: str = "user") -> dict:
    """受控软归档；服务端校验仅活跃+非保护可归档；审计可逆。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.archive_memory_governance(memory_id, reason=str(reason or ""),
                                         approver=str(approver or "user"))


@mcp_gov.tool()
def restore_memory_governance(memory_id: int, reason: str = "",
                             approver: str = "user") -> dict:
    """受控恢复归档记忆到活跃检索；审计可逆。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.restore_memory_governance(memory_id, reason=str(reason or ""),
                                         approver=str(approver or "user"))


@mcp_gov.tool()
def get_governance_action(action_id: int) -> dict:
    """按 id 取治理动作及其回滚信息。"""
    _ensure_db()
    try:
        action_id = int(action_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "action_id must be an integer"}
    return gov.get_governance_action(action_id)


@mcp_gov.tool()
def rollback_governance_action(action_id: int, reason: str = "") -> dict:
    """回滚已执行的可逆治理动作（archive/restore/protect/mark_review/supersede）。"""
    _ensure_db()
    try:
        action_id = int(action_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "action_id must be an integer"}
    return gov.rollback_governance_action(action_id, reason=str(reason or ""))


@mcp_gov.tool()
def set_review_status(memory_id: int, reason: str = "", approver: str = "user") -> dict:
    """把记忆标 pending_review（人工裁决保留）；排除自动归档候选。"""
    _ensure_db()
    try:
        memory_id = int(memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "memory_id must be an integer"}
    return gov.set_review_status(memory_id, reason=str(reason or ""),
                                 approver=str(approver or "user"))


# ==========================================================================
# Phase 5.4 — autonomous maintenance loop (dry_run / review / apply_approved)
# ==========================================================================

@mcp_gov.tool()
def run_memory_maintenance(mode: str = "dry_run", limit: int = 50,
                          category: str = "", approver: str = "user") -> dict:
    """治理闭环：dry_run 预览 / review 待批 / apply_approved 执行。"""
    _ensure_db()
    if mode not in ("dry_run", "review", "apply_approved"):
        return {"ok": False, "message": "mode 必须是 dry_run / review / apply_approved"}
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"ok": False, "message": "limit must be an integer"}
    return maint.run_memory_maintenance(
        mode=str(mode), limit=limit, category=str(category or ""),
        approver=str(approver or "user"))


@mcp_gov.tool()
def plan_memory_cleanup(limit: int = 50, category: str = "",
                       dry_run: bool = True) -> dict:
    """生成整理计划（dry_run 预览 / 持久化为待批候选）。"""
    _ensure_db()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"ok": False, "message": "limit must be an integer"}
    return maint.plan_memory_cleanup(limit=limit, category=str(category or ""),
                                     dry_run=bool(dry_run))


@mcp_gov.tool()
def review_governance_actions(approval_status: str = "", execution_status: str = "",
                             run_id: int = None, limit: int = 50) -> dict:
    """按状态列出治理动作供人工审查。"""
    _ensure_db()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"ok": False, "message": "limit must be an integer"}
    rid = None
    if run_id is not None:
        try:
            rid = int(run_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "run_id must be an integer"}
    return maint.review_governance_actions(
        approval_status=str(approval_status or ""),
        execution_status=str(execution_status or ""),
        run_id=rid, limit=limit)


@mcp_gov.tool()
def apply_governance_actions(action_ids: list, approver: str = "user") -> dict:
    """审批并执行指定动作（服务端重验权限）。"""
    _ensure_db()
    if not isinstance(action_ids, (list, tuple)):
        return {"ok": False, "message": "action_ids must be a list"}
    try:
        ids = [int(x) for x in action_ids]
    except (TypeError, ValueError):
        return {"ok": False, "message": "action_ids 含非整数值"}
    return maint.apply_governance_actions(ids, approver=str(approver or "user"))


@mcp_gov.tool()
def get_governance_run(run_id: int) -> dict:
    """取一次治理运行的动作汇总与每动作状态。"""
    _ensure_db()
    try:
        run_id = int(run_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "run_id must be an integer"}
    return maint.get_governance_run(run_id)


@mcp_gov.tool()
def get_vector_consistency() -> dict:
    """检查向量索引与内容一致性（缺失/过期/归档保留）。"""
    _ensure_db()
    return maint.get_vector_consistency()


# ---------------------------------------------------------------------------
# Phase 6 + 7 tool surface (additive). Each new tool is documented with its
# parameters and its risk profile; high-risk writes are split into preview /
# approve / execute and are enforced server-side (caller privileges are NEVER
# trusted). Existing Phase 1-5 tools are left untouched.
# ---------------------------------------------------------------------------
@mcp_gov.tool()
def recall_memories(query: str = "", top_k: int = 5, category: str = "",
                    memory_type: str = "", include_related: bool = False,
                    explain: bool = False) -> dict:
    """融合语义+关键词+治理分的可解释检索；含关联子图与激活扩散；稳定排序。"""
    _ensure_db()
    if not isinstance(top_k, int):
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            top_k = 5
    top_k = max(1, min(top_k, 50))
    from app.embeddings import is_ready
    model_ready = is_ready()
    # Ensure the model is warming up even if a request is the first trigger.
    ensure_warmup(DEFAULT_MODEL)
    with connect() as conn:
        rows = life.recall_memories(
            query=query, top_k=top_k, category=category, memory_type=memory_type,
            include_related=include_related, explain=explain, conn=conn)
    result = {"results": rows, "count": len(rows),
              "weights": life.RECALL_WEIGHTS if explain else None}
    # Surface whether semantic relevance was actually applied (vs keyword-only
    # degradation while the model is loading) so the caller can decide to retry.
    if query.strip() and not model_ready:
        result["semantic_status"] = "degraded"
        result["semantic_note"] = ("嵌入模型仍在后台加载/下载，本次结果按关键词相关性返回；"
                                   "模型就绪后语义排序将生效。可稍后重试或先调用 warmup_embeddings。")
    return result


@mcp_gov.tool()
def get_memory_details(memory_id: int) -> dict:
    """某记忆完整视图：基础字段+生命周期+治理+反馈+关系。"""
    _ensure_db()
    if not isinstance(memory_id, int):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "memory_id must be an integer"}
    return life.get_memory_details(memory_id)


@mcp_gov.tool()
def get_related_memories(memory_id: int, max_depth: int = 2, relation: str = "",
                        max_nodes: int = 30) -> dict:
    """返回某记忆的关系子图；防环、限深限节点。"""
    _ensure_db()
    if not isinstance(memory_id, int):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "memory_id must be an integer"}
    return life.get_related_memories(memory_id, max_depth=max_depth,
                                     relation=relation, max_nodes=max_nodes)


@mcp_gov.tool()
def record_memory_feedback(memory_id: int, feedback_type: str, note: str = "",
                          approver: str = "user") -> dict:
    """记录一条使用反馈信号（retrieved/used/helpful/not_relevant/corrected）。"""
    _ensure_db()
    if not isinstance(memory_id, int):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "memory_id must be an integer"}
    return life.record_memory_feedback(memory_id, feedback_type, note=note, approver=approver)


@mcp_gov.tool()
def classify_memory(memory_id: int = 0, content: str = "", category: str = "",
                   source: str = "") -> dict:
    """用确定性信号把记忆归入类型分类（猜测，不写库）。"""
    _ensure_db()
    if memory_id and memory_id > 0:
        with connect() as conn:
            row = conn.execute(
                "SELECT content, category, source FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row:
                return {"ok": False, "message": "memory not found"}
            content = row["content"] or ""
            category = row["category"] or ""
            source = row["source"] or ""
    return life.classify_memory_type(content, category, source)


@mcp_gov.tool()
def find_duplicate_memories(memory_ids: list = None, category: str = "",
                           candidate_count: int = 8) -> dict:
    """在活跃记忆中找重复/近似/纠正对（复用冲突检测）。"""
    _ensure_db()
    ids = None
    if memory_ids:
        if not isinstance(memory_ids, (list, tuple)):
            return {"ok": False, "message": "memory_ids must be a list"}
        ids = [int(x) for x in memory_ids]
    if not isinstance(candidate_count, int):
        candidate_count = 8
    candidate_count = max(5, min(candidate_count, 10))
    return life.find_duplicate_memories(memory_ids=ids, category=category,
                                        candidate_count=candidate_count)


@mcp_gov.tool()
def create_memory_relation(source_memory_id: int, target_memory_id: int,
                           relation: str = "related_to", note: str = "",
                           approver: str = "user") -> dict:
    """在 memory_links 建关系；防环、幂等、校验合法性。"""
    _ensure_db()
    try:
        s, t = int(source_memory_id), int(target_memory_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "source/target memory ids must be integers"}
    return life.create_memory_relation(s, t, relation=relation, note=note, approver=approver)


@mcp_gov.tool()
def list_memory_relations(memory_id: int) -> dict:
    """列出某记忆的全部关系（双向，只读）。"""
    _ensure_db()
    if not isinstance(memory_id, int):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "memory_id must be an integer"}
    return life.list_memory_relations(memory_id)


@mcp_gov.tool()
def get_memory_lifecycle(memory_id: int) -> dict:
    """返回记忆生命周期状态及合法转移。"""
    _ensure_db()
    if not isinstance(memory_id, int):
        try:
            memory_id = int(memory_id)
        except (TypeError, ValueError):
            return {"ok": False, "message": "memory_id must be an integer"}
    with connect() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            return {"ok": False, "message": "memory not found"}
        d = dict(row)
        return {"ok": True, "memory_id": memory_id,
                "lifecycle_state": life.lifecycle_state_of(d),
                "allowed_transitions": life.lifecycle_transitions(d),
                "status": d.get("status"), "memory_status": d.get("memory_status")}


@mcp_gov.tool()
def consolidate_memory_candidates(persist: bool = False, limit: int = 50,
                                 category: str = "", memory_type: str = "",
                                 approver: str = "user") -> dict:
    """仅扫描生成巩固候选（不改动记忆）。"""
    _ensure_db()
    if not isinstance(limit, int):
        limit = 50
    return life.consolidate_memory_candidates(
        persist=persist, limit=limit, category=category, memory_type=memory_type,
        approver=approver)


@mcp_gov.tool()
def run_memory_consolidation(candidate_ids: list = None, dry_run: bool = False,
                            approver: str = "user", rule_version: str = "") -> dict:
    """逐候选独立事务应用已批准巩固候选；审计可逆。"""
    _ensure_db()
    ids = None
    if candidate_ids is not None:
        if not isinstance(candidate_ids, (list, tuple)):
            return {"ok": False, "message": "candidate_ids must be a list"}
        ids = [int(x) for x in candidate_ids]
    return life.run_memory_consolidation(candidate_ids=ids, dry_run=dry_run,
                                         approver=approver, rule_version=rule_version)


@mcp_gov.tool()
def get_memory_usage_stats(memory_id: int = 0) -> dict:
    """返回记忆使用反馈统计（单条或全活跃排名）。"""
    _ensure_db()
    return life.get_memory_usage_stats(memory_id=int(memory_id or 0))


# ----------------------------------------------------------------------
# Phase 7: activation spreading
# ----------------------------------------------------------------------
@mcp_gov.tool()
def spread_activation(memory_ids: list = None, decay: float = 0.5, max_depth: int = 2,
                      include_recent_events: bool = True, window_days: int = 30) -> dict:
    """沿关系图传播激活值（纯读、确定性、防环、衰减），供检索重排。"""
    _ensure_db()
    ids = None
    if memory_ids:
        ids = [int(x) for x in memory_ids]
    return spreading.compute_activation(
        memory_ids=ids, decay=float(decay), max_depth=int(max_depth),
        include_recent_events=bool(include_recent_events), window_days=int(window_days))


@mcp_gov.tool()
def record_memory_activation(memory_id: int, reason: str = "", approver: str = "user") -> dict:
    """仅写审计事件记录人工再激活（不改状态）。"""
    _ensure_db()
    return spreading.record_activation(int(memory_id), reason=reason, approver=approver)



def _run(server, transport):
    _ensure_db()
    server.run(transport=transport)


def _run_cli():
    """Console-script entry (registered in pyproject.toml as `bionic-agent`)."""
    _run(mcp, "stdio")


if __name__ == "__main__":
    # Default launch = core instance (21 legacy tools). Launch the governance
    # instance with: python -m app.mcp_server_gov
    _run_cli()