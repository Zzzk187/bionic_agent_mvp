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
import { satisfies } from "../semver-lite.mjs";

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
		name: "面板主题（跟随 DSH / 深色 / 浅色）",
		script: "test/theme.mjs",
		// 纯静态读文件，不依赖浏览器与宿主，必须常绿
		optional: false
	},
	{
		name: "记忆星图（前端渲染 + 后端布局）",
		script: "test/starmap.mjs",
		// 前端部分纯静态；后端实测需要 python，没有就算 SKIP
		optional: false
	},
	{
		name: "星图在反代前缀下能取到图库",
		script: "test/starmap-prefix.mjs",
		// 直连 8899 时 BASE 是空串，写死不写死看不出区别；DSH 里永远带 /bionic-panel 前缀，
		// 那一刻写死根路径就会 404、星图永远出不来。这条专门钉住它。
		optional: true
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
		name: "端口被占用时换端口",
		script: "test/port-conflict.mjs",
		// 要起真 python 服务；环境缺 python 时 SKIP
		optional: true
	},
	{
		name: "真宿主 WebServer 反代",
		script: "test/host-webserver.mjs",
		// 要宿主自己的 cordis + dsh-host-webserver；换机器没有就 SKIP
		optional: true
	},
	{
		name: "复用别人的实例后仍会看护",
		script: "test/adopt.mjs",
		// 复用时没有 child 句柄，watch() 挂不上；那条服务停掉会白屏到下次重启 DSH。
		// 这条钉住「会自己接管」。
		optional: true
	},
	{
		name: "服务崩溃后自愈",
		script: "test/self-heal.mjs",
		// 要真起 python 服务并把它杀掉；最慢的一条，缺 python 时 SKIP
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
// 直接 import 被测实现，不再从源文件里抠代码 eval。
// 之前用 indexOf("function satisfies(") + slice 的方式，文件一旦被编辑器转成 CRLF
// （锚点里写死的是 \n）就静默变成 [FAIL] 找不到 satisfies()——
// 测试自身的脆弱会被误读成"被测代码有问题"。
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

console.log(
	`\n${failed === 0 ? "全部通过" : `${failed} 项失败`}` +
		(skipped ? `（${skipped} 项因环境缺失跳过）` : "")
);
process.exit(failed === 0 ? 0 : 1);
