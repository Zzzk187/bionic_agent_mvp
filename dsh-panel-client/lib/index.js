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
 * bootTrace() 落盘用的目录。apply() 一开始就按 config 更新它，
 * 让痕迹和 admin_ui.log 永远落在同一个 projectDir 下。
 */
let traceProjectDir = null;

/**
 * 项目根的默认值：本包通常就放在 bionic-agent-mcp 目录下，
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
	/** bionic-agent-mcp 项目根目录（含 app/ 与 data/）。 */
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

/**
 * 端口上那个东西到底是不是我们。
 *
 * 只看「端口有人在听」是不够的：8899 是常见端口，用户的别的服务、另一个
 * Python 进程、甚至一次失败的旧启动都可能占着它。把它们当成自己的管理服务
 * 复用，表现就是面板打开后是别人的页面 / 一片乱码，而且没有任何报错。
 *
 * 判据用管理面板自己的接口：GET /api/stats 必须返回 {"ok":true,"data":{...}}
 * 且 data 里有 memories_total —— 这是本服务独有的形状。
 */
function identifyAdmin(port, timeoutMs = 1500) {
	return new Promise((done) => {
		let settled = false;
		const finish = (result) => {
			if (settled) return;
			settled = true;
			done(result);
		};
		const req = httpRequest(
			{
				host: "127.0.0.1",
				port,
				path: "/api/stats",
				method: "GET",
				headers: { host: `127.0.0.1:${port}`, accept: "application/json" },
				timeout: timeoutMs
			},
			(res) => {
				const chunks = [];
				res.on("data", (c) => chunks.push(c));
				res.on("end", () => {
					const text = Buffer.concat(chunks).toString("utf8");
					if (res.statusCode !== 200) {
						finish({ ours: false, reason: `HTTP ${res.statusCode}` });
						return;
					}
					let body;
					try {
						body = JSON.parse(text);
					} catch {
						finish({ ours: false, reason: "返回的不是 JSON（多半是别的服务）" });
						return;
					}
					const ours = body?.ok === true && body?.data !== undefined && "memories_total" in body.data;
					finish({ ours, reason: ours ? "是 bionic 管理服务" : "JSON 形状不匹配（不是本服务）" });
				});
			}
		);
		req.on("timeout", () => {
			req.destroy();
			finish({ ours: false, reason: "探测超时" });
		});
		req.on("error", (e) => finish({ ours: false, reason: `连不上：${e?.message ?? e}` }));
		req.end();
	});
}

/** 从 start 往后找一个没人听的端口；找不到返回 start。 */
async function pickFreePort(start, tries = 10) {
	for (let i = 0; i < tries; i += 1) {
		const port = start + i;
		if (port > 65535) break;
		if (!(await isListening(port))) return port;
	}
	return start;
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
	// 先把 projectDir 定下来，后面所有落盘（面板痕迹 / admin_ui.log）都读它，
	// 保证两者不会落到两个不同目录里去。
	traceProjectDir = resolve(String(merged.projectDir ?? DEFAULTS.projectDir));
	const configuredUrl = String(merged.adminUrl ?? DEFAULTS.adminUrl);
	const configuredPort = portOf(configuredUrl, merged.port);
	// 实际在用的地址。端口被别的程序占着时会改成旁边的端口，
	// 所以反代与探测都必须读这个可变值，而不是上面那个配置值。
	let adminUrl = configuredUrl;
	let port = configuredPort;
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
	let proxyFiber = null;
	if (proxyMountPath !== "") {
		// ctx.inject 返回的是一个子 fiber；路由挂在它上面，所以收的时候要
		// 显式 dispose 这个 fiber —— 只调 register 返回的 disposer 在没有
		// 「父 fiber 一起销毁」这条路径时（比如单测直接调 apply 的返回值）
		// 会把路由留在真 webServer 里。
		proxyFiber = ctx.inject(["webServer"], (inner) => {
			// 这个回调可能被触发不止一次（服务重新 provide 时）。只挂一次：
			// 第二次注册会被 webServer 判为 duplicate 而失败，那条失败路径
			// 返回 undefined，若直接赋给 disposeProxy 就会把第一次的有效
			// disposer 覆盖掉，最终 dispose 时摘不掉路由。
			if (disposeProxy === null) {
				disposeProxy = mountProxy(inner, proxyMountPath, () => adminUrl) ?? null;
			}
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
			// 端口有人听时，先问清楚"是不是我们自己"。
			// 直接复用会在别的服务占着 8899 时把面板接到别人页面上——
			// 现象是打开后一片乱码，且没有任何报错，最难查。
			if (await isListening(port)) {
				const who = await identifyAdmin(port);
				if (who.ours) {
					bootTrace(ctx, `${adminUrl} 已在运行，复用现有服务`);
					return;
				}
				const free = await pickFreePort(port + 1);
				bootTrace(ctx, `端口 ${port} 被别的程序占着（${who.reason}），改用 ${free}`, "warn");
				port = free;
				adminUrl = `http://127.0.0.1:${free}`;
			}
			launched = launchAdmin(merged, projectDir, port);
			if (launched.error !== undefined) {
				bootTrace(ctx, `启动管理服务失败：${launched.error?.message ?? launched.error}`, "warn");
				return;
			}
			const up = await waitUntilListening(port, merged.startTimeoutMs);
			if (up) {
				// 端口通了还要认一下形状：万一刚起来的那个进程被别人抢了先，
				// 或者 python 起来的是个错误的东西，这里能当场发现。
				const who = await identifyAdmin(port);
				if (who.ours) {
					bootTrace(ctx, `管理服务已就绪 ${adminUrl}（${pythonHint(launched)}）`);
				} else {
					bootTrace(ctx, `${adminUrl} 有响应但不是管理服务（${who.reason}），面板会显示空白`, "warn");
				}
			} else if (!disposed) {
				bootTrace(
					ctx,
					`管理服务未能就绪（${pythonHint(launched)}），面板会显示空白；日志见 ${launched.logPath ?? "data/admin_ui.log"}`,
					"warn"
				);
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
		// 先收路由再收 fiber：disposeProxy 是精确的，fiber 是兜底。
		disposeProxy?.();
		disposeProxy = null;
		try {
			proxyFiber?.dispose?.();
		} catch {
			/* 已经收掉了 */
		}
		proxyFiber = null;
	};
}

/**
 * 把启动痕迹追加到 <projectDir>/data/panel-boot.log。
 * 写盘失败绝不影响插件运行（吞掉异常）。
 *
 * 测试夹具会设 BIONIC_PANEL_NO_TRACE=1 跳过落盘 —— 否则 test/ 里跑的假宿主
 * 也会往这份日志里写，重启后就没法用「有没有痕迹」判断 DSH 到底加载没加载了。
 */
function bootTrace(ctx, message, level = "info") {
	try {
		// 日志只走这一条路：调用处不再单独 logger.info/warn，
		// 否则「端口被占」这类事会在终端里打两遍。
		const log = ctx.logger?.[level] ?? ctx.logger?.info;
		log?.(`[bionic-panel] ${message}`);
		if (process.env.BIONIC_PANEL_NO_TRACE === "1") return;
		const line = `${new Date().toISOString()} pid=${process.pid} ${message}\n`;
		// 用运行时解析出的 projectDir（config 里可能改过），而不是写死默认值 ——
		// 否则 config 改了 projectDir，痕迹和 admin_ui.log 会落到两个不同目录，
		// 排查时互相找不到。
		const dir = resolve(String(traceProjectDir ?? DEFAULTS.projectDir), "data");
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
function mountProxy(ctx, mountPath, adminUrlOf) {
	const prefix = String(mountPath ?? "").replace(/\/+$/, "");
	if (!prefix.startsWith("/")) return undefined;
	const webServer = ctx.webServer;
	if (webServer?.register === undefined) {
		bootTrace(ctx, `宿主没有 webServer，跳过同源挂载 ${prefix}（面板仍可用跨源地址打开）`);
		return undefined;
	}

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
		// 地址每次请求现取：端口可能因为被占用而改到别的号上，
		// 而这条路由是启动时就注册好的，不能把地址固化成注册那一刻的值。
		const adminUrl = adminUrlOf();
		let base;
		let origin;
		try {
			base = new URL(adminUrl);
			origin = base.origin;
		} catch (error) {
			res.writeHead(502, { "content-type": "application/json; charset=utf-8" });
			res.end(
				JSON.stringify({
					ok: false,
					message: `管理服务地址不合法（${adminUrl}）：${error?.message ?? error}`
				})
			);
			return;
		}
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
		// 注意：这里不能引用 handler 内部的 base/origin（它们在 handler 作用域里）。
		// 之前写成 `${origin}` 会在 register 成功之后抛 ReferenceError，
		// 被下面的 catch 吞掉并返回 undefined —— 路由挂上了、disposer 却丢了，
		// 现象就是「功能正常但卸载后路由摘不掉」，日志还会误报挂载失败。
		let shown = adminUrlOf();
		try {
			shown = new URL(shown).origin;
		} catch {
			/* 地址不合法时原样打出，交给 handler 报 502 */
		}
		bootTrace(ctx, `已挂同源路由 ${prefix} -> ${shown}`);
		return dispose;
	} catch (error) {
		// 重复注册（比如两个 DSH 实例共用一个 profile）会让宿主抛错 —— 不能因此
		// 让整个插件加载失败，面板退化成走跨源地址即可。
		ctx.logger?.warn?.(`[bionic-panel] 同源挂载 ${prefix} 失败：${error?.message ?? error}`);
		bootTrace(ctx, `同源挂载失败：${error?.message ?? error}`);
		return undefined;
	}
}