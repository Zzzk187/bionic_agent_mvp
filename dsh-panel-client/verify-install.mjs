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
import { findAsar, readDshRuntimeVersion } from "./test/asar-read.mjs";
import { satisfies } from "./semver-lite.mjs";


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
// 管理服务端口不能写死 8899。
// 插件宿主半在「8899 已被别的程序占着、且形状不像本服务」时会自动往后换端口
// （pickFreePort），所以这里要按同样的思路扫一段端口，用 /api/stats 的响应形状
// 认出「哪个才是我们的」，而不是假定它在 8899。
// 形状判据与 lib/index.js 的 identifyAdmin() 保持一致：ok===true 且 data.memories_total 存在。
const PORT_SCAN = [8899, 8900, 8901, 8902, 8903, 8904, 8905, 8906, 8907, 8908];

/** 探一个端口上是不是我们的管理服务；是则返回 {url, data}。 */
async function probeAdmin(candidatePort, timeoutMs = 2500) {
	const url = `http://127.0.0.1:${candidatePort}`;
	try {
		const res = await fetch(`${url}/api/stats`, { signal: AbortSignal.timeout(timeoutMs) });
		if (!res.ok) return undefined;
		const body = await res.json();
		if (body?.ok === true && body?.data !== undefined && "memories_total" in body.data) {
			return { url, data: body.data };
		}
	} catch {
		/* 端口没人听 / 不是 HTTP / 不是我们的 JSON：下一个 */
	}
	return undefined;
}

let adminUrl;
let adminData;
for (const candidate of PORT_SCAN) {
	const hit = await probeAdmin(candidate);
	if (hit) {
		adminUrl = hit.url;
		adminData = hit.data;
		break;
	}
}

if (adminUrl) {
	check(
		"管理服务在监听",
		true,
		`${adminUrl}${adminUrl.endsWith(":8899") ? "" : "（8899 被占，插件自动换到了这个端口）"}`
	);
	check("GET /api/stats 正常", true, JSON.stringify(adminData).slice(0, 120));
} else {
	check(
		"管理服务在监听",
		false,
		`${PORT_SCAN[0]}–${PORT_SCAN[PORT_SCAN.length - 1]} 都没探到：DSH 可能还没重启，或插件宿主半没生效`
	);
}

// ── 8. 宿主半的启动痕迹（判断「插件到底加载没加载」的唯一直接证据）──────
// panel-boot.log 由 lib/index.js 的 bootTrace() 写：apply() 一被调用就落一行。
// 有这行 = 宿主真的把这插件的 Node 半执行了；没有 = 根本没加载（被静默跳过）。
//
// 这一项在"还没重启 DSH"时必然为空，属于预期状态而非缺陷 —— 所以用 pending
// 而不是 fail，避免把"你还没重启"报成"插件坏了"。
//
// 但日志只是「快照」，会被清理误删（我自己就删过一次假信号）。所以这里还并列一个
// 更强的证据：DSH 自己端口上那条活着的前缀反代路由 —— 它由宿主半 apply() 注册，
// 只要还能取到 /bionic-panel/api/stats 的真实 JSON，就证明宿主半在运行中确实跑过，
// 这比任何日志都硬（活着的路由伪造不了）。两者任一成立即算通过。
const bootLog = resolve(PKG_DIR, "..", "data", "panel-boot.log");
let bootOk = false;
let bootDetail = "";
// 当前正在运行的管理服务实例标识（来自 /api/stats 的 boot 字段）。
// 第 9 项靠它把「上一批进程留下的旧报到」和「这次真的被嵌进来了」区分开。
let liveBootId = null;

// (a) 日志痕迹
if (existsSync(bootLog)) {
	const lines = readFileSync(bootLog, "utf8").trim().split(/\r?\n/).filter(Boolean);
	if (lines.length > 0) {
		bootOk = true;
		bootDetail = `${lines.length} 行痕迹`;
		console.log("  最近几行：");
		for (const line of lines.slice(-6)) console.log("    " + line);
	}
}

// (b) 活路由证据：DSH 端口上的同源反代还在应答
if (!bootOk) {
	const dshUrl = (process.env.DSH_WEB_URL || "").replace(/\/+$/, "");
	if (dshUrl) {
		try {
			const res = await fetch(`${dshUrl}/bionic-panel/api/stats`, { signal: AbortSignal.timeout(4000) });
			const body = await res.json();
			if (res.ok && body?.ok === true && body?.data !== undefined) {
				bootOk = true;
				bootDetail = `活路由应答 ${dshUrl}/bionic-panel/api/stats（比日志更硬的证据）`;
			} else {
				bootDetail = `DSH 端口有响应但不是我们的路由（HTTP ${res.status}）`;
			}
		} catch (error) {
			bootDetail = `探测 ${dshUrl} 失败：${String(error?.message ?? error)}`;
		}
	} else {
		bootDetail = "环境里没有 DSH_WEB_URL，无法做活路由探测";
	}
}

if (bootOk) {
	check("宿主半加载过（日志痕迹 或 DSH 端口上的活路由）", true, bootDetail);
} else {
	pending("宿主半加载过（日志痕迹 或 DSH 端口上的活路由）",
		`还没有证据：若你尚未重启 DSH 属正常；重启后仍无，说明插件被宿主静默跳过（多半是 dsh.engines.dsh 不满足）。${bootDetail}`);
}

// ── 9. 浏览器半的报到痕迹（判断「iframe 到底渲染没渲染」的唯一直接证据）──
// panel-client.log 由面板页面里的 fetch('/api/ping') 写。它和 panel-boot.log 是
// 两层独立的信号：前者证明浏览器真的把 iframe 跑起来了，后者只证明 Node 侧加载了。
// 两个都空 = 没重启；只有 boot 有 = 卡在浏览器端；两个都有 = 通路全通。

// 先取当前服务实例的 boot id：痕迹文件是跨进程累积的，不比对就会把
// 「上一批进程里点开过面板」误当成本次证据（这坑自己踩过）。
if (adminUrl) {
	try {
		const res = await fetch(`${adminUrl}/api/stats`, { signal: AbortSignal.timeout(3000) });
		const body = await res.json();
		if (res.ok && body?.ok === true && typeof body?.data?.boot === "string") {
			liveBootId = body.data.boot;
		}
	} catch {
		/* 服务没起来时 liveBootId 保持 null，第 9 项退化为"只看有没有宿主报到" */
	}
}

const clientLog = resolve(PKG_DIR, "..", "data", "panel-client.log");
if (existsSync(clientLog)) {
	const lines = readFileSync(clientLog, "utf8").trim().split(/\r?\n/).filter(Boolean);
	// 只认「被宿主嵌进来的」那一条，其余都不算证据：
	//  - curl / 脚本探测：user-agent 带 curl/
	//  - 我或用户直接用外部浏览器打开 8899：UA 是普通浏览器，没有 DSH 标识
	//
	// 最可靠的判据是 UA：DSH 的 Electron 窗口会带 `@deepseek-ai/dsh-desktop/<ver>`。
	// 不再依赖 referer —— 实测宿主 iframe 里发起的 fetch 的 referer 是空串
	// （页面声明了 no-referrer 之类），按 referer 判定会把真实报到误判成噪声。
	//
	// 报到分两类，证据强度不同，必须分开判：
	//   ① source=client-boot（client.js 顶层发出，dest=empty）—— 证明「浏览器半真的被
	//      宿主求值了」。这条不需要用户点开面板就会自己来，是最硬的自动证据。
	//   ② dest=iframe —— 证明「面板真的被嵌进去打开过」，只有用户点开才有。
	const hosted = lines.filter((line) => {
		if (line.includes("curl/")) return false;
		let record;
		try { record = JSON.parse(line); } catch { return false; }
		const ua = String(record.user_agent || "");
		if (!/dsh-desktop|deepseek-ai\/dsh|DeepSeekHarness/i.test(ua)) return false;
		const source = String(record.source ?? "");
		if (source === "client-boot") return true;
		const dest = String(record.dest ?? "");
		// dest 为空 = 旧版本记的，保留兼容；有值就必须是 iframe
		return dest === "" || dest === "iframe";
	});
	// 排除"上一批进程留下的旧报到"：
	// 痕迹文件是跨进程追加的，昨天点开过面板也会留一条宿主 iframe 记录。
	// 若当前服务实例（/api/stats 的 boot）已经能取到，就只认同一 boot 的报到，
	// 否则「没重启过也可能显示 PASS」—— 这正是我之前被误导的地方。
	const currentBoot = liveBootId;
	const fresh = currentBoot === null ? hosted : hosted.filter((line) => {
		try { return JSON.parse(line).boot === currentBoot; } catch { return false; }
	});
	const parseSafe = (line) => { try { return JSON.parse(line); } catch { return {}; } };
	const bootReports = fresh.filter((l) => String(parseSafe(l).source ?? "") === "client-boot");
	const openedReports = fresh.filter((l) => {
		const r = parseSafe(l);
		return String(r.source ?? "") !== "client-boot" && String(r.dest ?? "") !== "empty";
	});

	// ① 浏览器半是否被加载 —— 只要 client.js 被求值，这条就会自己来。
	if (bootReports.length > 0) {
		check("浏览器半加载了（client.js 一被求值就自动报到）",
			true,
			`当前服务实例 ${currentBoot ?? "(未知)"} 收到 ${bootReports.length} 条 client-boot 报到`);
	} else if (fresh.length > 0) {
		pending("浏览器半加载了（client.js 一被求值就自动报到）",
			`当前实例 ${currentBoot} 还没收到 client-boot 报到；若你刚重启过，刷新一次 DSH 窗口再看`);
	} else if (currentBoot !== null) {
		pending("浏览器半加载了（client.js 一被求值就自动报到）",
			`当前实例 ${currentBoot} 没收到任何报到 —— 若 DSH 已重启且刷新过窗口仍为空，说明宿主跳过了这个插件或 client.js 没被求值`);
	} else {
		pending("浏览器半加载了（client.js 一被求值就自动报到）", "拿不到当前服务实例，无法判定");
	}

	// ② 面板是否真的被打开过 —— 这条要用户点侧边栏图标，属体验验证，不是安装正确性。
	if (openedReports.length > 0) {
		check("面板被打开过（宿主 iframe 报到）",
			true,
			`当前实例 ${currentBoot ?? "(未知)"} 收到 ${openedReports.length} 条 iframe 报到`);
		console.log("  最近几条 iframe 报到：");
		for (const line of openedReports.slice(-3)) console.log("    " + line);
	} else if (hosted.length > 0 && currentBoot !== null) {
		pending("面板被打开过（宿主 iframe 报到）",
			`有 ${hosted.length} 条历史报到，但都属于更早的服务实例；当前实例 ${currentBoot} 还没收到 —— 点开侧边栏「记忆/任务」图标后即可确认`);
	} else {
		pending("面板被打开过（宿主 iframe 报到）",
			"还没有 iframe 报到（直开面板/脚本探测都不算）；点开侧边栏图标后再看");
	}
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
