// 用**真的** @deepseek-ai/dsh-host-webserver + 真的 cordis 跑一遍宿主半。
//
// 上一份 proxy-mount.mjs 用的是手写的假 webServer —— 它复刻了 register 的契约
// （{kind,path,handler}、重复抛错、返回 disposer），但有一件事它证明不了：
// 真宿主里 webServer 是一个 cordis **服务**，只有 `ctx.inject(["webServer"], ...)`
// 在服务就绪时才会回调。如果这条路径不通，插件会安静地什么都不挂，而假 webServer
// 的测试永远绿。
//
// 所以这里直接把 DSH 安装目录里的 WebServer 类装进一个新 Context，
// 让 apply() 走完全真实的加载路径。缺宿主文件时 SKIP（不是 FAIL）。
process.env.BIONIC_PANEL_NO_TRACE = "1";

import http from "node:http";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const DEPS =
	process.env.DEPS_DIR ??
	"C:/tmp/dsh_asar_extract/app/dsh/node_modules/@deepseek-ai";
const CORDIS = join(DEPS, "cordis", "lib", "index.js");
const WEBSERVER = join(DEPS, "dsh-host-webserver", "lib", "index.js");

for (const p of [CORDIS, WEBSERVER]) {
	if (!existsSync(p)) {
		console.log(`[SKIP] 找不到 ${p}（用 DEPS_DIR 指定宿主 node_modules）`);
		process.exit(0);
	}
}

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const PROJECT_DIR = dirname(dirname(TEST_DIR));
const PORT = 8894;

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

const toUrl = (p) => new URL(`file:///${p.replace(/\\/g, "/")}`).href;
const { Context } = await import(toUrl(CORDIS));
const webMod = await import(toUrl(WEBSERVER));
const WebServer = webMod.WebServer ?? webMod.default;
check("宿主 webserver 模块可加载", typeof WebServer === "function", `WebServer=${typeof WebServer}`);

function listening(port) {
	return new Promise((done) => {
		const req = http.get({ host: "127.0.0.1", port, path: "/" }, (res) => {
			res.resume();
			done(true);
		});
		req.on("error", () => done(false));
	});
}

async function waitPort(port, ms) {
	const deadline = Date.now() + ms;
	while (Date.now() < deadline) {
		if (await listening(port)) return true;
		await new Promise((r) => setTimeout(r, 300));
	}
	return false;
}

function get(pathname, port) {
	return new Promise((done) => {
		const req = http.get({ host: "127.0.0.1", port, path: pathname }, (res) => {
			const chunks = [];
			res.on("data", (c) => chunks.push(c));
			res.on("end", () =>
				done({ status: res.statusCode, body: Buffer.concat(chunks).toString() })
			);
		});
		req.on("error", (e) => done({ status: 0, body: String(e.message) }));
	});
}

// ---- 起管理服务（临时库，别写脏用户数据）----
const PY = existsSync("D:/Software/Dev/Miniforge/python.exe")
	? "D:/Software/Dev/Miniforge/python.exe"
	: "python";
const tmpDbDir = mkdtempSync(join(tmpdir(), "bionic-hws-test-"));
const child = spawn(PY, ["-m", "app.admin_ui", "--port", String(PORT)], {
	cwd: PROJECT_DIR,
	env: {
		...process.env,
		PYTHONIOENCODING: "utf-8",
		PYTHONUNBUFFERED: "1",
		BIONIC_DB_PATH: join(tmpDbDir, "agent.db")
	},
	stdio: "ignore",
	windowsHide: true
});
const up = await waitPort(PORT, 40000);
check("管理服务已就绪", up, `port=${PORT}`);
if (!up) {
	child.kill();
	process.exit(1);
}

// ---- 真 Context + 真 WebServer ----
const app = new Context();
const logs = [];
app.logger = {
	info: (...a) => logs.push(["info", a.join(" ")]),
	warn: (...a) => logs.push(["warn", a.join(" ")]),
	error: (...a) => logs.push(["error", a.join(" ")])
};

// WebServer 自己会 bind 一个端口；给 0 让系统分配，避免和我们自己的测试端口打架，
// 然后从服务实例上读出真实端口。
let boundPort = null;
// WebServer 的 Config 里 host / port 都是 required（见 dsh-host-webserver Config）。
// port:0 -> 让系统分配，避免与测试自己的 8894 打架。
app.plugin(WebServer, { host: "127.0.0.1", port: 0 });
const webFiber = app.registry?.get?.("webServer");
void webFiber;

// 等服务就绪（Service.init 是异步的）
const deadline = Date.now() + 15000;
while (Date.now() < deadline) {
	const ws = app.get?.("webServer");
	if (typeof ws?.port === "number") {
		boundPort = ws.port;
		break;
	}
	await new Promise((r) => setTimeout(r, 200));
}
check("真 WebServer 已监听", typeof boundPort === "number", `port=${boundPort}`);
if (typeof boundPort !== "number") {
	child.kill();
	rmSync(tmpDbDir, { recursive: true, force: true });
	console.log("\n=== FAIL：宿主 WebServer 没起来，后续用例无法进行 ===");
	process.exit(1);
}

// ---- 装插件，走真实 apply ----
const { apply } = await import("../lib/index.js");
const dispose = apply(app, {
	adminUrl: `http://127.0.0.1:${PORT}`,
	autoStart: false,
	mountPath: "/bionic-panel"
});
// ctx.inject 的服务回调是异步的，给它一拍
await new Promise((r) => setTimeout(r, 1500));

const page = await get("/bionic-panel/", boundPort);
check(
	"真宿主路由下取到面板页",
	page.status === 200 && page.body.includes("Bionic Agent"),
	`status=${page.status} bytes=${page.body.length}`
);

const stats = await get("/bionic-panel/api/stats", boundPort);
let statsJson = null;
try {
	statsJson = JSON.parse(stats.body);
} catch {}
check(
	"真宿主路由下 /api/stats 正常",
	stats.status === 200 && statsJson?.ok === true,
	`status=${stats.status} body=${stats.body.slice(0, 80)}`
);

const unknown = await get("/definitely-not-ours", boundPort);
check(
	"未注册路径不会命中我们的前缀",
	unknown.status !== 200 || !unknown.body.includes("Bionic Agent"),
	`status=${unknown.status}`
);

// ---- 卸载：路由要能从真 webServer 里摘掉 ----
if (typeof dispose === "function") dispose();
await new Promise((r) => setTimeout(r, 800));
const after = await get("/bionic-panel/api/stats", boundPort);
check(
	"dispose 后前缀路由已摘除",
	after.status !== 200 || !after.body.includes('"ok":true'),
	`status=${after.status} body=${after.body.slice(0, 60)}`
);

// ---- 收尾 ----
child.kill();
await new Promise((r) => setTimeout(r, 800));
try {
	rmSync(tmpDbDir, { recursive: true, force: true });
} catch {}

console.log(`\n真宿主反代：${pass} 通过 / ${fail} 失败`);
process.exit(fail === 0 ? 0 : 1);
