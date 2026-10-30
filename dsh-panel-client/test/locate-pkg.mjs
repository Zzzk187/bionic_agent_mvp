#!/usr/bin/env node
// 验证宿主能不能「找到我这个包的 manifest」。
//
// 为什么单独验这个：本插件是用符号链接装进 profile 的，而其他插件都是真实目录。
// 宿主 dsh-client-modules 的 locatePkgJson() 走的是「先解析模块真实路径，
// 再往上逐级找 package.json，且 name 必须等于包名」这条路。Node 默认会把
// 符号链接解析成真实路径（preserveSymlinks=false），所以模块 URL 会落在
// D:\Cache\...\dsh-panel-client\lib\index.js，而不是 profile 的 node_modules 下。
//
// 如果 nearestPackage 只认 profile 目录那一层，我的包就会被判定为「不是 client 包」
// 而静默跳过 —— 界面上将什么都没有，且不报错。这正是不重启就无法排除的风险。
//
// 这里把宿主那段逻辑原样复刻一遍（createRequire 解析 → 向上找 manifest → 比对 name），
// 用真实的 profile 目录跑，能提前证明链接形态不会导致失配。
import { createRequire } from "node:module";
import { dirname, join, isAbsolute } from "node:path";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";

const PKG_DIR = dirname(dirname(fileURLToPath(import.meta.url)));
const PKG = JSON.parse(readFileSync(join(PKG_DIR, "package.json"), "utf8"));
const PKG_NAME = PKG.name;

const DSH_HOME = process.env.DSH_HOME ?? join(process.env.USERPROFILE ?? process.env.HOME ?? "", ".dsh");
const profileName = process.env.DSH_PROFILE ?? "desktop";
const profileDir = join(DSH_HOME, "profiles", profileName);

let failed = 0;
function check(name, ok, detail = "") {
	console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " — " + detail : ""}`);
	if (!ok) failed++;
}

// ── 复刻宿主的 nearestPackage()：从模块文件往上找 manifest，name 要匹配 ──
function nearestPackage(modulePath, expectedPackageName) {
	let dir = dirname(modulePath);
	for (;;) {
		const candidate = join(dir, "package.json");
		if (existsSync(candidate)) {
			try {
				const name = JSON.parse(readFileSync(candidate, "utf8")).name;
				if (typeof name === "string" && (expectedPackageName === void 0 || name === expectedPackageName)) {
					return { path: candidate, packageName: name };
				}
			} catch {
				/* 坏 manifest 继续往上 */
			}
		}
		const parent = dirname(dir);
		if (parent === dir) return void 0;
		dir = parent;
	}
}

// ── 1. 从 profile 目录解析本包（宿主 loader 的基准就是 profile 目录）────
const req = createRequire(pathToFileURL(join(profileDir, "package.json")).href);
let pkgJsonPath;
try {
	pkgJsonPath = req.resolve(`${PKG_NAME}/package.json`);
	check("从 profile 解析到本包 package.json", true, pkgJsonPath);
} catch (error) {
	check("从 profile 解析到本包 package.json", false, error.code ?? error.message);
}

if (pkgJsonPath) {
	// ── 2. package.json 里的 name 必须等于我们以为的包名（否则宿主不认）──
	const resolvedName = JSON.parse(readFileSync(pkgJsonPath, "utf8")).name;
	check("解析出的 manifest name 与包名一致", resolvedName === PKG_NAME, `${resolvedName} vs ${PKG_NAME}`);

	// ── 3. 复刻 locatePkgJson 全过程：解析 main → 向上找 manifest ──────
	let mainUrl;
	try {
		// 等价于 loader 解析 `@local/bionic-agent-panel` 主入口得到的 file URL
		const mainPath = req.resolve(PKG_NAME);
		mainUrl = pathToFileURL(mainPath).href;
		const located = nearestPackage(mainPath, PKG_NAME);
		check("宿主 locatePkgJson 能定位到 manifest", located !== void 0,
			located ? located.path : "向上找不到 name 匹配的 package.json");
	} catch (error) {
		check("宿主 locatePkgJson 能定位到 manifest", false, error.code ?? error.message);
	}

	// ── 4. 符号链接是否被解析成真实路径（说明宿主看到的是项目目录）────
	if (mainUrl) {
		const mainPath = fileURLToPath(mainUrl);
		const real = realpathSync(mainPath);
		const isSymlinked = real !== mainPath;
		console.log(`  主入口：${mainPath}`);
		console.log(`  真实路径：${real}`);
		check("真实路径仍在包内（manifest 向上可达）",
			dirname(dirname(real)) === PKG_DIR, `包根 ${PKG_DIR}`);
	}

	// ── 5. client 半必须可解析，且文件真的存在（宿主会读它）──────────────
	try {
		const clientPath = req.resolve(`${PKG_NAME}/client`);
		check("白名单 exports['./client'] 可解析", existsSync(clientPath), clientPath);
	} catch (error) {
		check("白名单 exports['./client'] 可解析", false, error.code ?? error.message);
	}
}

console.log(failed ? `\n${failed} 项失败` : "\n全部通过：符号链接形态不会让宿主找丢 manifest");
process.exit(failed ? 1 : 0);
