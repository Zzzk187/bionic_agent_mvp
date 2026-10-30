// 验证「服务崩了能自愈」：拉起来 → 杀掉子进程 → 观察是否自动重拉。
//
// 这条用例存在的理由：宿主半原来只在 apply 时启动一次服务，
// python 崩掉后要等 DSH 重启才有面板，而用户不知道要重启。
//
// 踩过的坑（别改回去）：一开始用「在目标端口上 listen 一个临时 server」来判断
// 端口空不空。但宿主半自己的 isListening() 也是连一下看通不通 —— 我的探测
// server 正好在那个瞬间占着端口并接受连接，于是插件判成"已被别人占用"，
// 跑去 8897 起服务，测试却盯着 8896 永远等不到。探活要探 HTTP，不要抢端口。
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execSync, execFileSync } from "node:child_process";

process.env.BIONIC_PANEL_NO_TRACE = "1";

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const PKG_DIR = dirname(TEST_DIR);
const PROJECT_DIR = dirname(PKG_DIR);
const DEPS = process.env.DEPS_DIR || "C:/tmp/dsh_asar_extract/app/dsh/node_modules/@deepseek-ai";
const toUrl = (p) => new URL(`file:///${p.replace(/\\/g, "/")}`).href;

const PORT = 8896;
const PY = process.env.BIONIC_PYTHON || "D:/Software/Dev/Miniforge/python.exe";

const results = [];
const check = (name, ok, detail = "") => {
	results.push({ name, ok });
	console.log(`${ok ? "[PASS]" : "[FAIL]"} ${name}${detail ? " — " + detail : ""}`);
};

// 探 HTTP，不抢端口。
const bootOf = async (ms = 2000) => {
	try {
		const res = await fetch(`http://127.0.0.1:${PORT}/api/stats`, { signal: AbortSignal.timeout(ms) });
		const body = await res.json();
		return body?.ok === true ? String(body.data?.boot ?? "") : "";
	} catch {
		return "";
	}
};

const waitBoot = async (timeoutMs, prev = null) => {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const boot = await bootOf();
		if (boot !== "" && boot !== prev) return boot;
		await new Promise((r) => setTimeout(r, 400));
	}
	return "";
};

const waitDown = async (timeoutMs) => {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if ((await bootOf(1200)) === "") return true;
		await new Promise((r) => setTimeout(r, 300));
	}
	return false;
};

const tmpDbDir = mkdtempSync(join(tmpdir(), "bionic-heal-test-"));
process.env.BIONIC_DB_PATH = join(tmpDbDir, "agent.db");

const { Context } = await import(toUrl(join(DEPS, "cordis", "lib", "index.js")));
const plugin = await import(toUrl(join(PKG_DIR, "lib", "index.js")));

const app = new Context();
const logs = [];
for (const level of ["info", "warn", "error", "debug"]) {
	app.logger = { ...(app.logger ?? {}), [level]: (m) => logs.push(String(m)) };
}
app.on?.("internal/error", () => {});

app.plugin(plugin, {
	adminUrl: `http://127.0.0.1:${PORT}`,
	autoStart: true,
	mountPath: "",
	projectDir: PROJECT_DIR,
	// 日志落临时目录：这条用例会起停好几次服务，写进项目 data/ 会把
	// 真实的 admin_ui.log 刷成测试噪声，排查真故障时第一手证据就废了。
	logDir: tmpDbDir,
	pythonPath: PY,
	startTimeoutMs: 40000,
});

const boot1 = await waitBoot(60000);
check("服务起来了", boot1 !== "", `boot=${boot1}`);

// 杀掉监听 PORT 的那个 admin_ui 进程。
// 必须用 execFileSync 传参数数组：走 execSync 的话命令要经过 cmd.exe 转义，
// PowerShell 的 $_ 会被吃成非法字符，结果一个 PID 都匹配不到 —— 坑过一次。
const killAdmin = (port) => {
	const ps =
		`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ` +
		`Where-Object { $_.CommandLine -like '*admin_ui*${port}*' } | ` +
		`ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`;
	try {
		execFileSync("powershell", ["-NoProfile", "-Command", ps], { stdio: "pipe" });
		return true;
	} catch {
		return false;
	}
};

if (boot1 !== "") {
	killAdmin(PORT);
}

const down = await waitDown(15000);
check("杀掉后服务确实停了（确认真的杀到了）", down, `boot 不再应答`);

const boot2 = await waitBoot(90000, boot1);
check("自愈：服务被重新拉起来了", boot2 !== "", `boot=${boot2}`);
check("重拉的是新实例（boot 变了）", boot2 !== "" && boot2 !== boot1, `${boot1} -> ${boot2}`);
check(
	"日志里留下了重拉记录",
	logs.some((l) => l.includes("后重拉")),
	logs.filter((l) => l.includes("重拉")).slice(-2).join(" | ")
);

const failed = results.filter((r) => !r.ok);
console.log(`\n自愈验证：${results.length - failed.length} 通过 / ${failed.length} 失败`);

try {
	app.dispose?.();
} catch {
	/* ignore */
}
await new Promise((r) => setTimeout(r, 1000));
// 兜底：把可能残留的 python 收掉，别污染后面的用例
killAdmin(PORT);
try {
	rmSync(tmpDbDir, { recursive: true, force: true });
} catch {
	/* Windows 上 WAL 句柄可能还没释放，删不掉不影响结论 */
}

process.exit(failed.length === 0 ? 0 : 1);
