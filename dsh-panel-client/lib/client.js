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

		const zh = {
			"entry.label": "记忆/任务",
			"entry.tooltip": "bionic-agent 记忆与任务管理面板",
			"panel.title": "bionic-agent 管理面板"
		};
		const en = {
			"entry.label": "Memory / Tasks",
			"entry.tooltip": "bionic-agent memory & task management panel",
			"panel.title": "bionic-agent admin panel"
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

		function BionicPanelPage() {
			return react_jsx_runtime.jsx("div", {
				className: "bionic-agent-panel-view",
				"data-dsh-plugin": PANEL_ID,
				style: { position: "absolute", inset: 0, border: "none", overflow: "hidden", background: "#fff" },
				children: react_jsx_runtime.jsx("iframe", {
					src: DEFAULT_ADMIN_URL,
					title: tt("panel.title"),
					style: { width: "100%", height: "100%", border: "none", display: "block" },
					allow: "clipboard-read; clipboard-write"
				})
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
