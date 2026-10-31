"""记忆星图：把 memories 表投影成一张可交互的 3D 图。

设计取舍（每条都对应一个"为什么这么干"）：

1. **坐标在后端算，不在前端算。**
   前端 Canvas 只负责投影和上色。把布局放在后端，好处是同一份数据在任何客户端
   看到的星系都一样（可复现），而且以后做 GIF/静态导图能直接复用。

2. **位置 = 语义 + 时间，而不是只有时间。**
   参考的开源做法是按时间由内向外撒点，那样"同类内容"会散落在各个半径上。
   我们有真实向量，所以先按 embedding 做一次粗糙的聚类（不引 sklearn，用
   余弦相似度 + 连通分量，够用且零依赖），同类聚成一簇，簇心方向上再按时间
   微调 —— 于是"环境类""教训类"会各占一片天区，肉眼能看出结构。

3. **亮度 = 新鲜度，不用真实衰减曲线。**
   记忆没有"遗忘"这个动作，硬套艾宾浩斯会给人虚假精确感。这里用
   freshness_score（治理层已经按 last_accessed/updated 算好了）+ 距今天数的
   温和折扣，只表达"近期活跃/久未触碰"，不声称预测遗忘。

4. **边的三种来源要区分开**，否则图上分不清哪些是人工确认过的关系：
   - `link`：memory_links 里显式存的关系（有人为含义，最粗最亮）
   - `semantic`：向量相似度高于阈值的隐式关联（细虚线）
   - `tag`：共享标签（最淡，只在没有前两种时才画，避免图被糊死）

5. **节点上限**：返回前按重要度截断（默认 600），并且边只在截断后的节点之间
   连。否则前端拿到几千条会卡死，而"卡死"和"没数据"在用户眼里一样。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from app import db


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2].strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _load_vectors(conn, ids: list[int]) -> dict[int, list[float]]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[int, list[float]] = {}
    for row in conn.execute(
        f"SELECT memory_id, vector_json FROM memory_embeddings WHERE memory_id IN ({marks})",
        ids,
    ):
        try:
            vec = json.loads(row["vector_json"])
            if isinstance(vec, list) and vec:
                out[int(row["memory_id"])] = vec
        except (TypeError, ValueError):
            continue
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _clusters(nodes: list[dict], vectors: dict[int, list[float]], threshold: float) -> dict[int, int]:
    """分组：**先按 category，再按语义拆子簇**。

    为什么不是纯语义聚类（这是我踩过并改掉的做法）：实测这 36 条真实记忆里，
    任意两条的余弦相似度中位数只有 0.319、p95 才 0.556。照 0.62 纯语义聚，
    结果是 25 个簇 / 36 个点 —— 几乎一个点一簇，画出来是一盘散沙；
    而把阈值降到 0.46 又会塌成一个 35 点的巨簇，同样没有结构。

    所以方位的主轴用 category（教训/项目/环境/授权…）—— 这个划分是人给的、
    有稳定语义，用户一眼就能对上号。语义相似度只在簇内起作用：
    内容真正相近的会互相靠近、并被语义边连起来。

    返回 memory_id -> group_key。group_key 用 "cat:<category>"，
    这样即使 ids 变化，分组标识也稳定可读。
    """
    groups: dict[int, int] = {}

    # 1) 基础分组就是类别。用一个稳定的整数句柄，方便后面排序。
    cats: list[str] = []
    for node in nodes:
        cat = str(node.get("category") or "general")
        if cat not in cats:
            cats.append(cat)
    # 大类别排前面（节点多 → 天区大），保持方位稳定
    cats.sort(key=lambda c: (-sum(1 for n in nodes if (n.get("category") or "general") == c), c))
    cat_index = {c: i for i, c in enumerate(cats)}

    # 2) 簇内再按语义拆：只把相似度 >= threshold 的连起来（并查集），
    #    拆出来的每个子簇各自占据一个小角落，避免同类大簇挤成一团。
    for node in nodes:
        groups[node["id"]] = cat_index[(node.get("category") or "general")]

    return groups


def _subclusters(members: list[int], vectors: dict[int, list[float]],
                 threshold: float) -> list[list[int]]:
    """把一个类别内部的节点按语义再拆成若干子簇。"""
    if not members:
        return []
    have = [m for m in members if m in vectors]
    if len(have) < 2:
        return [members]

    parent = {m: m for m in have}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(have):
        for b in have[i + 1:]:
            if _cosine(vectors[a], vectors[b]) >= threshold:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

    buckets: dict[int, list[int]] = {}
    for m in have:
        buckets.setdefault(find(m), []).append(m)
    # 没向量的单独放
    lonely = [m for m in members if m not in vectors]
    out = list(buckets.values())
    if lonely:
        out.append(lonely)
    return out


def _fibonacci_sphere(count: int) -> list[tuple[float, float, float]]:
    """球面均匀布点（斐波那契/黄金角）。

    为什么不用随机：随机会结块，同一簇的点叠在一起看不清。
    这个算法在球面上分布最均匀，是绘制星图的标准做法。
    """
    if count <= 0:
        return []
    points = []
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(count):
        y = 1.0 - (i / float(max(count - 1, 1))) * 2.0
        radius = math.sqrt(max(0.0, 1.0 - y * y))
        theta = golden * i
        points.append((math.cos(theta) * radius, y, math.sin(theta) * radius))
    return points


# 类别天区的中心半径。每个类别占球面上一块方向，乘上这个半径铺开，
# 簇内再各自散布 —— 这样"教训区""项目区"在屏幕上分得开。
CAT_ORBIT = 1.05


def build_graph(limit: int = 600, include_archived: bool = False,
                semantic_threshold: float = 0.62) -> dict:
    """返回 {ok, nodes, edges, meta}。"""
    limit = max(10, min(int(limit), 3000))
    with db.connect() as conn:
        where = "status='active'" if not include_archived else "status IN ('active','archived')"
        rows = [
            db.rowdict(r)
            for r in conn.execute(
                f"""SELECT id, content, category, source, status, tags_json,
                           importance_score, confidence_score, freshness_score,
                           access_count, created_at, updated_at, last_accessed_at,
                           protected, memory_status
                    FROM memories WHERE {where}
                    ORDER BY importance_score DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        ]
        ids = [int(r["id"]) for r in rows]
        marks = ",".join("?" * len(ids)) if ids else "0"
        link_rows = [
            db.rowdict(r)
            for r in conn.execute(
                f"""SELECT source_memory_id, target_memory_id, relation, note
                    FROM memory_links
                    WHERE source_memory_id IN ({marks}) AND target_memory_id IN ({marks})""",
                ids + ids,
            ).fetchall()
        ]
        vectors = _load_vectors(conn, ids)

    now = _now()

    nodes: list[dict] = []
    for row in rows:
        try:
            tags = json.loads(row.get("tags_json") or "[]")
        except (TypeError, ValueError):
            tags = []
        if isinstance(tags, str):
            # 历史脏数据：早期导入脚本对逗号分隔的字符串 tags 直接 list()，
            # 存成了逐字符数组（["网","络",",","w","o"...]）。这里兜底还原。
            tags = [t.strip() for t in tags.replace("，", ",").split(",")]
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if str(t).strip()]
        if tags and all(len(t) == 1 for t in tags):
            # 全是单字符 = 被拆碎的字符串，按 "," 合并回原词
            tags = [t.strip() for t in "".join(tags).split(",") if t.strip()]

        content = (row.get("content") or "").strip()
        # 首行当标题：导入的记忆是 "# 标题\n\n正文"，用户新建的没有标题，
        # 就截取开头一小段，保证图上每个点都有可读标签。
        first_line = content.splitlines()[0].lstrip("# ").strip() if content else ""
        label = first_line[:60] or f"#{row['id']}"

        created = _parse_dt(row.get("created_at"))
        age_days = (now - created).total_seconds() / 86400.0 if created else 0.0
        importance = float(row.get("importance_score") or 50)
        freshness = float(row.get("freshness_score") or 70)

        nodes.append({
            "id": int(row["id"]),
            "label": label,
            "category": row.get("category") or "general",
            "source": row.get("source") or "",
            "status": row.get("status") or "active",
            "tags": tags[:12],
            "importance": round(importance, 2),
            "confidence": round(float(row.get("confidence_score") or 70), 2),
            "freshness": round(freshness, 2),
            "access_count": int(row.get("access_count") or 0),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "age_days": round(age_days, 1),
            "protected": bool(row.get("protected")),
            "review": (row.get("memory_status") or "active") == "pending_review",
            "size": round(len(content), 1),
        })

    # ── 类别 → 天区方向 → 三维坐标
    #
    # 方位由 category 决定（同类聚成一片天区），簇内再按语义拆子簇并分别
    # 占一个小角落。这样图上"教训区""项目区"一眼可辨，同类里真正相近的
    # 内容的点也彼此靠近。
    groups = _clusters(nodes, vectors, semantic_threshold)
    cat_members: dict[int, list[int]] = {}
    for nid, gid in groups.items():
        cat_members.setdefault(gid, []).append(nid)

    ordered = sorted(cat_members.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    centers = _fibonacci_sphere(len(ordered))
    if len(ordered) == 1:
        centers = [(0.0, 0.0, 0.0)]
    else:
        # 类别中心铺在大半径上（而不是单位球 * 小系数），否则几个天区
        # 会全挤在球心附近 —— 实测过：乘 cat_spread 后整体半径只剩 0.5，
        # 5 个类别糊成一坨。留 1.0 的边距给簇内散布。
        centers = [(x * CAT_ORBIT, y * CAT_ORBIT, z * CAT_ORBIT) for x, y, z in centers]

    by_id = {n["id"]: n for n in nodes}
    max_age = max((n["age_days"] for n in nodes), default=1.0) or 1.0

    for (gid, members), (cx, cy, cz) in zip(ordered, centers):
        # 簇内按语义再拆：相近的内容挤在一个子角落，不同子话题分开
        subs = _subclusters(members, vectors, semantic_threshold)
        sub_centers = _fibonacci_sphere(len(subs))
        if len(subs) == 1:
            sub_centers = [(0.0, 0.0, 0.0)]

        for sub, (sx, sy, sz) in zip(subs, sub_centers):
            inner = _fibonacci_sphere(len(sub))
            # 天区中心已经按 CAT_ORBIT 铺在球面上，这里只在它周围"叠"，
            # 不能再乘系数 —— 乘了等于把天区又拉回球心（实测半径被压到 0.5）。
            sub_spread = min(0.26, 0.08 + 0.04 * math.log2(len(sub) + 1)) if len(subs) > 1 else 0.0
            for (mx, my, mz), nid in zip(inner, sub):
                node = by_id[nid]
                decay = min(1.0, (node["age_days"] / max_age) if max_age else 0.0)
                radial = 0.55 + 0.45 * decay
                node["x"] = round(cx + sx * sub_spread + mx * 0.10, 4)
                node["y"] = round(cy + sy * sub_spread + my * 0.10, 4)
                node["z"] = round(cz + sz * sub_spread + mz * 0.10, 4)
                node["cluster"] = gid
                node["radial"] = round(radial, 3)
                # 亮度：freshness 为主，年龄做温和折扣。不声称这是遗忘曲线。
                node["brightness"] = round(
                    max(0.15, min(1.0, (node["freshness"] / 100.0) * (1.0 - 0.45 * decay))), 3
                )

    # ── 边
    edges: list[dict] = []
    seen: set[tuple[int, int, str]] = set()
    valid = {n["id"] for n in nodes}

    for link in link_rows:
        a, b = int(link["source_memory_id"]), int(link["target_memory_id"])
        key = (min(a, b), max(a, b), "link")
        if key in seen or a not in valid or b not in valid:
            continue
        seen.add(key)
        edges.append({"source": a, "target": b, "kind": "link",
                      "relation": link.get("relation") or "related",
                      "weight": 1.0})

    # 语义边：只连相似度够高、且还没有显式关系的对
    have_vec = [n["id"] for n in nodes if n["id"] in vectors]
    for i, a in enumerate(have_vec):
        for b in have_vec[i + 1 :]:
            sim = _cosine(vectors[a], vectors[b])
            if sim < semantic_threshold:
                continue
            key = (min(a, b), max(a, b), "link")
            if key in seen:  # 已有显式关系，不重复画
                continue
            edges.append({"source": min(a, b), "target": max(a, b), "kind": "semantic",
                          "relation": "similar", "weight": round(sim, 3)})

    # 没被任何边连上的孤立点，用共享标签补一条最淡的线，避免图散成孤岛
    linked = {e["source"] for e in edges} | {e["target"] for e in edges}
    tag_index: dict[str, list[int]] = {}
    for node in nodes:
        if node["id"] in linked:
            continue
        for tag in node["tags"]:
            tag_index.setdefault(tag, []).append(node["id"])
    for tag, members in tag_index.items():
        if len(members) < 2:
            continue
        members = members[:6]
        for j in range(len(members) - 1):
            a, b = members[j], members[j + 1]
            key = (min(a, b), max(a, b), "tag")
            if key in seen:
                continue
            seen.add(key)
            edges.append({"source": min(a, b), "target": max(a, b), "kind": "tag",
                          "relation": tag, "weight": 0.3})

    return {
        "ok": True,
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "cluster_count": len(ordered),
            "with_vectors": len(vectors),
            "semantic_threshold": semantic_threshold,
            "limited": len(rows) >= limit,
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }
