// 离线跑一遍 client.js 的 factory + apply()：
// 用假的 window.__ModuleLoader__ / require / ctx，验证它真的能注册
// sidebar.panellist 与 main 两个 slot，且不抛异常。
// 这能在重启 DSH 之前抓到 client.js 的运行时错误。
import { readFileSync } from "node:fs";
import vm from "node:vm";

const clientSource = readFileSync(new URL("../lib/client.js", import.meta.url), "utf-8");

let loaded = null;
const fakeWindow = {
	__ModuleLoader__: {
		load: (def) => {
			loaded = def;
		}
	}
};

// 极简 react / jsx-runtime 桩。
//
// useEffect 必须真的同步执行一次：面板页现在先探服务、探到才渲染 iframe，
// 桩若把 effect 丢掉，组件就永远停在 probing，测不到 iframe 那条路径。
// setState 也真的写入，这样 effect 里 setState 之后能反映到返回值上。
const reactStub = {
	createElement: (type, props, ...children) => ({ type, props: { ...props, children } }),
	// 持久状态槽：按调用序号取值，重渲染时能拿到 setState 之后的新值。
	// 每次 render 前调用 __beginRender() 重置游标，模拟 React 的 hook 顺序。
	useState: (v) => {
		const index = reactStub.__cursor++;
		if (reactStub.__store[index] === undefined) {
			reactStub.__store[index] = typeof v === "function" ? v() : v;
		}
		const set = (next) => { reactStub.__store[index] = next; };
		return [reactStub.__store[index], set];
	},
	useEffect: (fn) => {
		const cleanup = fn();
		if (typeof cleanup === "function") reactStub.__cleanups.push(cleanup);
	},
	useMemo: (f) => f(),
	useRef: (v) => ({ current: v }),
	useSyncExternalStore: (sub, get) => get(),
	__store: [],
	__cursor: 0,
	__cleanups: [],
	__fetched: [],
	__serviceUp: true,
	__beginRender() {
		reactStub.__cursor = 0;
	}
};
const jsxStub = {
	jsx: (type, props) => ({ type, props }),
	jsxs: (type, props) => ({ type, props }),
	Fragment: "Fragment"
};
const requireStub = (spec) => {
	if (spec === "react") return reactStub;
	if (spec === "react/jsx-runtime") return jsxStub;
	throw new Error("unexpected require: " + spec);
};

// 页面会 fetch(`${DEFAULT_ADMIN_URL}/api/stats`) 探测服务是否活着。
// 桩的行为由 reactStub.__serviceUp 控制，以便分别测「服务在」与「服务不在」两条路径。
const fetchStub = async (url) => {
	reactStub.__fetched.push(String(url));
	if (reactStub.__serviceUp) return { ok: true, status: 200 };
	throw new Error("connect ECONNREFUSED 127.0.0.1:8899");
};

// 执行 client.js
const sandbox = { window: fakeWindow, console, URL, Symbol, Object, Array, JSON, fetch: fetchStub };
vm.createContext(sandbox);
new vm.Script(clientSource, { filename: "client.js" }).runInContext(sandbox);

if (loaded === null) {
	console.error("FAIL: __ModuleLoader__.load 没有被调用");
	process.exit(1);
}
console.log("PASS: ModuleLoader.load 被调用, id =", loaded.id);

const exported = loaded.factory(requireStub);
console.log("PASS: factory 返回, apply =", typeof exported.apply, "| inject =", JSON.stringify(exported.inject));

// 假 ctx：记录注册了什么
const registrations = [];
const injections = [];
const localeRegister = [];
let effectLabels = [];

const slotsFace = {
	inject(name, fn) {
		injections.push(name);
		const dispose = fn();
		return () => (dispose ? dispose() : undefined);
	},
	register(decl, Component) {
		registrations.push({ decl, Component });
		return () => {};
	}
};

const layoutFace = {
	selectPanel: (id) => {
		layoutFace._selected = id;
	},
	panelInfo: {
		getSnapshot: () => ({ activePanelId: layoutFace._selected ?? null }),
		subscribe: () => () => {}
	}
};

const ctx = {
	slots: slotsFace,
	locale: {
		register(ns, dicts) {
			localeRegister.push({ ns, keys: Object.keys(dicts.zh ?? {}) });
			return () => {};
		},
		bind: (ns) => (key) => `${ns}:${key}`
	},
	get: (name) => (name === "layout" ? layoutFace : undefined),
	effect(fn, label) {
		effectLabels.push(label);
		const dispose = fn();
		return dispose;
	},
	logger: { info() {}, warn() {} }
};

try {
	exported.apply(ctx);
} catch (error) {
	console.error("FAIL: apply() 抛异常 ->", error && error.stack ? error.stack : error);
	process.exit(1);
}

console.log("PASS: apply() 无异常");
console.log("  locale 注册:", JSON.stringify(localeRegister));
console.log("  slot inject:", JSON.stringify(injections));
console.log("  ctx.effect:", JSON.stringify(effectLabels));

const seatNames = registrations.map((r) => `${r.decl.name}${r.decl.id ? "#" + r.decl.id : r.decl.key ? "#" + r.decl.key : ""}`);
console.log("  slot 注册:", JSON.stringify(seatNames));

const hasSidebar = registrations.some((r) => r.decl.name === "sidebar.panellist");
const hasMain = registrations.some((r) => r.decl.name === "main");
if (!hasSidebar || !hasMain) {
	console.error(`FAIL: 缺少注册 sidebar=${hasSidebar} main=${hasMain}`);
	process.exit(1);
}

// ── 渲染页面组件 ───────────────────────────────────────────────────
//
// 面板页的设计是「iframe 一律渲染，探测失败只叠加说明条」。所以两条路径都必须
// 断言 iframe 在场 —— 这正是设计契约本身，不是偷懒：如果哪天有人把它改成
// 「探不到就不渲染」，用户会在服务偶发抖动时看到一片空白，而不是能用的面板。
const mainReg = registrations.find((r) => r.decl.name === "main");
const face = typeof mainReg.decl.inject === "function" ? mainReg.decl.inject() : {};

let failed = 0;
const check = (name, ok, detail = "") => {
	console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${detail ? " — " + detail : ""}`);
	if (!ok) failed++;
};

// 页面会把 iframe 放进 children 数组（有告警条时数组里多一个 div），
// 这里统一摊平去找。
const flatten = (node) => {
	const out = [];
	const walk = (n) => {
		if (n === null || n === undefined || typeof n === "boolean") return;
		if (Array.isArray(n)) return n.forEach(walk);
		out.push(n);
		if (n?.props?.children !== undefined) walk(n.props.children);
	};
	walk(node);
	return out;
};

// 第一轮：服务在 -> 渲染 iframe，且不该出现告警条
reactStub.__beginRender();
let page = mainReg.Component(face);
await new Promise((r) => setTimeout(r, 20)); // 让 effect 里的 fetch 探测落地
reactStub.__beginRender();
page = mainReg.Component(face);
let nodes = flatten(page);
let iframe = nodes.find((n) => n?.type === "iframe");
check("服务在线时渲染 iframe 并指向 localhost",
	iframe?.type === "iframe" && /^http:\/\/127\.0\.0\.1:\d+/.test(String(iframe?.props?.src ?? "")),
	`${iframe?.type} ${iframe?.props?.src}`);
check("服务在线时状态为 ready",
	page?.props?.["data-dsh-bionic-state"] === "ready",
	String(page?.props?.["data-dsh-bionic-state"]));
check("服务在线时不出告警条",
	!nodes.some((n) => n?.props?.["data-dsh-bionic-warn"]),
	String(nodes.filter((n) => n?.props?.["data-dsh-bionic-warn"]).length));
check("确实探测过 /api/stats",
	reactStub.__fetched.some((u) => u.endsWith("/api/stats")),
	JSON.stringify(reactStub.__fetched));

// 第二轮：服务不在 -> iframe 仍然渲染（这是设计契约），另外多一条说明条
reactStub.__serviceUp = false;
reactStub.__store = [];
reactStub.__cursor = 0;
reactStub.__fetched.length = 0;
reactStub.__beginRender();
page = mainReg.Component(face);
await new Promise((r) => setTimeout(r, 20));
reactStub.__beginRender();
page = mainReg.Component(face);
nodes = flatten(page);
iframe = nodes.find((n) => n?.type === "iframe");
const warnNode = nodes.find((n) => n?.props?.["data-dsh-bionic-warn"]);
const pageText = JSON.stringify(nodes);
check("服务不可用时 iframe 仍然渲染（不把面板判死）",
	iframe?.type === "iframe" && /^http:\/\/127\.0\.0\.1:\d+/.test(String(iframe?.props?.src ?? "")),
	`${iframe?.type} ${iframe?.props?.src}`);
check("服务不可用时状态为 warn",
	page?.props?.["data-dsh-bionic-state"] === "warn",
	String(page?.props?.["data-dsh-bionic-state"]));
check("服务不可用时出现说明条",
	warnNode?.props?.["data-dsh-bionic-warn"] === "service",
	String(warnNode?.props?.["data-dsh-bionic-warn"]));
check("说明条带出服务地址与失败原因",
	pageText.includes("8899") && pageText.includes("ECONNREFUSED"),
	pageText.slice(0, 160));

if (failed) {
	console.error(`FAIL: ${failed} 项渲染检查未通过`);
	process.exit(1);
}

// 渲染图标
const iconReg = registrations.find((r) => r.decl.name === "sidebar.panellist");
const icon = iconReg.Component({ size: 16 });
console.log("PASS: 侧边栏图标渲染 ->", icon?.type, "| data-dsh-panel-entry =", icon?.props?.["data-dsh-panel-entry"]);

console.log("\n=== 全部通过：client.js 契约与运行时行为正确 ===");
