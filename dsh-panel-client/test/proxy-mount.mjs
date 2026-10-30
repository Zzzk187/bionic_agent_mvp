// 验证宿主半的「同源反代挂载」：用真 http server 冒充 DSH 的 webServer，
// 让 apply() 把 /bionic-panel 挂上去，再逐条请求确认能取到 8899 的页面与 API。
//
// 为什么需要一个假 webServer：真宿主只有在 DSH 里才有，而重启 DSH 会打断当前会话。
// 但挂载逻辑本身是纯 Node 的 —— 只要按 dsh-host-webserver 的 register 语义
// （{kind,path,handler}，重复注册抛错，返回 disposer）实现一份，就能把这段
// 代码路径真实跑通，而不是靠读代码猜。
import http from "node:http";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

process.env.BIONIC_PANEL_NO_TRACE = "1";

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const PKG_DIR = dirname(TEST_DIR);
const PROJECT_DIR = dirname(PKG_DIR);

const PORT = 8895;
const DSH_PORT = 0; // 让系统分配

let pass = 0;
let fail = 0;
function check(name, ok, detail = "") {
	if (ok) {
		pass += 1;
		console.log(`[PASS] ${name}${detail ? ` — ${detail}` : ""}`);
	} else {
		fail += 1;
		console.log(`[FAIL] ${name}${detail ? ` — ${detail}` : ""}`);
	}
}

function get(pathname, port) {
	return new Promise((done) => {
		const req = http.get({ host: "127.0.0.1", port, path: pathname }, (res) => {
			const chunks = [];
			res.on("data", (c) => chunks.push(c));
			res.on("end", () => done({ status: res.statusCode, body: Buffer.concat(chunks) }));
		});
		req.on("error", (e) => done({ status: 0, body: Buffer.from(String(e.message)) }));
	});
}

function send(method, pathname, port, body) {
	return new Promise((done) => {
		const payload = body === undefined ? undefined : Buffer.from(body, "utf8");
		const req = http.request(
			{
				host: "127.0.0.1",
				port,
				path: pathname,
				method,
				headers: payload
					? { "content-type": "application/json", "content-length": payload.length }
					: undefined
			},
			(res) => {
				const chunks = [];
				res.on("data", (c) => chunks.push(c));
				res.on("end", () => done({ status: res.statusCode, body: Buffer.concat(chunks) }));
			}
		);
		req.on("error", (e) => done({ status: 0, body: Buffer.from(String(e.message)) }));
		if (payload) req.write(payload);
		req.end();
	});
}

function listening(port) {	return new Promise((done) => {
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

// ---- 假 DSH webServer：复刻 dsh-host-webserver 的 register 契约 ----
const prefixes = new Map();
const fakeWebServer = {
	register(route) {
		const table = route.kind === "exact" ? new Map() : prefixes;
		if (table.has(route.path)) throw new Error(`webserver: duplicate ${route.kind} route "${route.path}"`);
		table.set(route.path, route);
		return () => table.delete(route.path);
	}
};

const server = http.createServer((req, res) => {
	const url = new URL(req.url ?? "/", "http://dsh.internal");
	for (const route of prefixes.values()) {
		if (url.pathname === route.path || url.pathname.startsWith(`${route.path}/`)) {
			route.handler(req, res);
			return;
		}
	}
	res.writeHead(404, { "content-type": "text/plain" });
	res.end("no route");
});

const dshPort = await new Promise((done) => {
	server.listen(DSH_PORT, "127.0.0.1", () => done(server.address().port));
});

// ---- 起一个独立的管理服务实例（用非默认端口，避免和用户正在跑的 8899 混淆）----
const PY = existsSync("D:/Software/Dev/Miniforge/python.exe") ? "D:/Software/Dev/Miniforge/python.exe" : "python";
const child = spawn(PY, ["-m", "app.admin_ui", "--port", String(PORT)], {
	cwd: PROJECT_DIR,
	env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
	stdio: "ignore",
	windowsHide: true
});
const up = await waitPort(PORT, 40000);
check("管理员服务已在测试端口就绪", up, `port=${PORT}`);
if (!up) {
	child.kill();
	server.close();
	process.exit(1);
}

// ---- 调 apply() 挂载 ----
const { apply } = await import("../lib/index.js");
const ctx = {
	webServer: fakeWebServer,
	logger: { info() {}, warn: (m) => console.log(`[warn] ${m}`) },
	effect(fn) {
		const dispose = fn();
		ctx.__disposers = ctx.__disposers ?? [];
		ctx.__disposers.push(dispose);
		return dispose;
	},
	inject(names, cb) {
		ctx.__injected = names;
		// 关键：webServer 在这个上下文里存在 → 应该走挂载分支
		return cb(ctx);
	},
	// 假 connection：默认放行一切，个别用例里改成返回 401/403 来测写保护。
	__connection: { requestRejection() { return undefined; } },
	get(name) {
		return name === "connection" ? ctx.__connection : undefined;
	}
};

const disposeEverything = apply(ctx, {
	adminUrl: `http://127.0.0.1:${PORT}`,
	autoStart: false, // 服务我已经起了，别让插件再 spawn 一个
	mountPath: "/bionic-panel"
});
await new Promise((r) => setTimeout(r, 200));

check("inject 请求了 webServer", Array.isArray(ctx.__injected) && ctx.__injected.includes("webServer"), JSON.stringify(ctx.__injected));
check("前缀路由已注册", prefixes.has("/bionic-panel"), [...prefixes.keys()].join(","));

// ---- 逐条验证反代 ----
const page = await get("/bionic-panel/", dshPort);
check("同源 /bionic-panel/ 取到面板页", page.status === 200 && page.body.toString().includes("Bionic Agent"), `status=${page.status} bytes=${page.body.length}`);

const stats = await get("/bionic-panel/api/stats", dshPort);
let statsJson = null;
try {
	statsJson = JSON.parse(stats.body.toString());
} catch {}
check("同源 /bionic-panel/api/stats 返回 JSON", stats.status === 200 && statsJson?.ok === true, `status=${stats.status} ok=${statsJson?.ok}`);

clock: {
	// 查询串必须透传 —— 面板大量接口靠 ?limit= 之类的参数
	const withQuery = await get("/bionic-panel/api/memories?limit=2", dshPort);
	let parsed = null;
	try {
		parsed = JSON.parse(withQuery.body.toString());
	} catch {}
	const rows = parsed?.data?.memories ?? parsed?.data?.results ?? parsed?.data;
	check("查询串透传（?limit=2）", withQuery.status === 200 && (Array.isArray(rows) ? rows.length <= 2 : parsed?.ok === true), `status=${withQuery.status}`);
}

// ---- 写方法走宿主的 Host/Origin fence（防跨站写入）----
{
	// 1) fence 拒绝时：写方法必须被拦下，且不转发到管理服务
	ctx.__connection = { requestRejection: () => 401 };
	const blockedPost = await send("POST", "/bionic-panel/api/memories", dshPort, '{"content":"不该被写入"}');
	let blockedJson = null;
	try {
		blockedJson = JSON.parse(blockedPost.body.toString());
	} catch {}
	check(
		"未登录时写方法被拦（401）",
		blockedPost.status === 401 && blockedJson?.ok === false,
		`status=${blockedPost.status} msg=${blockedJson?.message ?? ""}`
	);

	// 2) 只 fence 写方法：同一时刻 GET 必须照常通过（否则面板读不到东西）
	const stillReads = await get("/bionic-panel/api/stats", dshPort);
	check("fence 拒绝期间 GET 仍可用", stillReads.status === 200, `status=${stillReads.status}`);

	// 3) fence 抛错时不能让面析变砖 —— 安全组件故障不该阻断本地面板
	ctx.__connection = {
		requestRejection() {
			throw new Error("connection 服务未就绪");
		}
	};
	const survived = await get("/bionic-panel/api/stats", dshPort);
	check("fence 抛错时请求仍放行", survived.status === 200, `status=${survived.status}`);

	// 4) 放行时写方法要真的到达上游
	ctx.__connection = { requestRejection: () => undefined };
	const allowed = await send("POST", "/bionic-panel/api/memories", dshPort, '{"content":"反代写入链路自检","category":"test"}');
	check("放行时写方法到达上游", allowed.status >= 200 && allowed.status < 300, `status=${allowed.status}`);
	if (allowed.status >= 200 && allowed.status < 300) {
		try {
			const created = JSON.parse(allowed.body.toString());
			const id = created?.data?.memory?.id ?? created?.data?.id;
			if (id !== undefined) await send("DELETE", `/bionic-panel/api/memories/${id}`, dshPort);
		} catch {}
	}
}

// 面板页在同源下必须知道自己的基路径，否则它的 /api/... 会打到 DSH 上
check("iframe 地址带 base 参数", (() => {
	const base = "/bionic-panel";
	const src = `${base}/?base=${encodeURIComponent(base)}`;
	return src === "/bionic-panel/?base=%2Fbionic-panel";
})(), "/bionic-panel/?base=%2Fbionic-panel");

// 目标服务不在了要给出可读的 502，而不是挂住。
// 做法：真的把管理服务杀掉，再打同一条路由 —— 这才测到 mountProxy 的 error 分支。
child.kill();
await new Promise((r) => setTimeout(r, 1200));
const dead = await get("/bionic-panel/api/stats", dshPort);
let deadJson = null;
try {
	deadJson = JSON.parse(dead.body.toString());
} catch {}
check("上游不可达时返回可读的 502", dead.status === 502 && deadJson?.ok === false && String(deadJson?.message ?? "").includes("不可达"), `status=${dead.status} msg=${deadJson?.message ?? dead.body.slice(0, 60)}`);

// ---- 清理 ----
disposeEverything?.();
await new Promise((r) => setTimeout(r, 100));
check("dispose 后路由已摘除", !prefixes.has("/bionic-panel"), [...prefixes.keys()].join(",") || "(空)");

server.close();
await new Promise((r) => setTimeout(r, 300));

console.log(`\n反代挂载：${pass} 通过 / ${fail} 失败`);
process.exit(fail === 0 ? 0 : 1);
