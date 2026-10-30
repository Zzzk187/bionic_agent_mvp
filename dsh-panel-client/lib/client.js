// Browser half of the bionic-agent-panel bundle.
// Renders a sidebar row + a center-column page that simply embeds the FastAPI
// admin UI (default http://127.0.0.1:8899) in an iframe. The pattern mirrors
// @linxin666/dsh-client-ui-skill-explorer (sidebar.panellist + main slots,
// a PanelController driving layout.selectPanel, locale + L2 semantic attrs).
window.__ModuleLoader__.load({
	id: "@local/bionic-agent-panel",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		// 浏览器侧的自证标记：模块一被求值就写，早于任何可能抛错的逻辑。
		// 宿主若因版本/声明不匹配静默跳过插件，这段代码根本不会执行，
		// 于是「有标记=client 半已加载」「无标记=根本没被求值」，可区分。
		try {
			window.__bionicPanelLoaded = (window.__bionicPanelLoaded || 0) + 1;
			if (document.documentElement) {
				document.documentElement.setAttribute("data-bionic-panel", "loaded");
			}
		} catch (_) { /* 标记失败不影响主流程 */ }
		let react = require("react");
		let react_jsx_runtime = require("react/jsx-runtime");

		//#region constants + i18n
		const PANEL_ID = "bionic-agent-panel";
		const PANEL_ORDER = 30;
		const NS = "dsh-bionic-agent-panel";
		const DEFAULT_ADMIN_URL = "http://127.0.0.1:8899";
		// 探测节奏：宿主半冷启动要 spawn python + 起 uvicorn，实测约 5~15 秒。
		// 探得太急会一直刷状态，探得太稀又会让用户对着空白等。
		const PROBE_INTERVAL_MS = 800;
		const PROBE_TIMEOUT_MS = 30000;

		const zh = {
			"entry.label": "记忆/任务",
			"entry.tooltip": "bionic-agent 记忆与任务管理面板",
			"panel.title": "bionic-agent 管理面板",
			"panel.probing": "正在连接管理服务…",
			"panel.downTitle": "管理服务没有响应",
			"panel.downHint": "插件会在 DSH 启动时自动拉起本机管理服务；没起来通常是下面几种原因，处理完点这个面板重新打开即可：",
			"panel.downUrl": "服务地址",
			"panel.downReason": "具体原因",
			"panel.downLog": "若 Python 或依赖缺失，请在 bionic-agent 项目目录执行 pip install -r requirements.txt；日志见 data/admin_ui.log"
		};
		const en = {
			"entry.label": "Memory / Tasks",
			"entry.tooltip": "bionic-agent memory & task management panel",
			"panel.title": "bionic-agent admin panel",
			"panel.probing": "Connecting to the admin service…",
			"panel.downTitle": "The admin service is not responding",
			"panel.downHint": "The plugin starts the local admin service when DSH boots. When it is down, one of these is usually the cause; fix it and reopen this panel:",
			"panel.downUrl": "Service",
			"panel.downReason": "Reason",
			"panel.downLog": "If Python or its dependencies are missing, run pip install -r requirements.txt in the bionic-agent project; the log is data/admin_ui.log"
		};

		let runtimeTranslate = null;
		function setRuntimeTranslate(fn) {
			runtimeTranslate = fn;
		}
		function tt(key) {
			if (runtimeTranslate) {
				try {
					return runtimeTranslate(key);
				} catch {
					/* fall through */
				}
			}
			return key;
		}
		//#endregion

		//#region panel controller
		var PanelController = class {
			deps;
			panelOpen = false;
			listeners = new Set();
			snapshot = { panelOpen: false };
			constructor(deps = {}) {
				this.deps = deps;
			}
			getSnapshot() {
				return this.snapshot;
			}
			subscribe(fn) {
				this.listeners.add(fn);
				return () => {
					this.listeners.delete(fn);
				};
			}
			open() {
				if (this.panelOpen) return;
				this.panelOpen = true;
				this.commit();
				this.selectPanel(PANEL_ID);
			}
			close() {
				if (!this.panelOpen) return;
				this.panelOpen = false;
				this.commit();
				this.selectPanel(null);
			}
			toggle() {
				if (this.panelOpen) this.close();
				else this.open();
			}
			syncPanelSelection(panelId) {
				const open = panelId === PANEL_ID;
				if (open === this.panelOpen) return;
				this.panelOpen = open;
				this.commit();
			}
			commit() {
				this.snapshot = { panelOpen: this.panelOpen };
				for (const fn of [...this.listeners]) fn();
			}
			selectPanel(panelId) {
				try {
					this.deps.panel?.select(panelId);
				} catch {
					/* no-op when layout is unavailable */
				}
			}
		};
		//#endregion

		//#region components
		function BionicPanelIcon({ size }) {
			return react_jsx_runtime.jsxs("svg", {
				"data-dsh-panel-entry": PANEL_ID,
				viewBox: "0 0 16 16",
				width: size,
				height: size,
				fill: "none",
				stroke: "currentColor",
				strokeWidth: "1.3",
				strokeLinecap: "round",
				strokeLinejoin: "round",
				"aria-hidden": "true",
				children: [
					react_jsx_runtime.jsx("path", { d: "M8 2C6 2 4.5 3 4.5 3v10s1.5 1 3.5 1 3.5-1 3.5-1V3s-1.5-1-3.5-1z" }),
					react_jsx_runtime.jsx("path", { d: "M4.5 3s1.5 1 3.5 1 3.5-1 3.5-1" }),
					react_jsx_runtime.jsx("path", { d: "M4.5 8s1.5 1 3.5 1 3.5-1 3.5-1" })
				]
			});
		}

		// 面板页：iframe 里是 8899 的 FastAPI 管理界面。
		//
		// 设计取舍（重要）：iframe 一律渲染，探测只用来叠加一条说明条。
		// 反过来做（探不到就不渲染 iframe）看着更"严谨"，实际更脆：探测走的是
		// 跨源 fetch，只要 CORS/拦截/版本有任何一处不对，明明能用的面板就会被
		// 自己判死、整个变成错误页。所以让 iframe 自己加载，探测失败只是在顶部
		// 加一条可读的原因 —— 最坏情况退化成"能用的面板 + 一条多余提示"，
		// 而不是"完全打不开"。
		function BionicPanelPage() {
			// status: "probing" 服务还没应答 / "ready" 已应答 / "down" 等超时还没应答
			const [status, setStatus] = react.useState("probing");
			const [detail, setDetail] = react.useState("");
			// iframe 的重挂载计数。服务是宿主半 spawn 出来的，冷启动要十几秒；
			// 如果 iframe 先挂上、服务后起来，浏览器已经把那次请求判失败，
			// iframe 会停在错误页不会自愈。所以服务晚到时改 key 让它重新加载。
			const [reloadKey, setReloadKey] = react.useState(0);
			react.useEffect(() => {
				let cancelled = false;
				let timer = null;
				let attempt = 0;
				// 宿主半最多等 startTimeoutMs（20s）才判定失败，这里探得更久一点，
				// 保证"服务起来了但探测已经放弃"这种尴尬不会发生。
				const deadline = Date.now() + PROBE_TIMEOUT_MS;
				const tick = async () => {
					if (cancelled) return;
					attempt += 1;
					try {
						const response = await fetch(`${DEFAULT_ADMIN_URL}/api/stats`, { method: "GET" });
						if (cancelled) return;
						if (response.ok) {
							setStatus("ready");
							setDetail("");
							// 只有"之前探失败过"才需要重挂 iframe；一路顺利就别动它。
							if (attempt > 1) setReloadKey((n) => n + 1);
							return;
						}
						setDetail(`HTTP ${response.status}`);
					} catch (error) {
						if (cancelled) return;
						setDetail(String(error?.message ?? error));
					}
					if (Date.now() >= deadline) {
						setStatus("down");
						return;
					}
					setStatus("probing");
					timer = setTimeout(tick, PROBE_INTERVAL_MS);
				};
				tick();
				return () => {
					cancelled = true;
					if (timer !== null) clearTimeout(timer);
				};
			}, []);
			const children = [];
			if (status !== "ready") {
				children.push(react_jsx_runtime.jsxs("div", {
					"data-dsh-bionic-warn": status,
					style: {
						flex: "0 0 auto", padding: "8px 12px",
						background: status === "down" ? "#fff8e5" : "#f2f6ff",
						borderBottom: "1px solid " + (status === "down" ? "#f0d9a8" : "#cfe0ff"),
						color: status === "down" ? "#6b4e00" : "#1f3a68",
						font: "12px/1.6 ui-sans-serif, system-ui, \"Segoe UI\", sans-serif"
					},
					children: status === "down"
						? [
							react_jsx_runtime.jsx("strong", { children: tt("panel.downTitle") }),
							` — ${tt("panel.downUrl")} ${DEFAULT_ADMIN_URL}（${tt("panel.downReason")}：${detail}）。`,
							` ${tt("panel.downLog")}`
						]
						: [
							react_jsx_runtime.jsx("strong", { children: tt("panel.probing") }),
							` — ${DEFAULT_ADMIN_URL}`
						]
				}));
			}
			children.push(react_jsx_runtime.jsx("iframe", {
				key: reloadKey,
				src: DEFAULT_ADMIN_URL,
				title: tt("panel.title"),
				style: { flex: "1 1 auto", width: "100%", border: "none", display: "block", minHeight: 0 },
				allow: "clipboard-read; clipboard-write"
			}));
			return react_jsx_runtime.jsx("div", {
				className: "bionic-agent-panel-view",
				"data-dsh-plugin": PANEL_ID,
				"data-dsh-bionic-state": status,
				style: {
					position: "absolute", inset: 0, border: "none", overflow: "hidden",
					background: "#fff", display: "flex", flexDirection: "column"
				},
				children
			});
		}
		//#endregion

		//#region registration
		function registerPanel(ctx, controller) {
			const slots = ctx.slots;
			const disposers = [];
			disposers.push(
				slots.inject("sidebar.panellist", () =>
					slots.register(
						{
							name: "sidebar.panellist",
							id: PANEL_ID,
							order: PANEL_ORDER,
							label: () => tt("entry.label")
						},
						BionicPanelIcon
					)
				)
			);
			disposers.push(
				slots.inject("main", () =>
					slots.register(
						{
							name: "main",
							key: PANEL_ID,
							inject: () => ({})
						},
						BionicPanelPage
					)
				)
			);
			return () => {
				for (const dispose of disposers.splice(0)) dispose();
			};
		}
		//#endregion

		//#region apply
		function apply(ctx) {
			ctx.effect(
				() => {
					try {
						return ctx.locale.register(NS, { zh, en });
					} catch {
						return () => {};
					}
				},
				"bionic-panel: dictionaries"
			);
			try {
				setRuntimeTranslate(ctx.locale.bind(NS));
			} catch {
				/* locale service absent */
			}
			const controller = new PanelController({
				panel: {
					select: (panelId) => {
						ctx.get?.("layout")?.selectPanel?.(panelId);
					}
				}
			});
			const disposers = [];
			try {
				disposers.push(registerPanel(ctx, controller));
				const layoutFace = ctx.get?.("layout");
				if (layoutFace?.panelInfo !== void 0) {
					const sync = () => {
						const active = layoutFace.panelInfo.getSnapshot().activePanelId;
						controller.syncPanelSelection(active === PANEL_ID ? PANEL_ID : null);
					};
					sync();
					disposers.push(layoutFace.panelInfo.subscribe(sync));
				}
			} catch (error) {
				console.warn("[bionic-panel] panel registration failed:", error);
			}
			ctx.effect(
				() => () => {
					for (const dispose of disposers.splice(0)) dispose();
				},
				"bionic-panel: ui mounts"
			);
		}
		//#endregion

		exports.apply = apply;
		exports.inject = ["slots", "locale", "layout"];
		return module.exports;
	}
});
