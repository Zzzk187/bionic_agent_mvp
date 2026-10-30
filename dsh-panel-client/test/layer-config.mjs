// 用宿主自己的函数确认：profile 层叠后本包的 patch 真的被读进配置树，且 config 完整。
// 这是「改了 cordis.patch.yml 却没生效」这类问题的唯一可靠答案来源 —— 光看文件存在不算数。
//
// 需要宿主 asar 解包出的 @deepseek-ai/dsh-app-boot（环境缺失就 SKIP）。
import { existsSync } from "node:fs";
import { join } from "node:path";

const DEPS =
	process.env.DEPS_DIR ??
	"C:/tmp/dsh_asar_extract/app/dsh/node_modules/@deepseek-ai";

const DSH_HOME =
	process.env.DSH_HOME ?? join(process.env.USERPROFILE ?? process.env.HOME ?? "", ".dsh");
const PROFILE = process.env.DSH_PROFILE ?? "desktop";
const profileDir =
	process.env.DSH_PROFILE_DIR ?? join(DSH_HOME, "profiles", PROFILE);

const APP_BOOT = join(DEPS, "dsh-app-boot", "lib", "index.js");
if (!existsSync(APP_BOOT)) {
	console.log(`[SKIP] 找不到 ${APP_BOOT}（可用 DEPS_DIR 指定宿主 node_modules）`);
	process.exit(0);
}

const mod = await import(`file:///${APP_BOOT.replace(/\\/g, "/")}`);
const loadProfileDirectory = mod.loadProfileDirectory ?? mod.default?.loadProfileDirectory;
if (typeof loadProfileDirectory !== "function") {
	console.log("SKIP: dsh-app-boot 没有导出 loadProfileDirectory，导出 =", Object.keys(mod));
	process.exit(0);
}

const PKG_NAME = "@local/bionic-agent-panel";
const result = await loadProfileDirectory("dsh", profileDir, APP_BOOT);
const layers = Array.isArray(result) ? result : (result.layers ?? []);
const skipped = Array.isArray(result) ? [] : (result.skippedBundles ?? []);

console.log("profile 目录 =", profileDir);
console.log("layer 数 =", layers.length);
console.log("skippedBundles =", JSON.stringify(skipped));

let failed = 0;
const ok = (name, detail) =>
	console.log(`[PASS] ${name}${detail ? ` — ${detail}` : ""}`);
const bad = (name, detail) => {
	console.log(`[FAIL] ${name}${detail ? ` — ${detail}` : ""}`);
	failed++;
};

if (skipped.some((s) => String(s).includes("bionic"))) {
	bad("本包没有被静默跳过", JSON.stringify(skipped));
}

const mine = layers.find((l) => l?.packageName === PKG_NAME);
if (!mine) {
	bad(
		"profile 层里有本包",
		"没有：宿主没扫到（dependencies 与 dsh.profile.bundles 都要登记）"
	);
} else {
	ok("profile 层里有本包", `patches: ${(mine.patchPaths ?? []).join(", ") || "(空)"}`);

	const entries = [];
	const walk = (node) => {
		if (Array.isArray(node)) return node.forEach(walk);
		if (node === null || typeof node !== "object") return;
		if (node.id === "bionic-agent-panel" || node.name === PKG_NAME) entries.push(node);
		Object.values(node).forEach(walk);
	};
	walk(mine.patches);

	if (entries.length === 0) {
		bad("patch 里有 insert 条目", "没找到 id=bionic-agent-panel 的条目");
	} else {
		const cfg = entries[0].config ?? {};
		ok("patch 里有 insert 条目", JSON.stringify(cfg));
		if (!cfg.adminUrl) bad("config.adminUrl 存在", JSON.stringify(cfg));
		else ok("config.adminUrl 存在", cfg.adminUrl);
		if (cfg.mountPath === undefined) {
			bad("config.mountPath 存在", "未配置：面板会退回纯跨源模式");
		} else {
			ok("config.mountPath 存在", String(cfg.mountPath) || "''（已关闭反代）");
		}
	}
}

console.log(failed === 0 ? "\n[PASS] 层叠与 config 全部对上" : `\n[FAIL] ${failed} 项不对`);
process.exit(failed === 0 ? 0 : 1);
