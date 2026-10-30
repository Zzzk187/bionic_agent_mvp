// 测试用的「宿主依赖解析器」：找到 @deepseek-ai/* 那几个包在哪。
//
// 背景：四条测试要 import 宿主自己的 `@deepseek-ai/cordis`、`dsh-app-boot`、
// `dsh-host-webserver`。这些包只存在于 DSH 安装目录的 app.asar 里，项目本身不带
// 依赖（这是插件该有的样子）。以前各自的默认值写死成 `C:/tmp/dsh_asar_extract/...`
// —— 那是我手工解包的临时目录，换台机器就没有，测试集体 SKIP，而 SKIP 不会
// 让任何东西变红，于是「真宿主路径」长期没人验证。
//
// 现在的顺序：
//   1. DEPS_DIR 环境变量（CI / 特殊布局用）
//   2. C:/tmp/dsh_asar_extract/... 已经解好的（快，不动它）
//   3. 都找不到 → 用 test/extract-deps.mjs 从 app.asar 现场解到
//      C:/tmp/dsh_asar_extract/...，再返回
//   4. 连 app.asar 都找不到 → 返回 undefined，调用方 SKIP
import { existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** 默认解包位置（与 extract-deps.mjs 的默认值一致）。 */
export const DEFAULT_DEPS = "C:/tmp/dsh_asar_extract/app/dsh/node_modules/@deepseek-ai";

/** 这个 deps 目录看起来完整吗（只查三条测试真正 import 的入口）。 */
export function depsUsable(deps) {
	if (!deps) return false;
	return (
		existsSync(join(deps, "cordis", "lib", "index.js")) &&
		existsSync(join(deps, "dsh-app-boot", "lib", "index.js")) &&
		existsSync(join(deps, "dsh-host-webserver", "lib", "index.js"))
	);
}

/**
 * 解析宿主依赖目录；必要时现场从 app.asar 解包。
 * @returns 可用的依赖目录，或 undefined（没装 DSH / asar 结构变了）
 */
export function resolveDeps() {
	const explicit = process.env.DEPS_DIR;
	if (explicit) {
		if (depsUsable(explicit)) return explicit;
		// 显式指定了却不可用，说明是配置错误，别悄悄改用别的目录
		console.log(`[warn] DEPS_DIR=${explicit} 不完整（缺 cordis/dsh-app-boot/dsh-host-webserver）`);
		return undefined;
	}
	if (depsUsable(DEFAULT_DEPS)) return DEFAULT_DEPS;

	// 现场解包：解包脚本自己会读 app.asar，找不到就退出非零
	try {
		const here = dirname(fileURLToPath(import.meta.url));
		const script = join(here, "extract-deps.mjs");
		console.log("[info] 宿主依赖尚未解包，正在从 app.asar 现场解出（首次约 3 秒）…");
		execFileSync(process.execPath, [script, "--out", DEFAULT_DEPS], { stdio: "inherit" });
	} catch (error) {
		console.log(`[warn] 自动解包失败：${error?.message ?? error}`);
		return undefined;
	}
	return depsUsable(DEFAULT_DEPS) ? DEFAULT_DEPS : undefined;
}

/** 供测试统一用的 import URL 构造（Windows 盘符路径要转成 file://）。 */
export function toUrl(p) {
	return new URL(`file:///${p.replace(/[\\/]+/g, "/")}`).href;
}
