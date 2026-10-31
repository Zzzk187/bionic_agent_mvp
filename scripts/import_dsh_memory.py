"""把 dsh-memory/记忆.jsonl 的 34 条真实记忆导入 bionic-agent MCP 的 SQLite 库。

设计要点（都对应一个具体的坑）：
1. **幂等**：源 id（A01/A02/...）写进 tags 作为 `src:A01`，重复跑只更新不新增。
   之所以不靠 content 判重——正文可能被后续编辑，靠正文比对会重复导入。
2. **不丢原文**：dsh-memory 的 body 是 markdown（含表格），标题单独存在 title。
   这里把 `# 标题` + 正文拼成 content，保证星图和检索里读到的是完整信息。
3. **保留元数据**：kind→category、trust→confidence、date→created_at、
   uses→access_count，全部映射过去，否则星图的时间/权重维度就没了依据。
4. **不自动跑向量**：嵌入模型此刻 model_ready=false，导入只写 memories 表；
   向量由 index_memories 之后补（它会按 content_hash 增量算）。
5. 走 SQLite 直连而不是 save_memory MCP 函数——同一进程里没有 MCP client，
   且这里需要控制 created_at，函数签名给不了。
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

SRC = Path(r"C:/Users/xing/.dsh/dsh-memory/记忆.jsonl")
DB = Path(r"D:/Cache/Cache_Project/bionic-agent-mcp/data/agent.db")
SOURCE_TAG = "dsh-memory"  # 用来识别"这条是从 dsh-memory 导进来的"


def load_rows() -> list[dict]:
    rows = []
    with io.open(SRC, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def build_content(row: dict) -> str:
    """标题 + 正文。标题进 content 是因为星图节点要显示可读名字。"""
    title = (row.get("title") or "").strip()
    body = (row.get("body") or "").strip()
    if title and body:
        return f"# {title}\n\n{body}"
    return title or body


def main() -> int:
    if not SRC.is_file():
        print(f"[FAIL] 源文件不存在：{SRC}")
        return 1
    if not DB.is_file():
        print(f"[FAIL] 目标库不存在：{DB}")
        return 1

    rows = load_rows()
    print(f"源记忆 {len(rows)} 条：{SRC}")

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    # 注意：计数器叫 added/updated_count/skipped，不要复用 row 里的 updated 时间戳，
    # 否则字符串和整数混在同一个变量里，第二次运行会 TypeError（踩过）。
    added = updated_count = skipped = 0

    try:
        for row in rows:
            src_id = str(row.get("id") or "").strip()
            if not src_id:
                skipped += 1
                continue

            content = build_content(row)
            if not content:
                skipped += 1
                continue

            # tags 里固定塞一个 src:<id>，作为幂等锚点
            #
            # 注意：源数据里 tags 有两种形态 —— 28 条是列表，6 条是
            # "powerShell,编码,BOM" 这样的逗号分隔字符串。对字符串直接
            # list() 会逐字符拆开（变成 ["P","o","w","e","r"...]），
            # 曾在库里留下 tags 长达 17 个单字符的脏数据。所以这里分清类型。
            raw_tags = row.get("tags")
            if isinstance(raw_tags, str):
                tags = [t.strip() for t in raw_tags.replace("，", ",").split(",")]
                tags = [t for t in tags if t]
            elif isinstance(raw_tags, list):
                tags = [str(t).strip() for t in raw_tags if str(t).strip()]
            else:
                tags = []
            anchor = f"src:{src_id}"
            if SOURCE_TAG not in tags:
                tags.append(SOURCE_TAG)
            tags = [t for t in tags if t != anchor] + [anchor]

            category = str(row.get("kind") or "general").strip() or "general"
            # trust: user→0.95（用户亲口），verified→0.8
            trust = str(row.get("trust") or "").strip()
            confidence = 0.95 if trust == "user" else 0.8
            created = str(row.get("date") or "").strip() or None
            updated_at_val = str(row.get("updated") or "").strip() or created
            uses = int(row.get("uses") or 0)
            last_used = str(row.get("last_used") or "").strip() or None
            loc = str(row.get("loc") or "").strip()

            # 幂等查找：靠 tags_json 里的 src:<id>
            existing = conn.execute(
                "SELECT id FROM memories WHERE tags_json LIKE ?", (f'%"{anchor}"%',)
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE memories SET content=?, category=?, source=?, confidence=?,
                           tags_json=?, updated_at=?, access_count=?, last_accessed_at=?
                       WHERE id=?""",
                    (content[:10000], category[:100], f"dsh-memory:{loc}"[:200], confidence,
                     json.dumps(tags, ensure_ascii=False), updated_at_val, uses, last_used,
                     existing["id"]),
                )
                updated_count += 1
            else:
                cur = conn.execute(
                    """INSERT INTO memories(content,category,source,confidence,tags_json,
                           created_at,updated_at,access_count,last_accessed_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (content[:10000], category[:100], f"dsh-memory:{loc}"[:200], confidence,
                     json.dumps(tags, ensure_ascii=False),
                     created or "2026-10-17 00:00:00",
                     updated_at_val or created or "2026-10-17 00:00:00",
                     uses, last_used),
                )
                conn.execute(
                    "INSERT INTO events(event_type,detail) VALUES(?,?)",
                    ("memory.created",
                     f"memory_id={cur.lastrowid}; category={category}; source=dsh-memory; src_id={src_id}"),
                )
                added += 1

        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
    finally:
        conn.close()

    print(f"新增 {added} / 更新 {updated_count} / 跳过 {skipped}")
    print(f"库内合计 memories={total}，其中 active={active}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
