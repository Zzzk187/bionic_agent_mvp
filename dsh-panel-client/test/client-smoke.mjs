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
		// setState 支持函数式更新（React 真实语义），不然 setX(n => n + 1) 会把函数本身存进去。
		const set = (next) => {
			reactStub.__store[index] = typeof next === "function" ? next(reactStub.__store[index]) : next;
		};
		return [reactStub.__store[index], set];
	},
	// useEffect 必须遵守"deps 没变就不重跑"这条语义。
	// 桩若每次 render 都执行，effect 里的局部计数（重试次数）会被反复清零，
	// 测出来的行为就和真实 React 不一样了 —— 之前就因此误判过自愈逻辑。
	useEffect: (fn, deps) => {
		const index = reactStub.__effectCursor++;
		const prev = reactStub.__effectDeps[index];
		const same = prev !== undefined && Array.isArray(deps) && deps.length === prev.length
			&& deps.every((d, i) => d === prev[i]);
		if (same) return;
		reactStub.__effectDeps[index] = deps;
		const cleanup = fn();
		if (typeof cleanup === "function") reactStub.__cleanups.push(cleanup);
	},
	useMemo: (f) => f(),
	useRef: (v) => ({ current: v }),
	useSyncExternalStore: (sub, get) => get(),
	__store: [],
	__cursor: 0,
	__effectCursor: 0,
	__effectDeps: [],
	__cleanups: [],
	__fetched: [],
	__serviceUp: true,
	// 模拟"同源挂载没生效、请求被 DSH 自己的回退处理器 200 收走"：
	// 只看 resp.ok 会误判成都就绪，必须靠 JSON 判定。
	__mountBroken: false,
	__beginRender() {
		reactStub.__cursor = 0;
		reactStub.__effectCursor = 0;
	},
	// 模拟"组件彻底卸载再挂载"：连同 effect 依赖一起清空。
	__resetAll() {
		reactStub.__store = [];
		reactStub.__cursor = 0;
		reactStub.__effectCursor = 0;
		reactStub.__effectDeps = [];
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

// 页面会 fetch(`${MOUNT_PATH}/api/stats` 与 `${FALLBACK_ADMIN_URL}/api/stats`) 探测服务是否活着。
// 桩的行为由 reactStub.__serviceUp 控制，以便分别测「服务在」与「服务不在」两条路径。
// __htmlOn 用来模拟"同源路由没挂上、DSH 的回退处理器返回了 200 的 HTML"——
// 只看 resp.ok 会把它当成服务就绪，于是 iframe 嵌进 DSH 界面自己。
const fetchStub = async (url) => {
	const target = String(url);
	reactStub.__fetched.push(target);
	const isMount = target.startsWith("/bionic-panel");
	const up = reactStub.__serviceUp && !(isMount && reactStub.__mountBroken);
	if (!up) {
		if (isMount && reactStub.__mountBroken && reactStub.__serviceUp) {
			// 同源路由被别的处理器接走：200，但不是我们的 JSON
			return {
				ok: true,
				status: 200,
				json: async () => {
					throw new SyntaxError("Unexpected token '<'");
				}
			};
		}
		throw new Error("connect ECONNREFUSED 127.0.0.1:8899");
	}
	return { ok: true, status: 200, json: async () => ({ ok: true, data: {} }) };
};

// 执行 client.js
// 面板页会用 setTimeout 做重试退避，vm 的干净上下文里没有这些，要显式给。
const sandbox = {
	window: fakeWindow, console, URL, Symbol, Object, Array, JSON, Promise,
	setTimeout, clearTimeout, Date, fetch: fetchStub
};
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
	iframe?.type === "iframe" && (String(iframe?.props?.src ?? "").startsWith("/bionic-panel/") || /^http:\/\/127\.0\.0\.1:[0-9]+/.test(String(iframe?.props?.src ?? ""))),
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

// 第二轮：服务不在 -> iframe 仍然渲染（这是设计契约），顶部出现探测中的说明条。
//
// 注意别在这里等 PROBE_TIMEOUT_MS（30 秒）：那是给真实冷启动留的预算，测试里
// 只需验证"第一次探测失败后进入 probing、并且还在重试"，就够了。
reactStub.__serviceUp = false;
reactStub.__resetAll();
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
	iframe?.type === "iframe" && (String(iframe?.props?.src ?? "").startsWith("/bionic-panel/") || /^http:\/\/127\.0\.0\.1:[0-9]+/.test(String(iframe?.props?.src ?? ""))),
	`${iframe?.type} ${iframe?.props?.src}`);
check("服务不可用时进入 probing 而不是直接报死",
	page?.props?.["data-dsh-bionic-state"] === "probing",
	String(page?.props?.["data-dsh-bionic-state"]));
check("服务不可用时出现说明条",
	warnNode?.props?.["data-dsh-bionic-warn"] === "probing",
	String(warnNode?.props?.["data-dsh-bionic-warn"]));
// probing 阶段的说明条只报"正在连接 + 地址"；失败详情留到 down 才展示，
// 免得用户盯着每 800ms 变一次的报错看。
check("探测中的说明条带出服务地址",
	pageText.includes("8899") && pageText.includes("probing"),
	pageText.slice(0, 120));

// 第三轮：服务恢复 -> 应该自愈成 ready，并且 iframe 换 key 重新加载
// 第三轮：服务恢复。**不能重置组件状态** —— 自愈逻辑依赖重试计数跨轮次累加，
// 重置就变成一个全新挂载，永远测不到 attempt>1 那条分支。
reactStub.__serviceUp = true;
// 等下一次重试 tick（PROBE_INTERVAL_MS=800ms）落地。
await new Promise((r) => setTimeout(r, 900));
reactStub.__beginRender();
page = mainReg.Component(face);
nodes = flatten(page);
iframe = nodes.find((n) => n?.type === "iframe");
check("服务恢复后自愈为 ready",
	page?.props?.["data-dsh-bionic-state"] === "ready",
	String(page?.props?.["data-dsh-bionic-state"]));
check("自愈时 iframe 换了 key 以真正重新加载",
	typeof iframe?.props?.key === "number" && iframe.props.key > 0,
	String(iframe?.props?.key));

// 第四轮：同源路由被别的处理器 200 收走（返回 HTML 而非我们的 JSON）。
// 这是最容易骗过探测的一种"假就绪"：只看 resp.ok 就会把 iframe 指向
// /bionic-panel/ 而实际嵌进 DSH 界面自己，且不再重试。必须靠 JSON 判定后
// 退回跨源地址，面板才不会白屏。
reactStub.__resetAll();
reactStub.__fetched.length = 0;
reactStub.__serviceUp = true;
reactStub.__mountBroken = true;
reactStub.__beginRender();
page = mainReg.Component(face);
await new Promise((r) => setTimeout(r, 30));
reactStub.__beginRender();
page = mainReg.Component(face);
nodes = flatten(page);
iframe = nodes.find((n) => n?.type === "iframe");
check("同源返回 HTML 时判定为未就绪并退回跨源地址",
	page?.props?.["data-dsh-bionic-state"] === "ready" &&
		/^http:\/\/127\.0\.0\.1:[0-9]+/.test(String(iframe?.props?.src ?? "")),
	`state=${page?.props?.["data-dsh-bionic-state"]} src=${iframe?.props?.src}`);
reactStub.__mountBroken = false;

if (failed) {
	console.error(`FAIL: ${failed} 项渲染检查未通过`);
	process.exit(1);
}

// 渲染图标
const iconReg = registrations.find((r) => r.decl.name === "sidebar.panellist");
const icon = iconReg.Component({ size: 16 });
console.log("PASS: 侧边栏图标渲染 ->", icon?.type, "| data-dsh-panel-entry =", icon?.props?.["data-dsh-panel-entry"]);

// 回归护栏：main 槽的父级不是我们的定位祖先，容器一旦写 position:absolute
// 就会相对 viewport 铺满、把整个 DSH 界面遮住（真实踩过的故障，只能重启客户端）。
// 所以这里明确禁止绝对定位，只认「撑满父容器」的写法。
const pageReg = registrations.find((r) => r.decl.name === "main");
const layoutRoot = pageReg.Component({});
const pageStyle = layoutRoot?.props?.style ?? {};
const absoluteKeys = ["position", "inset", "top", "left", "right", "bottom", "zIndex"];
const offenders = absoluteKeys.filter((k) => k in pageStyle);
if (offenders.length > 0) {
	console.error(`FAIL: 面板容器用了绝对定位相关属性（${offenders.join(", ")}），会铺满整个窗口遮住界面`);
	process.exit(1);
}
if (pageStyle.height !== "100%") {
	console.error(`FAIL: 面板容器应 height:100% 撑满父容器，实际 ${JSON.stringify(pageStyle.height)}`);
	process.exit(1);
}
console.log("PASS: 面板容器用撑满父容器的布局，未使用绝对定位 —", JSON.stringify({
	height: pageStyle.height, minHeight: pageStyle.minHeight, overflow: pageStyle.overflow
}));

console.log("\n=== 全部通过：client.js 契约与运行时行为正确 ===");
