#!/usr/bin/env node
/**
 * 星图自检：静态检查前端渲染代码 + 实际调用后端 build_graph 验证数据结构。
 *
 * 为什么要这两层：
 *   - 前端是手写 Canvas，没有构建步骤也没有类型系统，改坏了不会有任何报错，
 *     只会静默画不出来。这里把几条踩过的坑钉死成断言。
 *   - 后端布局算法是纯 Python，可以直接跑，比截图像素比对稳得多。
 *
 * 钉住的历史 bug：
 *   1. 固定比例缩放（min(w,h) * 常数）会让旋转后的边缘节点跑出画布
 *      —— 教训天区 15 个点被裁到只剩零星几颗。现在必须按实际占位自适应。
 *   2. drawStar 与 pickStar 各算一遍投影，必然对不齐（点得到的位置和看到的
 *      位置不同）。现在两边共用 projectNodes。
 *   3. 画布颜色写死深色 → 跟随 DSH 浅色主题时看不清。必须从 CSS 变量读。
 *   4. 后端 tags 读取对「逗号字符串」直接 list()，会存成逐字符数组
 *      （["网","络",",","w" ...]），让 tag 边全部失效。
 */
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const PROJECT_DIR = dirname(dirname(TEST_DIR));

let failed = 0;
let passed = 0;

function check(name, ok, detail = "") {
	if (ok) {
		passed++;
		console.log(`[PASS] ${name}${detail ? ` — ${detail}` : ""}`);
	} else {
		failed++;
		console.log(`[FAIL] ${name}${detail ? ` — ${detail}` : ""}`);
	}
}

// ── 前端静态断言 ────────────────────────────────────────────────────────────
const htmlPath = join(PROJECT_DIR, "app", "admin_ui.html");
if (!existsSync(htmlPath)) {
	console.log(`[SKIP] 找不到 ${htmlPath}`);
	process.exit(0);
}
const html = readFileSync(htmlPath, "utf8");

check(
	"星图 tab 存在",
	/data-tab="starmap"/.test(html) && /id="starmap"/.test(html),
);
check("画布元素存在", /id="starCanvas"/.test(html));
check(
	"投影函数 drawStar / pickStar 共用 projectNodes（避免点选对不齐）",
	/function projectNodes\(/.test(html) &&
		/drawStar[\s\S]{0,3000}?projectNodes\(/.test(html) &&
		/pickStar[\s\S]{0,2000}?projectNodes\(/.test(html),
);
check(
	"按实际占位自适应缩放（不再用固定比例，防止旋转后裁切）",
	/maxUx/.test(html) && /maxUy/.test(html) && /const fit = Math\.min/.test(html),
);
check(
	"画布颜色取自 CSS 变量（跟随主题，不写死）",
	/getPropertyValue\('--txt'\)/.test(html) &&
		!/ctx\.strokeStyle\s*=\s*['"]#[0-9a-fA-F]{3,8}['"]/.test(html),
);
check(
	"主题切换后重画（MutationObserver 盯 data-theme）",
	/MutationObserver\([\s\S]{0,80}drawStar/.test(html),
);
check(
	"点节点弹出信息卡并可跳回记忆列表",
	/id="starInfo"/.test(html) && /starOpenFull/.test(html) && /scrollIntoView/.test(html),
);
check(
	"天区颜色按 category 分配（不是按数组下标）",
	/STAR_COLORS\s*=\s*\{/.test(html) && /starColor\(n\.category\)/.test(html),
);

// ── 后端实测 ────────────────────────────────────────────────────────────────
let py = process.env.PYTHON;
if (!py) {
	for (const cand of [
		join(PROJECT_DIR, ".venv", "Scripts", "python.exe"),
		join(PROJECT_DIR, ".venv", "bin", "python"),
		"D:/Software/Dev/Miniforge/python.exe",
	]) {
		if (existsSync(cand)) {
			py = cand;
			break;
		}
	}
}

if (!py) {
	console.log("[SKIP] 找不到 python，跳过后端实测");
} else {
	const probe = `
import json, sys
from app import star_map
g = star_map.build_graph()
nodes, edges, meta = g["nodes"], g["edges"], g["meta"]
cats = sorted({n["category"] for n in nodes})
out = {
  "ok": g.get("ok", False),
  "node_count": len(nodes),
  "edge_count": len(edges),
  "meta": meta,
  "cats": cats,
  "cluster_count": len({n["cluster"] for n in nodes}),
  "kinds": sorted({e["kind"] for e in edges}),
  "max_radius": max((n["x"]**2 + n["y"]**2 + n["z"]**2) ** 0.5 for n in nodes) if nodes else 0,
  "bad_tags": sum(1 for n in nodes if n["tags"] and all(len(str(t)) == 1 for t in n["tags"])),
  "keys": sorted(nodes[0].keys()) if nodes else [],
}
print(json.dumps(out, ensure_ascii=False))
`;
	const r = spawnSync(py, ["-c", probe], {
		cwd: PROJECT_DIR,
		encoding: "utf8",
		env: { ...process.env, PYTHONIOENCODING: "utf-8" },
		timeout: 120000,
	});
	if (r.status !== 0) {
		check("后端 build_graph 可执行", false, (r.stderr || "").trim().split("\n").pop());
	} else {
		let data;
		try {
			data = JSON.parse(r.stdout.trim().split("\n").pop());
		} catch (e) {
			check("后端返回 JSON", false, e.message);
			data = null;
		}
		if (data) {
			check("后端 build_graph 返回 ok", data.ok === true);
			check(
				"每个节点都有渲染必需字段",
				["x", "y", "z", "brightness", "cluster", "category", "label"].every((k) =>
					data.keys.includes(k),
				),
				`${data.keys.length} 个字段`,
			);
			check(
				"天区数 = 类别数（类别作为方位主轴）",
				data.cluster_count === data.cats.length,
				`${data.cluster_count} 个天区 / ${data.cats.length} 个类别`,
			);
			check(
				"坐标半径在合理范围（不会全部缩在球心）",
				data.max_radius > 0.8,
				`最大半径 ${data.max_radius.toFixed(2)}`,
			);
			check(
				"tags 没有被拆成单字符（历史 bug 防回归）",
				data.bad_tags === 0,
				`${data.bad_tags} 个节点的 tags 异常`,
			);
		}
	}
}

console.log(`\n=== 星图：${passed} 通过 / ${failed} 失败 ===`);
process.exit(failed > 0 ? 1 : 0);
