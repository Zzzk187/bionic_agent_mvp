// 最小 semver 范围判断，只覆盖本项目会写的几种形状：
//   >=x.y.z / >x.y.z / <=x.y.z / <x.y.z / ^x.y.z / ~x.y.z / 精确值 / 用 || 连接多个
//
// 为什么自己写而不装 semver：
//   1. verify-install.mjs 要能在「没有任何 node_modules 的干净机器」上直接跑
//      （分发包里就没有 node_modules），装依赖会让自检在最需要它的时候不可用。
//   2. 这里要判的是宿主对 dsh.engines.dsh 的要求，判错会导致「明明能被加载却被
//      自检判成不满足」，所以必须能被单测覆盖而不是黑盒。
//
// 单独成文件是为了让 verify-install.mjs 与 test/all.mjs 共用同一份实现——
// 之前 all.mjs 是用 indexOf("function satisfies(") 从 verify-install.mjs 里
// 抠源码出来 eval 的，文件一旦被编辑器转成 CRLF 就抠不到（锚点里写的是 \n），
// 于是单测静默变成 [FAIL] 找不到 satisfies()。那是测试自身的脆弱，不是被测代码的问题。

/** 解析 x.y.z 或 x.y.z-pre */
function parse(v) {
	const m = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/.exec(String(v).trim());
	if (!m) return null;
	return { major: +m[1], minor: +m[2], patch: +m[3], pre: m[4] ?? "" };
}

/**
 * 预发布标识按 semver 规则逐段比较：
 *   纯数字段按数值比；数字段 < 字母段；前面全同则段数少的更小；
 *   没有预发布 = 正式版，大于同号的任何预发布。
 * 整串当字符串比会把 rc.10 判得比 rc.3 小（"rc.10" < "rc.3" 逐字符成立）。
 */
function cmpPre(a, b) {
	if (a === b) return 0;
	if (a === "") return 1;
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
			return nx ? -1 : 1;
		} else if (x !== y) {
			return x < y ? -1 : 1;
		}
	}
	return 0;
}

function cmp(a, b) {
	for (const key of ["major", "minor", "patch"]) {
		if (a[key] !== b[key]) return a[key] < b[key] ? -1 : 1;
	}
	return cmpPre(a.pre, b.pre);
}

/** 上界（不含）：^ 允许最左非零位不变，~ 允许次版本位不变。 */
function upperBound(op, b) {
	if (op === "^") {
		if (b.major > 0) return { major: b.major + 1, minor: 0, patch: 0, pre: "" };
		if (b.minor > 0) return { major: b.major, minor: b.minor + 1, patch: 0, pre: "" };
		return { major: b.major, minor: b.minor, patch: b.patch + 1, pre: "" };
	}
	if (op === "~") return { major: b.major, minor: b.minor + 1, patch: 0, pre: "" };
	return null;
}

/** version 是否满足 range */
export function satisfies(version, range) {
	const v = parse(version);
	if (!v) return false;
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
