#!/usr/bin/env node
// 安装态自检：确认 dsh-panel-client 真的被 DeepSeek Harness 的 desktop profile 接上了。
//
// 用途：装完插件、重启 DSH 之后跑一遍，或在「侧边栏没出现图标」时定位卡在哪一层。
// 设计成不需要 DSH 的 asar 解包目录——用 Node 自己实现宿主那套判定
// （读 profile 的 package.json → 找 bundle → 读它的 cordis.patch.yml → 看 insert 行），
// 这样换台机器也能直接跑。
//
// 退出码：0 = 静态接线全部正确；1 = 有检查项失败。

import { existsSync, readFileSync, statSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { connect } from "node:net";
import { findAsar, readDshRuntimeVersion } from "./test/asar-read.mjs";

/**
 * 最小 semver 范围判断，只覆盖本项目会写的几种形状（>=x.y.z、>x.y.z、^x.y.z、~x.y.z、精确值）。
 * 不去引入 semver 依赖 —— 这个自检脚本要能在没有任何 node_modules 的机器上直接跑。
 */
function satisfies(version, range) {
	const parse = (v) => {
		const m = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/.exec(String(v).trim());
		if (!m) return null;
		return { major: +m[1], minor: +m[2], patch: +m[3], pre: m[4] ?? "" };
	};

	// 预发布标识按 semver 规则逐段比较：纯数字段按数值、数字段小于字母段、
	// 前面都相同则段数少的更小。整串当字符串比会把 rc.10 判得比 rc.3 小。
	const cmpPre = (a, b) => {
		if (a === b) return 0;
		if (a === "") return 1; // 没有预发布 = 正式版，大于同号预发布
		if (b === "") return -1;
		const pa = a.split(".");
		const pb = b.split(".");
		for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
			const x = pa[i];
			const y = pb[i];
			if (x === undefined) return -1;
			if (y === undefined) return 1;
			const nx = /^\d+$/.test(x);
			const ny = /^\d+$/.test(y);
			if (nx && ny) {
				if (+x !== +y) return +x < +y ? -1 : 1;
			} else if (nx !== ny) {
				return nx ? -1 : 1; // 数字段 < 字母段
			} else if (x !== y) {
				return x < y ? -1 : 1;
			}
		}
		return 0;
	};

	const cmp = (a, b) => {
		for (const key of ["major", "minor", "patch"]) if (a[key] !== b[key]) return a[key] < b[key] ? -1 : 1;
		return cmpPre(a.pre, b.pre);
	};

	// 上界（不含）：^ 允许最左非零位不变，~ 允许次版本位不变。
	const upperBound = (op, b) => {
		if (op === "^") {
			if (b.major > 0) return { major: b.major + 1, minor: 0, patch: 0, pre: "" };
			if (b.minor > 0) return { major: b.major, minor: b.minor + 1, patch: 0, pre: "" };
			return { major: b.major, minor: b.minor, patch: b.patch + 1, pre: "" };
		}
		if (op === "~") return { major: b.major, minor: b.minor + 1, patch: 0, pre: "" };
		return null;
	};

	const v = parse(version);
	if (!v) return false;
	// 允许用 || 连接多个范围
	return String(range)
		.split("||")
		.some((part) => {
			const m = /^\s*(>=|<=|>|<|\^|~|=)?\s*(\S+)\s*$/.exec(part);
			if (!m) return false;
			const op = m[1] ?? "";
			const bound = parse(m[2]);
			if (!bound) return false;
			if (op === ">=") return cmp(v, bound) >= 0;
			if (op === ">") return cmp(v, bound) > 0;
			if (op === "<=") return cmp(v, bound) <= 0;
			if (op === "<") return cmp(v, bound) < 0;
			if (op === "=" || op === "") return cmp(v, bound) === 0;
			if (op === "^" || op === "~") {
				// 同时有下界与上界：>= bound && < upper
				return cmp(v, bound) >= 0 && cmp(v, upperBound(op, bound)) < 0;
			}
			return false;
		});
}

// 本脚本直接放在包根（与 package.json 同级）
const PKG_DIR = dirname(fileURLToPath(import.meta.url));
const PKG = JSON.parse(readFileSync(join(PKG_DIR, "package.json"), "utf8"));
const PKG_NAME = PKG.name;

const results = [];
function check(name, ok, detail = "") {
	results.push({ name, ok, detail });
	const mark = ok ? "PASS" : "FAIL";
	console.log(`[${mark}] ${name}${detail ? " — " + detail : ""}`);
}

// 不算失败、只提示的检查（比如"DSH 还没重启"这种必然状态）。
function pending(name, detail = "") {
	results.push({ name, ok: true, pending: true });
	console.log(`[WAIT] ${name}${detail ? " — " + detail : ""}`);
}

// ── 1. 找到 desktop profile ───────────────────────────────────────────
const DSH_HOME = process.env.DSH_HOME ?? join(process.env.USERPROFILE ?? process.env.HOME ?? "", ".dsh");
const profileName = process.env.DSH_PROFILE ?? "desktop";
const profileDir = join(DSH_HOME, "profiles", profileName);
check("profile 目录存在", existsSync(profileDir), profileDir);

if (!existsSync(profileDir)) {
	console.log("\n找不到 profile，插件没装。先跑：python dsh-panel-client/install.py");
	process.exit(1);
}

// ── 2. profile 的 package.json 里登记了包名 ──────────────────────────
const profilePkgPath = join(profileDir, "package.json");
const profilePkg = JSON.parse(readFileSync(profilePkgPath, "utf8"));
const inDeps = Boolean(profilePkg.dependencies?.[PKG_NAME]);
const inBundles = (profilePkg.dsh?.profile?.bundles ?? []).includes(PKG_NAME);
check(`dependencies 含 ${PKG_NAME}`, inDeps);
check(`dsh.profile.bundles 含 ${PKG_NAME}`, inBundles);

// ── 3. node_modules 里的包能解析到 ──────────────────────────────────
const installedDir = join(profileDir, "node_modules", ...PKG_NAME.split("/"));
const installedOk = existsSync(join(installedDir, "package.json"));
check("node_modules 里可找到包", installedOk, installedDir);

// ── 4. bundle 声明与 patch 文件（宿主加载 bundle 的前置条件）─────────
if (installedOk) {
	const installedPkg = JSON.parse(readFileSync(join(installedDir, "package.json"), "utf8"));
	const bundleDecl = installedPkg.dsh?.bundle;
	check("声明了 dsh.bundle.patch", Boolean(bundleDecl?.patch));

	const patchRel = typeof bundleDecl?.patch === "string" ? bundleDecl.patch : bundleDecl?.patch?.[0];
	const patchPath = patchRel ? join(installedDir, patchRel) : undefined;
	const patchOk = Boolean(patchPath && existsSync(patchPath));
	check("cordis.patch.yml 存在", patchOk, patchPath ?? "");

	if (patchOk) {
		// 极简解析：只关心有没有 insert 出本包这一行，以及 id 行有没有被行尾注释污染
		const text = readFileSync(patchPath, "utf8");
		check("patch 里有 insert 条目", /^\s*-\s*insert:/m.test(text));
		check("patch 引用了本包名", text.includes(PKG_NAME));
		// 宿主商城预检用正则抓 id 值，行尾注释会把注释也抓进去
		const dirtyId = text
			.split(/\r?\n/)
			.find((line) => /^\s*-\s*id:\s*\S+\s+#/.test(line));
		check("id 行没有行尾注释", dirtyId === undefined, dirtyId ?? "");
	}

	// ── 5. 两半入口都在 ──────────────────────────────────────────────
	check("宿主半 main 存在", existsSync(join(installedDir, installedPkg.main ?? "index.js")), installedPkg.main ?? "");
	const clientRel =
		typeof installedPkg.exports?.["./client"] === "string"
			? installedPkg.exports["./client"]
			: installedPkg.exports?.["./client"]?.default;
	check("client 半 exports['./client'] 存在", Boolean(clientRel && existsSync(join(installedDir, clientRel))), clientRel ?? "");
	check("dsh.client.platform === 'web'", installedPkg.dsh?.client?.platform === "web", String(installedPkg.dsh?.client?.platform));

	// ── 6. 版本兼容（宿主读 dsh.engines.dsh，不是顶层 engines）──────
	const enginesDsh = installedPkg.dsh?.engines?.dsh;
	check("声明了 dsh.engines.dsh（否则宿主要退去看 peerDependencies）", Boolean(enginesDsh), String(enginesDsh ?? ""));
	if (enginesDsh) {
		const runtimeVersion = readDshRuntimeVersion(findAsar());
		if (runtimeVersion) {
			check(
				`运行时版 ${runtimeVersion} 满足 ${enginesDsh}`,
				satisfies(runtimeVersion, enginesDsh),
				satisfies(runtimeVersion, enginesDsh) ? "" : "不满足：宿主会把这个 bundle 记为 skipped 并静默跳过"
			);
		} else {
			console.log("[SKIP] 读不到 DSH 运行时版本（找不到 app.asar），请自行确认");
		}
	}
}

// ── 7. 管理服务是否在跑（插件宿主半的产物）──────────────────────────
function isListening(port, timeoutMs = 700) {
	return new Promise((done) => {
		const socket = connect({ host: "127.0.0.1", port });
		let settled = false;
		const finish = (up) => {
			if (settled) return;
			settled = true;
			socket.destroy();
			done(up);
		};
		socket.setTimeout(timeoutMs);
		socket.once("connect", () => finish(true));
		socket.once("timeout", () => finish(false));
		socket.once("error", () => finish(false));
	});
}

const adminUrl = "http://127.0.0.1:8899";
const port = Number(new URL(adminUrl).port || 80);
const listening = await isListening(port);
check(`管理服务在 ${port} 监听`, listening, listening ? adminUrl : "没在跑：DSH 可能还没重启，或插件宿主半没生效");

if (listening) {
	try {
		const res = await fetch(`${adminUrl}/api/stats`);
		const body = await res.json();
		check("GET /api/stats 正常", res.ok && body.ok === true, JSON.stringify(body.data ?? {}).slice(0, 120));
	} catch (error) {
		check("GET /api/stats 正常", false, String(error?.message ?? error));
	}
}

// ── 8. 宿主半的启动痕迹（判断「插件到底加载没加载」的唯一直接证据）──────
// panel-boot.log 由 lib/index.js 的 bootTrace() 写：apply() 一被调用就落一行。
// 有这行 = 宿主真的把这插件的 Node 半执行了；没有 = 根本没加载（被静默跳过）。
//
// 这一项在"还没重启 DSH"时必然为空，属于预期状态而非缺陷 —— 所以用 pending
// 而不是 fail，避免把"你还没重启"报成"插件坏了"。
const bootLog = resolve(PKG_DIR, "..", "data", "panel-boot.log");
if (existsSync(bootLog)) {
	const lines = readFileSync(bootLog, "utf8").trim().split(/\r?\n/).filter(Boolean);
	check("宿主半加载过（data/panel-boot.log 有痕迹）", lines.length > 0, `${lines.length} 行`);
	console.log("  最近几行：");
	for (const line of lines.slice(-6)) console.log("    " + line);
} else {
	pending("宿主半加载过（data/panel-boot.log 有痕迹）",
		"还没有痕迹：若你尚未重启 DSH 属正常；重启后仍为空，说明插件被宿主静默跳过（多半是 dsh.engines.dsh 不满足）");
}

// ── 9. 浏览器半的报到痕迹（判断「iframe 到底渲染没渲染」的唯一直接证据）──
// panel-client.log 由面板页面里的 fetch('/api/ping') 写。它和 panel-boot.log 是
// 两层独立的信号：前者证明浏览器真的把 iframe 跑起来了，后者只证明 Node 侧加载了。
// 两个都空 = 没重启；只有 boot 有 = 卡在浏览器端；两个都有 = 通路全通。
const clientLog = resolve(PKG_DIR, "..", "data", "panel-client.log");
if (existsSync(clientLog)) {
	const lines = readFileSync(clientLog, "utf8").trim().split(/\r?\n/).filter(Boolean);
	// 只认「被宿主嵌进来的」那一条，其余都不算证据：
	//  - curl / 脚本探测：user-agent 带 curl/
	//  - 我或用户直接在浏览器打开 8899：referer 就是 8899 自己
	// 宿主 iframe 的 referer 一定带 DSH 的端口（如 59647），不含 :8899。
	const hosted = lines.filter((line) => {
		if (line.includes("curl/")) return false;
		let referer = "";
		try { referer = String(JSON.parse(line).referer || ""); } catch { return false; }
		return referer !== "" && !referer.includes(":8899");
	});
	if (hosted.length > 0) {
		check("浏览器半跑起来了（data/panel-client.log 有宿主 iframe 报到）",
			true,
			`共 ${lines.length} 条，其中来自宿主 iframe ${hosted.length} 条`);
		console.log("  最近几条宿主报到：");
		for (const line of hosted.slice(-3)) console.log("    " + line);
	} else {
		// 有记录但没有一条来自宿主 iframe：说明还没被嵌进去过（直开面板/脚本探测都不算）。
		// 这和"没有文件"是同一件事，只是多了一批噪声记录，同样按待确认处理。
		pending("浏览器半跑起来了（data/panel-client.log 有宿主 iframe 报到）",
			`有 ${lines.length} 条报到，但都不是宿主 iframe（直开面板或脚本探测不算）；等你重启并点开侧边栏图标后再看`);
	}
} else {
	pending("浏览器半跑起来了（data/panel-client.log 有宿主 iframe 报到）",
		"还没有报到：若你尚未重启/尚未点开面板属正常；重启并点开侧边栏图标后仍为空，说明 client.js 在浏览器端没跑起来");
}

// ── 10. 宿主半的日志（启动失败时最直接的证据）─────────────────────────
const logPath = resolve(PKG_DIR, "..", "data", "admin_ui.log");
if (existsSync(logPath)) {
	const size = statSync(logPath).size;
	const tail = readFileSync(logPath, "utf8").trim().split(/\r?\n/).slice(-6).join("\n");
	check("data/admin_ui.log 存在", true, `${size} 字节`);
	console.log("  最近几行：");
	for (const line of tail.split("\n")) console.log("    " + line);
} else {
	console.log("[SKIP] 还没有 data/admin_ui.log（插件从未拉起过服务）");
}

// ── 汇总 ────────────────────────────────────────────────────────────
const failed = results.filter((r) => !r.ok);
const waiting = results.filter((r) => r.pending);
console.log(`\n${results.length - failed.length - waiting.length}/${results.length - waiting.length} 项通过` +
	(waiting.length ? `（另有 ${waiting.length} 项待重启后确认）` : ""));
if (failed.length) {
	console.log("失败项：" + failed.map((r) => r.name).join("、"));
	process.exit(1);
}
console.log("静态接线全部正确。若侧边栏仍无图标，问题在浏览器端：看 DSH 窗口的 DevTools Console。");
console.log("静态接线全部正确。若侧边栏仍无图标，问题在浏览器端：看 DSH 窗口的 DevTools Console。");
