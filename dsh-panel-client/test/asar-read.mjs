// 从 app.asar 里读某个 npm 包的 package.json —— 只读，不修改任何东西。
//
// 为什么需要：宿主判断插件兼容性用的是它自己的运行时版本（dsh-app-boot 的 version），
// 这个值只存在于安装目录的 app.asar 内。profile 的 node_modules 里没有
// @deepseek-ai/dsh（它是安装器提供的），所以只能从 asar 里挖。
//
// asar 是 Chromium 的 Pickle 格式，结构：
//   [u32=4][u32=headerPickleLen] [headerPickle: u32=jsonLen, u32=strLen, json...] [文件内容...]
// header 里的 json 是整棵目录树，每个文件节点带 {offset, size}（相对数据区起点）。
import { openSync, readSync, closeSync, existsSync } from "node:fs";

/** 按 asar 的 Pickle 约定读出 header JSON。 */
function readAsarHeader(asarPath) {
	const fd = openSync(asarPath, "r");
	try {
		const sizeBuf = Buffer.alloc(8);
		readSync(fd, sizeBuf, 0, 8, 0);
		const headerPickleLen = sizeBuf.readUInt32LE(4);
		const headerBuf = Buffer.alloc(headerPickleLen);
		readSync(fd, headerBuf, 0, headerPickleLen, 8);
		// headerPickle 本身也是 Pickle：[u32 payloadSize][u32 strLen][json 字节]
		// payloadSize = strLen + 4，所以真正的 JSON 长度在 offset 4；读 offset 0 会多读 4 字节。
		const jsonLen = headerBuf.readUInt32LE(4);
		const json = headerBuf.toString("utf8", 8, 8 + jsonLen);
		return { fd, header: JSON.parse(json), dataOffset: 8 + headerPickleLen };
	} catch (error) {
		closeSync(fd);
		throw error;
	}
}

/** 在目录树里按路径段逐层找节点。 */
function walk(header, segments) {
	let node = header;
	for (const segment of segments) {
		if (!node || node.files === undefined) return undefined;
		node = node.files[segment];
		if (!node) return undefined;
	}
	return node;
}

/**
 * 读 asar 内某个文件的文本内容。
 * @param asarPath app.asar 绝对路径
 * @param segments 路径段数组，如 ["node_modules","@deepseek-ai","dsh-app-boot","package.json"]
 * @returns 文件文本，或 undefined（不存在）
 */
export function readAsarText(asarPath, segments) {
	if (!existsSync(asarPath)) return undefined;
	const { fd, header, dataOffset } = readAsarHeader(asarPath);
	try {
		const node = walk(header, segments);
		if (!node || node.files !== undefined) return undefined; // 不存在或是目录
		const size = Number(node.size);
		if (!Number.isFinite(size) || size <= 0) return "";
		const buf = Buffer.alloc(size);
		readSync(fd, buf, 0, size, dataOffset + Number(node.offset));
		return buf.toString("utf8");
	} finally {
		closeSync(fd);
	}
}

/** 找 app.asar：依次看环境变量与常见安装位置。 */
export function findAsar(candidates = []) {
	const guesses = [
		...candidates,
		process.env.DSH_APP_ASAR,
		"D:/Software/Dev/DeepSeekharness/resources/app.asar",
		joinIf(process.env.LOCALAPPDATA, "Programs/DeepSeek Harness/resources/app.asar"),
		joinIf(process.env.PROGRAMFILES, "DeepSeek Harness/resources/app.asar")
	].filter(Boolean);
	return guesses.find((p) => existsSync(p));
}

function joinIf(base, rest) {
	return base ? `${base.replace(/[\\/]+$/, "")}/${rest}` : undefined;
}

/**
 * 读出 DSH 运行时版本（dsh-app-boot 的 version 就是宿主 self 报的运行时版本）。
 * @returns 版本字符串，或 undefined
 */
export function readDshRuntimeVersion(asarPath) {
	// 实测该包在 asar 里的位置是 dsh/node_modules/@deepseek-ai/dsh-app-boot
	// （asar 根下还有一层 dsh/，是 Harness 自己的运行时目录）。
	const candidates = [
		["dsh", "node_modules", "@deepseek-ai", "dsh-app-boot", "package.json"],
		["node_modules", "@deepseek-ai", "dsh-app-boot", "package.json"]
	];
	for (const segments of candidates) {
		const text = readAsarText(asarPath, segments);
		if (text === undefined) continue;
		try {
			const version = JSON.parse(text).version;
			if (version) return version;
		} catch {
			/* 换下一个候选 */
		}
	}
	return undefined;
}
