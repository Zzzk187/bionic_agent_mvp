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

// 极简 react / jsx-runtime 桩
const reactStub = {
	createElement: (type, props, ...children) => ({ type, props: { ...props, children } }),
	useState: (v) => [typeof v === "function" ? v() : v, () => {}],
	useEffect: () => {},
	useMemo: (f) => f(),
	useRef: (v) => ({ current: v }),
	useSyncExternalStore: (sub, get) => get()
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

// 执行 client.js
const sandbox = { window: fakeWindow, console, URL, Symbol, Object, Array, JSON };
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

// 渲染页面组件，确认 iframe 指向正确
const mainReg = registrations.find((r) => r.decl.name === "main");
const face = typeof mainReg.decl.inject === "function" ? mainReg.decl.inject() : {};
const page = mainReg.Component(face);
const iframe = page?.props?.children;
console.log("PASS: main 页面渲染 ->", iframe?.type, iframe?.props?.src);

if (iframe?.type !== "iframe" || !/^http:\/\/127\.0\.0\.1:\d+/.test(String(iframe?.props?.src ?? ""))) {
	console.error("FAIL: main 页面没有渲染出指向 localhost 的 iframe");
	process.exit(1);
}

// 渲染图标
const iconReg = registrations.find((r) => r.decl.name === "sidebar.panellist");
const icon = iconReg.Component({ size: 16 });
console.log("PASS: 侧边栏图标渲染 ->", icon?.type, "| data-dsh-panel-entry =", icon?.props?.["data-dsh-panel-entry"]);

console.log("\n=== 全部通过：client.js 契约与运行时行为正确 ===");
