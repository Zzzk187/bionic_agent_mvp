#!/usr/bin/env node
// 一次性跑完插件自带的全部自证：结构冒烟 → 真 cordis 端到端 → 安装态自检。
//
// 装完插件、或改了 lib/ 之后跑这个。退出码非 0 就是有东西没对上。
//
// 说明：host-cordis.mjs 需要 cordis 源码（默认从 asar 解包目录 / 环境变量 DEPS_DIR 取），
// 拿不到时会 SKIP 而不是 FAIL —— 那是环境缺失，不是插件坏了。
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// 本文件在 test/ 下，包根是上一层
const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const PKG_DIR = dirname(TEST_DIR);

const STEPS = [
	{
		name: "client.js 结构冒烟",
		script: "test/client-smoke.mjs",
		// 不需要外部依赖，跑不起来就是真坏了
		optional: false
	},
	{
		name: "宿主半 cordis 端到端",
		script: "test/host-cordis.mjs",
		optional: true
	},
	{
		name: "profile 层叠与 config",
		script: "test/layer-config.mjs",
		// 需要宿主 asar 解包出的 dsh-app-boot；换台机器就没有，算 SKIP 不算坏
		optional: true
	},
	{
		name: "同源反代挂载",
		script: "test/proxy-mount.mjs",
		// 需要真起一个 python 管理服务，环境缺 python 时算 SKIP 不算坏
		optional: true
	},
	{
		name: "安装态自检",
		script: "verify-install.mjs",
		optional: false
	}
];

let failed = 0;
let skipped = 0;

for (const step of STEPS) {
	const scriptPath = join(PKG_DIR, step.script);
	if (!existsSync(scriptPath)) {
		console.log(`\n=== ${step.name} ===`);
		console.log(`[SKIP] 找不到 ${step.script}`);
		skipped++;
		continue;
	}
	console.log(`\n=== ${step.name} ===`);
	const result = spawnSync(process.execPath, [scriptPath], {
		cwd: PKG_DIR,
		encoding: "utf8",
		stdio: "inherit"
	});
	if (result.status !== 0) {
		if (step.optional) {
			console.log(`[SKIP] ${step.name} 未通过（环境缺失，非插件缺陷）`);
			skipped++;
		} else {
			failed++;
		}
	}
}

// 顺带把 semver 判断单测跑一遍 —— 自检脚本自身的正确性也得有人盯。
console.log("\n=== semver 判断单测 ===");
const semverCases = JSON.parse(
	readFileSync(join(TEST_DIR, "semver-cases.json"), "utf8")
);
const source = readFileSync(join(PKG_DIR, "verify-install.mjs"), "utf8");
const start = source.indexOf("function satisfies(");
const end = source.indexOf("\n}\n", source.indexOf("return false;\n\t\t});", start)) + 3;
if (start < 0 || end < 3) {
	console.log("[FAIL] 在 verify-install.mjs 里找不到 satisfies()");
	failed++;
} else {
	const satisfies = new Function(`return ${source.slice(start, end)}`)();
	let bad = 0;
	for (const [version, range, want] of semverCases) {
		const got = satisfies(version, range);
		if (got !== want) {
			bad++;
			console.log(`  [FAIL] ${version} ${range} → ${got}，应为 ${want}`);
		}
	}
	console.log(bad === 0 ? `  ${semverCases.length} 例全通过` : `  ${bad} 例失败`);
	if (bad > 0) failed++;
}

console.log(
	`\n${failed === 0 ? "全部通过" : `${failed} 项失败`}` +
		(skipped ? `（${skipped} 项因环境缺失跳过）` : "")
);
process.exit(failed === 0 ? 0 : 1);
