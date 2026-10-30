"""Bionic Agent 可视化管理面板（FastAPI + 内嵌静态前端）。

这是一个独立的管理服务，直连 `data/agent.db`，复用 `app.mcp_server` /
`app.governance_service` / `app.memory_lifecycle` 里现成的纯函数，因此与 MCP
工具写的是同一个数据库、行为完全一致（不会形成第二套数据）。

启动:  python -m app.admin_ui            (默认 http://127.0.0.1:8899)
       bionic-agent-admin               (pyproject 注册的控制台入口)
       python -m app.admin_ui --port 9000 --host 0.0.0.0

前端是一个零外部依赖的自包含 HTML（app/admin_ui.html），离线可用；
语义检索复用 bionic 已缓存的本地 FastEmbed 模型，不联网。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让脚本式启动（`python app/admin_ui.py`）也能 import app.*。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Query, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

# 复用既有后端（这些函数不依赖 MCP 协议，直接操作 SQLite）。
import app.mcp_server as core  # noqa: E402
from app import governance_service as gov  # noqa: E402
from app import memory_lifecycle as life  # noqa: E402
from app import embeddings as emb  # noqa: E402

HTML_PATH = Path(__file__).resolve().with_name("admin_ui.html")

app = FastAPI(title="Bionic Agent 管理面板", version="0.1.0")

# 允许本机页面跨端口访问。
#
# 面板有两种用法：直接在浏览器打开 8899，或者被 DeepSeek Harness 的 client 插件
# 用 iframe 嵌进去（宿主端口随机，如 59647）。后者是同机不同端口，属于跨源——
# 浏览器会拦掉不带 CORS 头的 fetch/XHR。这个服务只监听 127.0.0.1、只给本机用，
# 所以放行本机来源没有额外风险；限死 localhost/127.0.0.1 而不放开 *，是为了
# 万一将来改了监听地址也不至于变成任意网站都能读。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _ok(data):
    return JSONResponse(content={"ok": True, "data": data})


def _err(message: str, status: int = 400):
    return JSONResponse(content={"ok": False, "message": message}, status_code=status)


def _run(fn, *args, **kwargs):
    """调用后端函数，把 {"ok":False} 转成 404/400。"""
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # 后端内部异常也暴露给前端
        return _err(f"{type(exc).__name__}: {exc}", status=500)
    if isinstance(result, dict) and result.get("ok") is False:
        return _err(result.get("message", "操作失败"), status=404)
    return _ok(result)


# ---------------------------------------------------------------------------
# 记忆 (Memories)
# ---------------------------------------------------------------------------
@app.get("/api/memories")
def api_list_memories(
    q: str = Query("", description="关键词；为空列出最近"),
    category: str = Query(""),
    include_archived: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
):
    rows = core.search_memories(q, limit, category, include_archived)
    return _ok(rows)


@app.post("/api/memories")
def api_create_memory(payload: dict):
    content = (payload.get("content") or "").strip()
    if not content:
        return _err("content 不能为空")
    tags = payload.get("tags")
    if isinstance(tags, str):
        tags = [t for t in tags.split(",") if t.strip()]
    return _run(
        core.save_memory,
        content,
        payload.get("category", "general") or "general",
        payload.get("source", "admin-ui") or "admin-ui",
        float(payload.get("confidence", 0.8) or 0.8),
        tags,
    )


@app.get("/api/memories/{memory_id}")
def api_get_memory(memory_id: int, include_archived: bool = Query(True)):
    return _run(core.get_memory, memory_id, include_archived)


@app.put("/api/memories/{memory_id}")
def api_update_memory(memory_id: int, payload: dict):
    tags = payload.get("tags")
    if isinstance(tags, str):
        tags = [t for t in tags.split(",") if t.strip()]
    return _run(
        core.update_memory,
        memory_id,
        payload.get("content", "") or "",
        payload.get("category", "") or "",
        payload.get("source", "") or "",
        payload.get("confidence"),
        tags,
    )


@app.post("/api/memories/{memory_id}/archive")
def api_archive(memory_id: int):
    return _run(core.archive_memory, memory_id)


@app.post("/api/memories/{memory_id}/restore")
def api_restore(memory_id: int):
    return _run(core.restore_memory, memory_id)


@app.post("/api/memories/{memory_id}/protect")
def api_protect(memory_id: int, payload: dict):
    return _run(gov.set_protected, memory_id, bool(payload.get("protected", True)),
               payload.get("reason", "") or "")


@app.post("/api/memories/{memory_id}/review")
def api_set_review(memory_id: int, payload: dict):
    return _run(gov.set_review_status, memory_id, payload.get("reason", "") or "",
               payload.get("approver", "admin-ui"))


@app.get("/api/memories/{memory_id}/governance")
def api_gov_status(memory_id: int):
    return _run(gov.get_governance_status, memory_id)


@app.get("/api/memories/{memory_id}/details")
def api_details(memory_id: int):
    return _run(life.get_memory_details, memory_id)


@app.get("/api/memories/{memory_id}/related")
def api_related(memory_id: int, max_depth: int = Query(2, ge=1, le=3)):
    return _run(life.get_related_memories, memory_id, max_depth, "")


@app.get("/api/memories/{memory_id}/usage")
def api_usage(memory_id: int):
    return _run(life.get_memory_usage_stats, memory_id)


@app.post("/api/memories/{memory_id}/feedback")
def api_feedback(memory_id: int, payload: dict):
    return _run(life.record_memory_feedback, memory_id,
               payload.get("feedback_type", "helpful"), payload.get("note", "") or "")


@app.post("/api/memories/{memory_id}/governance-archive")
def api_gov_archive(memory_id: int, payload: dict):
    return _run(gov.archive_memory_governance, memory_id, payload.get("reason", "") or "")


@app.post("/api/memories/{memory_id}/governance-restore")
def api_gov_restore(memory_id: int, payload: dict):
    return _run(gov.restore_memory_governance, memory_id, payload.get("reason", "") or "")


@app.get("/api/memories/search/semantic")
def api_semantic(q: str = Query(""), limit: int = Query(10, ge=1, le=50),
                 category: str = Query(""), min_score: float = Query(-1.0)):
    if not q.strip():
        return _err("q 不能为空")
    result = core.semantic_search_memories(q, limit, category, min_score)
    # 模型未就绪时会降级成关键词检索（返回 dict），统一成 {results, degraded}
    if isinstance(result, dict) and ("degraded" in result or "error" in result):
        return _ok({"degraded": result.get("degraded", False),
                    "reason": result.get("reason") or result.get("error"),
                    "message": result.get("message"),
                    "results": result.get("results", [])})
    return _ok({"degraded": False, "results": result})


# ---------------------------------------------------------------------------
# 关系 (Links)
# ---------------------------------------------------------------------------
@app.post("/api/links")
def api_create_link(payload: dict):
    return _run(core.link_memories,
               int(payload.get("source_memory_id", 0)),
               int(payload.get("target_memory_id", 0)),
               payload.get("relation", "related") or "related",
               payload.get("note", "") or "")


@app.get("/api/memories/{memory_id}/links")
def api_links(memory_id: int):
    return _run(core.get_memory_links, memory_id)


# ---------------------------------------------------------------------------
# 任务 (Tasks)
# ---------------------------------------------------------------------------
@app.get("/api/tasks")
def api_tasks():
    return _ok(core.list_tasks())


@app.post("/api/tasks")
def api_create_task(payload: dict):
    title = (payload.get("title") or "").strip()
    goal = (payload.get("goal") or "").strip()
    if not title or not goal:
        return _err("title 与 goal 不能为空")
    return _run(core.create_task, title, goal)


@app.get("/api/tasks/{task_id}")
def api_get_task(task_id: int):
    return _run(core.get_task, task_id)


@app.post("/api/tasks/{task_id}/snapshot")
def api_snapshot(task_id: int, payload: dict):
    return _run(core.save_stage_snapshot, task_id, payload.get("summary", "") or "",
               payload.get("state_json", "{}") or "{}")


@app.post("/api/tasks/{task_id}/handoff")
def api_handoff(task_id: int, payload: dict):
    return _run(core.handoff_task, task_id, payload.get("summary", "") or "",
               payload.get("state_json", "{}") or "{}")


@app.post("/api/tasks/{task_id}/complete")
def api_complete(task_id: int, payload: dict):
    return _run(core.complete_task, task_id, payload.get("result", "") or "")


@app.post("/api/tasks/{task_id}/resume")
def api_resume(task_id: int):
    return _run(core.resume_task, task_id)


# ---------------------------------------------------------------------------
# 治理 (Governance)
# ---------------------------------------------------------------------------
@app.get("/api/embedding_status")
def api_embedding_status():
    return _ok(core.embedding_status())


@app.post("/api/embedding/warmup")
def api_warmup():
    emb.ensure_warmup(emb.DEFAULT_MODEL)
    return _ok(emb.model_state())


@app.get("/api/governance/score")
def api_score(category: str = Query(""), limit: int = Query(50, ge=1, le=200)):
    # 直接调服务函数并自己包一层，而不是先包成 JSONResponse 再反序列化回来改。
    # score_memories 只返回评分列、不含 category，界面要显示分类所以这里补齐。
    try:
        result = gov.score_memories(None, limit, category, True)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}", status=500)
    if not isinstance(result, dict) or result.get("ok") is False:
        return _err(result.get("message", "评分失败") if isinstance(result, dict) else "评分失败",
                    status=404)
    rows = result.get("results") or []
    ids = [r.get("memory_id") for r in rows if isinstance(r, dict) and r.get("memory_id")]
    if ids:
        from app.db import connect
        placeholders = ",".join("?" * len(ids))
        with connect() as conn:
            cats = {row[0]: (row[1] or "") for row in conn.execute(
                f"SELECT id, category FROM memories WHERE id IN ({placeholders})", ids)}
        for r in rows:
            if isinstance(r, dict) and not r.get("category"):
                r["category"] = cats.get(r.get("memory_id"), "")
    return _ok(result)


@app.post("/api/governance/detect")
def api_detect(payload: dict):
    # 注意：detect_conflicts(memory_ids, category, candidate_count, persist)
    # 第三个位置参数是 candidate_count（int），不能传 "" 占位，否则 int("") 抛错。
    return _run(gov.detect_conflicts, None, payload.get("category", "") or "",
                payload.get("candidate_count", 8), True)


@app.get("/api/governance/conflicts")
def api_conflicts(resolution: str = Query(""), memory_id: int = Query(0),
                  limit: int = Query(50, ge=1, le=200)):
    # list_conflicts(memory_id, resolution, limit) —— 这里第三个参数是 limit(int)。
    return _run(gov.list_conflicts, memory_id or None, resolution, limit)


@app.post("/api/governance/conflicts/{conflict_id}/review")
def api_review_conflict(conflict_id: int, payload: dict):
    return _run(gov.review_conflict, conflict_id, payload.get("resolution", "not_conflict"),
               payload.get("note", "") or "")


@app.get("/api/governance/maintenance")
def api_maintenance_preview(mode: str = Query("dry_run"),
                            category: str = Query(""),
                            limit: int = Query(50, ge=1, le=200)):
    return _run(core.run_memory_maintenance, mode, limit, category)


@app.post("/api/governance/maintenance/apply")
def api_maintenance_apply(payload: dict):
    return _run(core.run_memory_maintenance, "apply_approved",
               int(payload.get("limit", 50)), payload.get("category", "") or "")


@app.get("/api/governance/consolidation")
def api_consolidation(status: str = Query(""), limit: int = Query(50, ge=1, le=200)):
    return _run(life.list_consolidation_candidates, status, limit)


@app.get("/api/governance/duplicates")
def api_duplicates(category: str = Query(""), candidate_count: int = Query(8, ge=1, le=50)):
    # find_duplicate_memories(memory_ids, category, candidate_count)
    return _run(life.find_duplicate_memories, None, category, candidate_count)


# ---------------------------------------------------------------------------
# 统计 + 首页
# ---------------------------------------------------------------------------
@app.get("/api/stats")
def api_stats():
    from app.db import connect
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM memories WHERE status='archived'").fetchone()[0]
        tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        links = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return _ok({"memories_total": total, "memories_active": active,
                "memories_archived": archived, "tasks": tasks,
                "links": links, "events": events})


@app.get("/api/ping")
def api_ping(request: Request):
    """被 iframe 页面本身调用，用来证明"面板真的在浏览器里跑起来了"。

    为什么要这个接口：宿主半的 data/panel-boot.log 只能证明 Node 侧加载了插件，
    证明不了浏览器侧真的求值了 client.js、真的挂上了 iframe。两边都加载成功但
    中间某处静默失败时，只靠宿主痕迹会误判成"已经好了"。这里落一行
    data/panel-client.log，重启后一眼就能分清故障在哪一半。
    """
    from datetime import datetime, timezone
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "referer": request.headers.get("referer", ""),
        "origin": request.headers.get("origin", ""),
        "user_agent": request.headers.get("user-agent", "")[:120],
    }
    try:
        log_path = Path(__file__).resolve().parent.parent / "data" / "panel-client.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 —— 记账失败绝不能影响面板本身
        pass
    return _ok({"pong": True, "at": record["at"]})


@app.get("/")
def index():
    if HTML_PATH.exists():
        return FileResponse(HTML_PATH, media_type="text/html")
    return _err("找不到 admin_ui.html（请确认它和 admin_ui.py 在同一目录）", status=500)


def _run_cli():
    parser = argparse.ArgumentParser(description="Bionic Agent 管理面板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    _run_cli()
