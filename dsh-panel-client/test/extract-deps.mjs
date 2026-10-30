// 从 app.asar 里按需解出「宿主自己的那几个包」，供需要真实 cordis /
// 真 WebServer 的测试使用。
//
// 为什么需要它：`test/host-cordis.mjs`、`test/host-webserver.mjs`、
// `test/layer-config.mjs`、`test/self-heal.mjs` 都要 import 宿主自己的
// `@deepseek-ai/cordis` / `dsh-app-boot` / `dsh-host-webserver`，这些包只存在于
// DSH 安装目录的 app.asar 里（app.asar.unpacked 下没有打包进去的 JS 部分）。
// 换台机器、或者有人清过 C:/tmp，这几条测试就会 SKIP —— 而 SKIP 不会让 CI 红，
// 于是最关键的「真宿主路径」长期没人验证。
//
// 用法：
//   node dsh-panel-client/test/extract-deps.mjs            # 解到默认位置
//   node dsh-panel-client/test/extract-deps.mjs --out DIR  # 指定输出目录
//   DEPS_OUT=... node ...                                  # 或环境变量
//
// 解完把打印出的 DEPS_DIR 传给测试即可（三处测试都认 DEPS_DIR）。
import { openSync, readSync, closeSync, existsSync, mkdirSync, writeFileSync, statSync } from "node:fs";
import { dirname, join, resolve, basename } from "node:path";
import { findAsar } from "./asar-read.mjs";

/** 读 asar header（Pickle：先 u32 长度，再 u32 JSON 长度）。 */
function readHeader(asarPath) {
	const fd = openSync(asarPath, "r");
	try {
		const sizeBuf = Buffer.alloc(8);
		readSync(fd, sizeBuf, 0, 8, 0);
		const headerPickleLen = sizeBuf.readUInt32LE(4);
		const headerBuf = Buffer.alloc(headerPickleLen);
		readSync(fd, headerBuf, 0, headerPickleLen, 8);
		// headerPickle 自己也是 Pickle：[u32 payloadSize][u32 strLen][json]
		// JSON 长度在 offset 4；读 offset 0 会多读 4 字节而解析失败。
		const jsonLen = headerBuf.readUInt32LE(4);
		return {
			fd,
			header: JSON.parse(headerBuf.toString("utf8", 8, 8 + jsonLen)),
			dataOffset: 8 + headerPickleLen
		};
	} catch (error) {
		closeSync(fd);
		throw error;
	}
}

/** 在目录树里按路径段找节点。 */
function walk(header, segments) {
	let node = header;
	for (const segment of segments) {
		if (!node || node.files === undefined) return undefined;
		node = node.files[segment];
		if (!node) return undefined;
	}
	return node;
}

/** 把 asar 内某个目录整棵写到磁盘，返回写入的文件数。 */
function extractDir(fd, dataOffset, node, targetDir, counter) {
	mkdirSync(targetDir, { recursive: true });
	for (const [name, child] of Object.entries(node.files ?? {})) {
		// 包内嵌套的 node_modules 一律不拷：只拷它自己的一半，依赖由顶层那份提供。
		// 拷了反而更糟 —— 嵌套副本缺它自己的依赖，Node 先去嵌套里找就报
		// MODULE_NOT_FOUND（negotiator 就是这么炸的），而顶层那份是完整的。
		if (name === "node_modules") continue;
		const childPath = join(targetDir, name);
		if (child.files !== undefined) {
			extractDir(fd, dataOffset, child, childPath, counter);
			continue;
		}
		// 有些条目是符号链接/特殊节点，没有 offset，跳过即可
		const size = Number(child.size);
		if (!Number.isFinite(size) || size < 0 || child.offset === undefined) continue;
		if (size === 0) {
			writeFileSync(childPath, "");
			counter.files += 1;
			continue;
		}
		const buf = Buffer.alloc(size);
		readSync(fd, buf, 0, size, dataOffset + Number(child.offset));
		writeFileSync(childPath, buf);
		counter.files += 1;
		counter.bytes += size;
	}
	return counter;
}

/** 把 "a/b" 或 "@scope/pkg" 拆成路径段。 */
function segmentsOf(name) {
	return name.startsWith("@") ? name.split("/") : [name];
}

/**
 * 找一个包在 asar 里的位置，返回 {segs, rel}。
 *
 * `rel` 是相对 node_modules 的路径段 —— 也就是这个包真实的包名分段。
 * 必须用它而不是传进来的名字：ROOTS 里写的是裸 "cordis"，而它在 asar 里
 * 实际是 `@deepseek-ai/cordis`；按裸名拼目标路径会把它解到
 * `deps/@deepseek-ai` 之外，测试仍然找不到（踩过）。
 *
 * 依赖名形态有两种：`@deepseek-ai/cosmokit`（带 scope）与 `ajv`、`semver`
 * （不带）。它们不一定住在同一层 —— 宿主自己的包集中在
 * `dsh/node_modules/@deepseek-ai/` 下，而第三方平铺在 `dsh/node_modules/`。
 * 所以按「先 scope 内、再顶层」的顺序试，取第一个命中的。
 */
function findPkgSegments(header, name) {
	const tail = [...segmentsOf(name), "package.json"];
	const bases = [
		["dsh", "node_modules", "@deepseek-ai"], // 宿主自家包
		["dsh", "node_modules"], // 顶层第三方
		["node_modules"] // asar 根（保险）
	];
	for (const base of bases) {
		const node = walk(header, [...base, ...tail]);
		if (node && node.files === undefined) {
			return { segs: [...base, ...segmentsOf(name)], rel: relativeName(base, name) };
		}
	}
	return undefined;
}

/** base 若以 @ 开头（scope 目录），真实包名要带上 scope 段。 */
function relativeName(base, name) {
	const scoped = base[base.length - 1].startsWith("@") && !name.startsWith("@");
	return scoped ? [base[base.length - 1], ...segmentsOf(name)] : segmentsOf(name);
}

/** 读 asar 内某个包 package.json 节点（按 findPkgSegments 定位）。 */
function readPkgJson(header, fd, dataOffset, segments) {
	const node = walk(header, [...segments, "package.json"]);
	if (!node || node.files !== undefined) return undefined;
	try {
		return JSON.parse(readNodeText(fd, dataOffset, node));
	} catch {
		return undefined;
	}
}

function readNodeText(fd, dataOffset, node) {
	const size = Number(node.size);
	if (!Number.isFinite(size) || size <= 0) return "";
	const buf = Buffer.alloc(size);
	readSync(fd, buf, 0, size, dataOffset + Number(node.offset));
	return buf.toString("utf8");
}

/**
 * 解析一个包的完整依赖闭包。
 *
 * 除了 dependencies，也跟进 peerDependencies —— 直觉上 peer 是「宿主提供」的，
 * 但 DSH 把自家一堆 @deepseek-ai/* 都写成 peer（如 dsh-app-boot 的
 * `@deepseek-ai/cordis-plugin-loader`），实际又都打包在同一个 asar 里。
 * 只跟 dependencies 会在 import 时报 ERR_MODULE_NOT_FOUND（踩过）。
 * optionalDependencies 不跟：它们本来就允许缺失。
 */
function closure(header, fd, dataOffset, roots) {
	const seen = new Set();
	const queue = [...roots];
	const order = [];
	while (queue.length > 0) {
		const name = queue.shift();
		if (seen.has(name)) continue;
		seen.add(name);
		const segs = findPkgSegments(header, name);
		if (!segs) {
			console.log(`  [warn] asar 里没有 ${name}（跳过）`);
			continue;
		}
		order.push({ name, ...segs });
		const pkg = readPkgJson(header, fd, dataOffset, segs.segs);
		const deps = {
			...(pkg?.dependencies ?? {}),
			...(pkg?.peerDependencies ?? {})
		};
		for (const dep of Object.keys(deps)) {
			if (!seen.has(dep)) queue.push(dep);
		}
	}
	return order;
}

const ROOTS = ["cordis", "dsh-app-boot", "dsh-host-webserver"];
// 这几个包都住在宿主自己的 @deepseek-ai scope 下（不是顶层 node_modules）
const SCOPE = "@deepseek-ai";
const NM_SEGMENTS = ["dsh", "node_modules", SCOPE];

const wantDir = resolve(
	process.argv.includes("--out")
		? process.argv[process.argv.indexOf("--out") + 1]
		: process.env.DEPS_OUT || "C:/tmp/dsh_asar_extract/app/dsh/node_modules/@deepseek-ai"
);

// 解包根**必须**是一个字面名为 node_modules 的目录：Node 的解析规则是
// 从包内逐级向上找 node_modules，`@deepseek-ai/cordis/lib` 里的
// require("ajv") 会依次试 …/cordis/node_modules、…/@deepseek-ai/node_modules、
// …/node_modules。如果根目录叫别的名字（比如 deps_probe），ajv 就永远找不到。
const nodeModules = basename(wantDir) === "@deepseek-ai" ? dirname(wantDir) : wantDir;
const outRoot = basename(nodeModules) === "node_modules" ? nodeModules : join(nodeModules, "node_modules");
const outDir = join(outRoot, "@deepseek-ai");

const asarPath = findAsar();
if (!asarPath) {
	console.error("[FAIL] 找不到 app.asar：用 DSH_APP_ASAR 指定安装目录里的 resources/app.asar");
	process.exit(1);
}
console.log("asar:", asarPath);
console.log("输出:", outDir);

const { fd, header, dataOffset } = readHeader(asarPath);
const counter = { files: 0, bytes: 0 };
try {
	const wanted = closure(header, fd, dataOffset, ROOTS);
	console.log(`\n需要 ${wanted.length} 个包：${wanted.map((w) => w.name).join(", ")}\n`);
	// 目标路径用真实包名分段（rel，带 scope），不是传进来的裸名。
	// 依赖解析靠 outRoot 本身叫 node_modules，所以这里不能把整棵树挪进 @deepseek-ai。
	for (const { name, segs, rel } of wanted) {
		const node = walk(header, segs);
		if (!node || node.files === undefined) continue;
		const target = join(outRoot, ...rel);
		// 已经解过的跳过，省时间（重复运行是安全的）
		if (existsSync(join(target, "package.json"))) {
			console.log(`  [=] ${name} 已存在，跳过`);
			continue;
		}
		const before = counter.files;
		extractDir(fd, dataOffset, node, target, counter);
		console.log(`  [+] ${name} — ${counter.files - before} 个文件`);
	}
} finally {
	closeSync(fd);
}

console.log(`\n完成：${counter.files} 个文件，${(counter.bytes / 1048576).toFixed(1)} MB`);
console.log(`\n把下面这行交给测试：\n  DEPS_DIR=${outDir}`);
if (!existsSync(join(outDir, "cordis", "lib", "index.js"))) {
	console.error("\n[FAIL] 解完仍找不到 cordis/lib/index.js，检查 asar 内的目录层级是否变了");
	process.exit(1);
}
