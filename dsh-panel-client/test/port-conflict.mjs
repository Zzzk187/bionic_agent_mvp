// 端口被别的程序占着时，插件必须换端口 —— 而不是把别人的服务当成自己的。
//
// 这是「只探测端口有没有人听」埋下的坑：8899 是常见端口，可能被用户别的服务、
// 上一次没退干净的进程、甚至一个静态文件服务器占着。复用它，面板打开后就是
// 别人的页面或一片乱码，而且不报任何错。
//
// 用例做法：先起一个**假服务**占住 8891（只回 HTML，不是 /api/stats 的形状），
// 再让插件以 adminUrl=8891 / autoStart=true 加载，然后断言它换到了 8892。
process.env.BIONIC_PANEL_NO_TRACE = "1";

import http from "node:http";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const PROJECT_DIR = dirname(dirname(TEST_DIR));
const BUSY = 8891;
const EXPECT = 8892;

let pass = 0;
let fail = 0;
const check = (name, ok, detail = "") => {
	if (ok) {
		pass += 1;
		console.log(`[PASS] ${name}${detail ? ` — ${detail}` : ""}`);
	} else {
		fail += 1;
		console.log(`[FAIL] ${name}${detail ? ` — ${detail}` : ""}`);
	}
};

const PY = existsSync("D:/Software/Dev/Miniforge/python.exe")
	? "D:/Software/Dev/Miniforge/python.exe"
	: "python";

function get(port, pathname) {
	return new Promise((done) => {
		const req = http.get({ host: "127.0.0.1", port, path: pathname, timeout: 2500 }, (res) => {
			const chunks = [];
			res.on("data", (c) => chunks.push(c));
			res.on("end", () => done({ status: res.statusCode, body: Buffer.concat(chunks).toString() }));
		});
		req.on("timeout", () => {
			req.destroy();
			done({ status: 0, body: "timeout" });
		});
		req.on("error", (e) => done({ status: 0, body: String(e?.message ?? e) }));
	});
}

// ---- 1) 先占住 8891，且故意回一个「不是管理服务」的响应 ----
const squatter = http.createServer((req, res) => {
	res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
	res.end("<html><body>someone else's server</body></html>");
});
await new Promise((done) => squatter.listen(BUSY, "127.0.0.1", done));
const squat = await get(BUSY, "/api/stats");
check("占位服务已就位且形状不对", squat.status === 200 && !squat.body.includes('"ok":true'), `status=${squat.status}`);

// ---- 2) 以被占用的端口加载插件 ----
const { apply } = await import("../lib/index.js");
const logs = [];
const tmpDbDir = mkdtempSync(join(tmpdir(), "bionic-port-test-"));
// launchAdmin 会把 process.env 传给子进程，所以在这里设就能把测试写入隔离掉。
process.env.BIONIC_DB_PATH = join(tmpDbDir, "agent.db");

// 只做最小 ctx：没有 webServer（mountPath 关掉，不挂反代），只验端口冲突处理。
const ctx = {
	logger: {
		info: (...a) => logs.push(a.join(" ")),
		warn: (...a) => logs.push(a.join(" ")),
		error: (...a) => logs.push(a.join(" "))
	},
	get: () => undefined,
	effect(fn) {
		return fn();
	}
};

const dispose = apply(ctx, {
	adminUrl: `http://127.0.0.1:${BUSY}`,
	autoStart: true,
	mountPath: "",
	projectDir: PROJECT_DIR,
	startTimeoutMs: 40000,
	// 日志落临时目录，别把测试端口的启动记录写进真实的 data/admin_ui.log
	logDir: tmpDbDir,
	// 用 PY 解析出的解释器；BIONIC_DB_PATH 已在上方设好，测试不会写用户真实库
	pythonPath: PY
});

// 等它换端口并起服务（最多 45s）
const deadline = Date.now() + 45000;
let ready = null;
while (Date.now() < deadline) {
	const stats = await get(EXPECT, "/api/stats");
	if (stats.status === 200 && stats.body.includes('"ok":true')) {
		ready = stats;
		break;
	}
	await new Promise((r) => setTimeout(r, 500));
}

const switched = logs.some((l) => l.includes("被别的程序占着") && l.includes(String(EXPECT)));
check("日志说明端口被占并改用了新端口", switched, logs.join(" | ").slice(0, 200));
check("新端口上确实是管理服务", ready !== null, ready ? `status=${ready.status}` : "没等到");

const stillSquat = await get(BUSY, "/api/stats");
check(
	"原来的占位服务没被误杀",
	stillSquat.status === 200 && stillSquat.body.includes("someone else"),
	`status=${stillSquat.status}`
);

// ---- 3) 卸载后自己起的服务要收掉，占位服务仍在 ----
if (typeof dispose === "function") dispose();
await new Promise((r) => setTimeout(r, 1200));
const after = await get(EXPECT, "/api/stats");
check("dispose 后自己起的服务已收", after.status === 0, `status=${after.status}`);
const squatAfter = await get(BUSY, "/api/stats");
check("dispose 后占位服务仍在（不该被我们碰）", squatAfter.status === 200, `status=${squatAfter.status}`);

squatter.close();
try {
	rmSync(tmpDbDir, { recursive: true, force: true });
} catch {}

console.log(`\n端口冲突处理：${pass} 通过 / ${fail} 失败`);
process.exit(fail === 0 ? 0 : 1);
