// Host half of the bionic-agent-panel bundle.
//
// 职责只有一件事：保证承载管理界面的本地服务（默认 http://127.0.0.1:8899）
// 在 DSH 运行期间是活的，卸插件时收掉自己起的进程。
//
// 为什么放在宿主半：client.js 跑在浏览器里，既不能 spawn 进程也不能读磁盘，
// 所以"服务是否在跑"这件事只能由 Node 侧负责。这样用户就不必手动
// `bionic-agent-admin`，也不会出现"侧边栏点开是空白"。
//
// 不接管已在跑的实例：端口已被监听时只复用、不重复启动，更不 kill
// （可能是用户自己起的、或另一个 DSH 实例起的），避免互相踩。
import { spawn } from "node:child_process";
import { connect } from "node:net";
import { request as httpRequest } from "node:http";
import { openSync, closeSync, mkdirSync, existsSync, appendFileSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const PKG_DIR = dirname(dirname(fileURLToPath(import.meta.url)));

/**
 * 项目根的默认值：本包通常就放在 bionic_agent_mvp 目录下，
 * 所以用「包的上一级」推导，而不是写死某个人的绝对路径——
 * 这样换台机器、换个目录 clone 下来都还能用。
 */
const DEFAULT_PROJECT_DIR = dirname(PKG_DIR);

/** 默认配置。cordis.patch.yml 的 config 段会覆盖这里的值。 */
const DEFAULTS = {
	/**
	 * 管理服务地址。宿主半用它探活、拉起、反代。
	 *
	 * 注意 client 半拿不到 config（dsh-client-modules 只按 id 加载），所以它那边
	 * 只能「优先走 mountPath 同源、失败退回 http://127.0.0.1:8899」。也就是说：
	 * 改这里到别的端口后，反代会跟着变（iframe 不受影响），但浏览器半的跨源
	 * 退路仍指向 8899 —— 只要 mountPath 没关，这不是问题。
	 */
	adminUrl: "http://127.0.0.1:8899",
	/** 监听端口，由 adminUrl 推导，一般不用手填。 */
	port: 8899,
	/** 是否在插件加载时自动拉起服务。 */
	autoStart: true,
	/** Python 解释器；留空则用内置候选（见 resolvePython）。 */
	pythonPath: "",
	/** bionic_agent_mvp 项目根目录（含 app/ 与 data/）。 */
	projectDir: DEFAULT_PROJECT_DIR,
	/** 服务启动后最多等多久（毫秒）。 */
	startTimeoutMs: 20000,
	/**
	 * 挂到 DSH 自身服务器上的同源前缀，空串 = 不挂。
	 *
	 * 为什么要这一层：iframe 跨源（DSH 在 A 端口、面板在 8899）能显示，但要求
	 * 浏览器既不拦 iframe 也不拦 XHR。挂成同源后两者都不依赖浏览器策略，
	 * 也顺带消灭了「client 半硬编码 8899 / host 半可配 adminUrl」的错位。
	 */
	mountPath: "/bionic-panel"
};

/** adminUrl → 端口。用于把配置归一成一处的真值。 */
function portOf(url, fallback) {
	try {
		const u = new URL(url);
		if (u.port) return Number(u.port);
		return u.protocol === "https:" ? 443 : 80;
	} catch {
		return fallback;
	}
}

/** 探测端口是否已有进程在听。resolve(true) = 有人在听。 */
function isListening(port, timeoutMs = 800) {
	return new Promise((done) => {
		const socket = connect({ host: "127.0.0.1", port });
		let settled = false;
		const finish = (up) => {
			if (settled) return;
			settled = true;
			socket.destroy();
			done(up);
		};
		socket.setTimeout(timeoutMs);
		socket.once("connect", () => finish(true));
		socket.once("timeout", () => finish(false));
		socket.once("error", () => finish(false));
	});
}

/** 轮询等端口起来，最多等 timeoutMs。 */
async function waitUntilListening(port, timeoutMs) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await isListening(port)) return true;
		await new Promise((r) => setTimeout(r, 400));
	}
	return false;
}

/**
 * 解析要用的 Python 解释器。
 *
 * 顺序：config.pythonPath（相对路径按项目根解析）→ 项目自带的 .venv →
 * 本机常见的 Miniforge/Conda 安装 → 最后交给 PATH 上的 python。
 * 都不存在时返回 "python"，让 spawn 的错误显式暴露，而不是静默不启动。
 */
function resolvePython(config, projectDir) {
	const configured = String(config.pythonPath ?? "").trim();
	if (configured) {
		return isAbsolute(configured) ? configured : resolve(projectDir, configured);
	}
	const candidates = [
		resolve(projectDir, ".venv", "Scripts", "python.exe"),
		resolve(projectDir, ".venv", "bin", "python"),
		"D:\Software\Dev\Miniforge\python.exe",
		join(process.env.USERPROFILE ?? "", "miniforge3", "python.exe"),
		join(process.env.USERPROFILE ?? "", "anaconda3", "python.exe"),
		join(process.env.LOCALAPPDATA ?? "", "Programs", "Python", "Python311", "python.exe")
	];
	for (const candidate of candidates) {
		try {
			if (candidate && existsSync(candidate)) return candidate;
		} catch {
			/* 无权限等：跳过这个候选 */
		}
	}
	return process.platform === "win32" ? "python" : "python3";
}

/**
 * 后台拉起管理服务，返回 { child, kill } 或 null。
 * stdout/stderr 落到临时日志，方便起不来时排查。
 */
function launchAdmin(config, projectDir, port) {
	const python = resolvePython(config, projectDir);
	let logFd;
	let logPath;
	try {
		mkdirSync(resolve(projectDir, "data"), { recursive: true });
		logPath = resolve(projectDir, "data", "admin_ui.log");
		logFd = openSync(logPath, "a");
	} catch {
		logFd = undefined;
	}
	let child;
	try {
		child = spawn(python, ["-m", "app.admin_ui", "--port", String(port)], {
			cwd: projectDir,
			env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
			stdio: logFd === undefined ? "ignore" : ["ignore", logFd, logFd],
			// 不弹控制台窗口（Windows），也不脱离宿主：宿主退出时能一并收掉。
			windowsHide: true,
			detached: false
		});
	} catch (error) {
		if (logFd !== undefined) closeSync(logFd);
		return { child: null, kill: () => {}, python, error };
	}
	child.on("error", () => {});
	// spawn 成功与进程跑起来是两回事：EADDRINUSE / import 失败都会立刻退出，
	// 那时 child 已有 exitCode —— 由调用方 waitUntilListening 兜底判断。
	if (logFd !== undefined) {
		child.once("exit", () => {
			try {
				closeSync(logFd);
			} catch {
				/* already closed */
			}
		});
	}
	return {
		child,
		python,
		logPath,
		kill: () => {
			try {
				if (child.exitCode === null && child.signalCode === null) child.kill();
			} catch {
				/* 进程已走 */
			}
		}
	};
}

/**
 * 宿主半入口。ctx 是 Cordis 的 host 上下文；config 由 cordis.patch.yml 提供。
 */
export function apply(ctx, config = {}) {
	const merged = { ...DEFAULTS, ...config };
	const adminUrl = String(merged.adminUrl ?? DEFAULTS.adminUrl);
	const port = portOf(adminUrl, merged.port);
	let launched = null;
	let disposed = false;

	// 落一份可读的启动痕迹：ctx.logger 只进宿主终端，重启后想确认
	// "插件到底加载没加载" 时看不到。这份文件由 verify-install.mjs 读。
	bootTrace(ctx, "apply 被调用（host 半已加载）");

	// 同源挂载：插件先挂路由，再拉服务。顺序有意如此 —— 路由是同步注册的，
	// 服务要几秒才起来；反过来会让「刚重启就点开面板」的第一屏打到没起来的端口。
	//
	// 用 ctx.inject 而不是把 "webServer" 写进导出的 inject：非 Web 宿主（纯 CLI）
	// 没有 webServer 时，写进 inject 会让整个插件永远不加载；用它只让挂载这一块
	// 缺席，服务拉起的逻辑照常跑。
	const proxyMountPath = String(merged.mountPath ?? "").trim();
	let disposeProxy = null;
	if (proxyMountPath !== "") {
		ctx.inject(["webServer"], (inner) => {
			disposeProxy = mountProxy(inner, proxyMountPath, adminUrl) ?? null;
			return () => {
				disposeProxy?.();
				disposeProxy = null;
			};
		});
	}

	ctx.effect(() => {
		if (merged.autoStart === false) {
			bootTrace(ctx, "autoStart=false，不拉起管理服务");
			return () => {};
		}
		const projectDir = resolve(String(merged.projectDir ?? DEFAULTS.projectDir));

		void (async () => {
			// 已有实例在跑 → 复用（可能是用户自己起的）。
			if (await isListening(port)) {
				ctx.logger?.info?.(`[bionic-panel] ${adminUrl} 已在运行，复用现有服务`);
				bootTrace(ctx, `复用已有服务 ${adminUrl}`);
				return;
			}
			launched = launchAdmin(merged, projectDir, port);
			if (launched.error !== undefined) {
				ctx.logger?.warn?.(`[bionic-panel] 启动管理服务失败：${launched.error?.message ?? launched.error}`);
				bootTrace(ctx, `启动失败：${launched.error?.message ?? launched.error}`);
				return;
			}
			const up = await waitUntilListening(port, merged.startTimeoutMs);
			if (up) {
				ctx.logger?.info?.(`[bionic-panel] 管理服务已就绪：${adminUrl}`);
				bootTrace(ctx, `管理服务已就绪 ${adminUrl}（${pythonHint(launched)}）`);
			} else if (!disposed) {
				ctx.logger?.warn?.(
					`[bionic-panel] 管理服务未能就绪（${pythonHint(launched)}），面板会显示空白；日志见 ${launched.logPath ?? "data/admin_ui.log"}`
				);
				bootTrace(ctx, `管理服务未能就绪（${pythonHint(launched)}），见 ${launched.logPath ?? "data/admin_ui.log"}`);
			}
		})();

		return () => {
			disposed = true;
			// 只收自己起的；别人起的实例不碰。
			launched?.kill();
			launched = null;
			bootTrace(ctx, "插件已卸载（dispose），自己起的服务已收");
		};
	}, "bionic-panel: admin service lifecycle");

	return () => {
		disposed = true;
		launched?.kill();
		launched = null;
		disposeProxy?.();
		disposeProxy = null;
	};
}

/**
 * 把启动痕迹追加到 <projectDir>/data/panel-boot.log。
 * 写盘失败绝不影响插件运行（吞掉异常）。
 *
 * 测试夹具会设 BIONIC_PANEL_NO_TRACE=1 跳过落盘 —— 否则 test/ 里跑的假宿主
 * 也会往这份日志里写，重启后就没法用「有没有痕迹」判断 DSH 到底加载没加载了。
 */
function bootTrace(ctx, message) {
	try {
		ctx.logger?.info?.(`[bionic-panel] ${message}`);
		if (process.env.BIONIC_PANEL_NO_TRACE === "1") return;
		const line = `${new Date().toISOString()} pid=${process.pid} ${message}\n`;
		const dir = resolve(String(DEFAULTS.projectDir), "data");
		if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
		appendFileSync(resolve(dir, "panel-boot.log"), line, "utf8");
	} catch {
		/* 痕迹写不了也不影响主流程 */
	}
}

function pythonHint(launched) {
	return launched?.python ? `python=${launched.python}` : "python 路径未解析";
}

/**
 * 在 DSH 自己的服务器上开一个前缀路由，把请求反向代理给本地管理服务。
 *
 * 为什么不是「让面板直接跨源调 8899」：跨源要同时赌三件事——浏览器不拦
 * iframe、不拦 XHR、CORS 配置对。任何一件不成立，面板就是白屏或空列表，
 * 而报错还只出现在用户浏览器的 console 里。挂成同源后这些都变成不变量。
 *
 * 返回 disposer；webServer 缺失（非 Web 宿主）时返回 undefined，不报错。
 */
function mountProxy(ctx, mountPath, adminUrl) {
	const prefix = String(mountPath ?? "").replace(/\/+$/, "");
	if (!prefix.startsWith("/")) return undefined;
	const webServer = ctx.webServer;
	if (webServer?.register === undefined) {
		bootTrace(ctx, `宿主没有 webServer，跳过同源挂载 ${prefix}（面板仍可用跨源地址打开）`);
		return undefined;
	}
	const base = new URL(adminUrl);
	const origin = base.origin;

	// 同源反代让面板和 DSH 共用一个源，也就意味着**任何能打到 DSH 端口的网页**
	// 都能对我们的写接口发请求（读响应被同源策略挡住，但 POST/DELETE 会被执行）。
	// 记忆库的删除/归档是不可逆的，所以写方法一律走宿主自己的 Host/Origin fence。
	//
	// 只 fence 写方法而不是全部：GET 没有跨站后果（响应读不到），而把 GET 也
	// 卡在 cookie 判定上，会在 authority 变化的边缘情况下把能用的面板判死。
	// fence 自身抛错不拦请求 —— 安全组件故障不该让本地面板不可用。
	const guardWrite = (req, res) => {
		const method = String(req.method ?? "GET").toUpperCase();
		if (method === "GET" || method === "HEAD" || method === "OPTIONS") return true;
		let connection;
		try {
			connection = ctx.get?.("connection");
		} catch {
			return true;
		}
		if (connection?.requestRejection === undefined) return true;
		let rejection;
		try {
			rejection = connection.requestRejection(req);
		} catch {
			return true;
		}
		if (rejection === undefined) return true;
		res.writeHead(rejection, {
			"content-type": "application/json; charset=utf-8",
			"cache-control": "no-store"
		});
		res.end(
			JSON.stringify({
				ok: false,
				message:
					rejection === 403
						? "请求被拒绝：来源不是 DSH 页面（防跨站写入）"
						: "未登录 DSH，不能写管理面板（请从 DSH 窗口里操作）",
				hint: "面板读操作不受影响；写操作需从 DSH 自己的窗口发起"
			})
		);
		return false;
	};

	const handler = (req, res) => {
		if (!guardWrite(req, res)) return;
		const incoming = new URL(req.url ?? "/", "http://dsh.internal");
		let rest = incoming.pathname.slice(prefix.length);
		if (rest === "" || rest === "/") rest = "/";
		if (!rest.startsWith("/")) rest = `/${rest}`;
		const target = new URL(rest + incoming.search, origin);

		// 逐跳头不能透传；Host 必须换成目标源的，否则 uvicorn 侧校验会不认。
		const headers = { ...req.headers, host: base.host };
		delete headers["accept-encoding"];

		const upstream = httpRequest(
			{
				protocol: target.protocol,
				hostname: target.hostname,
				port: target.port || (target.protocol === "https:" ? 443 : 80),
				path: `${target.pathname}${target.search}`,
				method: req.method,
				headers
			},
			(up) => {
				res.writeHead(up.statusCode ?? 502, up.headers);
				up.pipe(res);
			}
		);
		upstream.on("error", (error) => {
			if (res.headersSent) {
				res.destroy();
				return;
			}
			res.writeHead(502, { "content-type": "application/json; charset=utf-8" });
			res.end(
				JSON.stringify({
					ok: false,
					message: `管理服务不可达（${origin}）：${error?.message ?? error}`,
					hint: "宿主半本应自动拉起它；起不来时看 data/admin_ui.log"
				})
			);
		});
		req.pipe(upstream);
	};

	try {
		const dispose = webServer.register({ kind: "prefix", path: prefix, handler });
		bootTrace(ctx, `已挂同源路由 ${prefix} -> ${origin}`);
		return dispose;
	} catch (error) {
		// 重复注册（比如两个 DSH 实例共用一个 profile）会让宿主抛错 —— 不能因此
		// 让整个插件加载失败，面板退化成走跨源地址即可。
		ctx.logger?.warn?.(`[bionic-panel] 同源挂载 ${prefix} 失败：${error?.message ?? error}`);
		bootTrace(ctx, `同源挂载失败：${error?.message ?? error}`);
		return undefined;
	}
}