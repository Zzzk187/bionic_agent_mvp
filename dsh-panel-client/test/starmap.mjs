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
check("3D 渲染容器存在（不再是自己手画 canvas）", /id="starCanvasWrap"/.test(html));
check(
	"用现成 3D 图库而不是手写投影",
	/ForceGraph3D\(\)\(host\)/.test(html) && /3d-force-graph/.test(html),
);
check(
	"库文件本地托管（不依赖 CDN，离线可用）",
	/\/static\/3d-force-graph\.min\.js/.test(html) && !/unpkg\.com|cdn\.jsdelivr/.test(html),
);
check(
	"手写投影代码已彻底移除（防止两套渲染并存）",
	!/function projectNodes\(/.test(html) && !/function drawStar\(/.test(html) && !/function pickStar\(/.test(html),
);
check(
	"天区颜色按 category 分配（不是按数组下标）",
	/STAR_COLORS\s*=\s*\{/.test(html) && /starColor\(n\.category\)/.test(html),
);
check(
	"节点位置钉死（fx/fy/fz），不让力模拟把天区揉散",
	/fx:\s*n\.x\s*\*\s*SCALE|fx:\s*raw\[i\]\.x/.test(html) && /fy:/.test(html) && /fz:/.test(html),
);
check(
	"坐标先减质心再摆位（否则相机对原点取景会裁掉边缘节点）",
	/raw\.reduce\(/.test(html) && /-\s*c\.x/.test(html),
);
check(
	"取景前等两帧（cameraPosition/zoomToFit 是异步生效的）",
	/settle\s*=\s*\(\)\s*=>\s*new Promise\(r\s*=>\s*requestAnimationFrame/.test(html),
);
check(
	"自适应取景：按屏幕包围盒反推相机距离",
	/const target = 0\.\d+/.test(html) && /zoomToFit/.test(html),
);
check(
	"3D 场景颜色取自 CSS 变量（跟随主题，不写死）",
	/getPropertyValue/.test(html) && /bg:\s*get\('--bg'/.test(html) && /backgroundColor\(pal\.bg\)/.test(html),
);
check(
	"主题切换只换颜色不重建（保留用户调好的视角）",
	/MutationObserver[\s\S]{0,400}?backgroundColor\(pal\.bg\)/.test(html),
);
check(
	"点星弹信息卡并可跳回记忆列表",
	/id="starInfo"/.test(html) && /starOpenFull/.test(html) && /scrollIntoView/.test(html),
);
check(
	"天区名称以覆盖层标签显示",
	/star-cat-label/.test(html) && /graph2ScreenCoords/.test(html),
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
