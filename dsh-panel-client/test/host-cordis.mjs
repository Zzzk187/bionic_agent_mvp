// 用真实的 @deepseek-ai/cordis 加载宿主半 index.js，
// 验证：① 模块能被 cordis 当插件识别；② config 真的传进 apply；
// ③ ctx.effect 生效、服务能被拉起；④ dispose 后被收掉。
// 这跑在真 Cordis 上（不是 mock），等价于 DSH 启动时的加载路径。
//
// cordis 是 DSH 安装目录里的包，项目自己不带依赖，所以用绝对路径 import。
const CORDIS_ENTRY =
	process.env.DSH_CORDIS ??
	"C:/tmp/dsh_asar_extract/app/dsh/node_modules/@deepseek-ai/cordis/lib/index.js";
const { Context } = await import(new URL(`file:///${CORDIS_ENTRY}`).href);
import { connect } from "node:net";

const TEST_PORT = 8896;
const TEST_URL = `http://127.0.0.1:${TEST_PORT}`;

function isListening(port, timeoutMs = 600) {
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

const app = new Context();

// 收集日志，证明走的是 ctx.logger 路径
const logs = [];
app.logger = {
	info: (...a) => logs.push(["info", a.join(" ")]),
	warn: (...a) => logs.push(["warn", a.join(" ")]),
	error: (...a) => logs.push(["error", a.join(" ")])
};

// 关键：用真实的 config（和 cordis.patch.yml 里的一模一样，只换端口避免撞车）
const config = {
	adminUrl: TEST_URL,
	autoStart: true,
	pythonPath: "",
	startTimeoutMs: 25000
};

console.log("before: listening =", await isListening(TEST_PORT));

const plugin = await import("../lib/index.js");
console.log("模块导出:", Object.keys(plugin), "| apply =", typeof plugin.apply);

// cordis 支持 function / class / {apply} 三种插件形状；
// 第二个参数就是 cordis.patch.yml 里的 config，会被原样传给 apply(ctx, config)。
const fiber = app.plugin(plugin, config);
console.log("app.plugin() 已调用，等待服务拉起…");

// 等服务起来（插件内部最多等 startTimeoutMs）
let up = false;
const deadline = Date.now() + 30000;
while (Date.now() < deadline) {
	if (await isListening(TEST_PORT)) {
		up = true;
		break;
	}
	await new Promise((r) => setTimeout(r, 500));
}

console.log("after start: listening =", up);
console.log("日志:", JSON.stringify(logs, null, 2));

// 直接打一次 HTTP，确认不只是端口开着
if (up) {
	try {
		const res = await fetch(`${TEST_URL}/api/stats`);
		const body = await res.json();
		console.log("GET /api/stats ->", res.status, JSON.stringify(body).slice(0, 160));
	} catch (e) {
		console.log("HTTP 请求失败:", e.message);
	}
}

// 卸载插件，确认按契约回收（fiber.dispose() 才是 cordis 的卸载入口）
await fiber.dispose();
console.log("已 fiber.dispose()");
let stillUp = true;
const d2 = Date.now() + 12000;
while (Date.now() < d2) {
	if (!(await isListening(TEST_PORT))) {
		stillUp = false;
		break;
	}
	await new Promise((r) => setTimeout(r, 400));
}
console.log("after dispose: listening =", stillUp);

const ok = up && !stillUp;
console.log(ok ? "\n=== PASS：cordis 真实加载路径全部正确 ===" : "\n=== FAIL ===");
process.exit(ok ? 0 : 1);
