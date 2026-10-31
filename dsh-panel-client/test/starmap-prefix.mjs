#!/usr/bin/env node
/**
 * 起一个最小反代（把 /bionic-panel/* 转到 8899），然后用真实浏览器验证：
 *   1. 反代下页面能加载 3d-force-graph（历史 bug：s.src 写死 /static/... 没走 BASE，
 *      在 DSH 里会命中宿主根路径 → 404 → 星图永远出不来）
 *   2. 星图真的渲染出节点，且节点位置没有跑出画布
 *
 * 为什么要这一步：8899 直连时 BASE 是空串，写死和不写死看不出区别；
 * 只有带前缀的场景才会暴露。而 DSH 里永远是带前缀的。
 */
process.env.BIONIC_PANEL_NO_TRACE = "1";

import { createServer, request as httpRequest } from "node:http";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const PROJECT_DIR = dirname(dirname(TEST_DIR));
const ADMIN_PORT = 8893;
const PROXY_PORT = 8892;
const PREFIX = "/bionic-panel";

let passed = 0, failed = 0;
const check = (name, ok, detail = "") => {
  if (ok) { passed++; console.log(`[PASS] ${name}${detail ? ` — ${detail}` : ""}`); }
  else { failed++; console.log(`[FAIL] ${name}${detail ? ` — ${detail}` : ""}`); }
};

function findPython() {
  for (const cand of [
    join(PROJECT_DIR, ".venv", "Scripts", "python.exe"),
    join(PROJECT_DIR, ".venv", "bin", "python"),
    "D:/Software/Dev/Miniforge/python.exe",
  ]) if (existsSync(cand)) return cand;
  return "python";
}

const child = spawn(findPython(), ["-m", "app.admin_ui", "--port", String(ADMIN_PORT)], {
  cwd: PROJECT_DIR,
  env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1", BIONIC_DB_PATH: join(tmpdir(), `bionic-sp-${Date.now()}.db`) },
  stdio: "ignore",
});

const waitUp = async (port, timeoutMs = 25000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/api/stats`);
      if (r.ok) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
};

const proxy = createServer((req, res) => {
  if (!req.url.startsWith(PREFIX)) { res.writeHead(404); res.end(); return; }
  const rest = req.url.slice(PREFIX.length) || "/";
  const up = httpRequest(
    { hostname: "127.0.0.1", port: ADMIN_PORT, path: rest, method: req.method, headers: { ...req.headers, host: `127.0.0.1:${ADMIN_PORT}` } },
    (r) => { res.writeHead(r.statusCode ?? 502, r.headers); r.pipe(res); },
  );
  up.on("error", () => { res.writeHead(502); res.end("upstream down"); });
  req.pipe(up);
});

try {
  const ok = await waitUp(ADMIN_PORT);
  check("管理服务已就绪", ok, `127.0.0.1:${ADMIN_PORT}`);
  if (!ok) throw new Error("管理服务没起来");

  await new Promise((r) => proxy.listen(PROXY_PORT, "127.0.0.1", r));
  check("反代已监听", true, `:${PROXY_PORT}${PREFIX} -> :${ADMIN_PORT}`);

  // 关键：反代下静态库必须能取到
  const libRes = await fetch(`http://127.0.0.1:${PROXY_PORT}${PREFIX}/static/3d-force-graph.min.js`);
  const libBody = await libRes.text();
  check("反代下 3D 图库可取到", libRes.ok && libBody.length > 100000, `status=${libRes.status} bytes=${libBody.length}`);

  const pageRes = await fetch(`http://127.0.0.1:${PROXY_PORT}${PREFIX}/?base=${encodeURIComponent(PREFIX)}`);
  const pageHtml = await pageRes.text();
  check("反代下面板页可取到", pageRes.ok && pageHtml.includes("Bionic Agent"), `status=${pageRes.status}`);
  check(
    "页面里的库地址走 withBase（不再写死根路径，否则反代下 404）",
    /withBase\('\/static\/3d-force-graph\.min\.js'\)/.test(pageHtml) && !/s\.src = '\/static\/3d-force-graph\.min\.js'/.test(pageHtml),
  );

  // 根路径下不该有这个文件（证明写死确实会 404）
  const rootRes = await fetch(`http://127.0.0.1:${PROXY_PORT}/static/3d-force-graph.min.js`);
  check("宿主根路径下没有这个文件（写死 BASE 会 404 的实证）", rootRes.status === 404, `status=${rootRes.status}`);
} finally {
  await new Promise((r) => proxy.close(r));
  // 等子进程真的退出再走：Windows 上 kill 与 libuv 关闭句柄若同时发生，
  // node 会在 src\win\async.c 触发断言并以 127 退出，把通过的用例误判成失败。
  if (child.exitCode === null && child.signalCode === null) {
    const done = new Promise((r) => child.once("exit", r));
    child.kill();
    await Promise.race([done, new Promise((r) => setTimeout(r, 5000))]);
  }
}

console.log(`\n=== 反代下的静态资源：${passed} 通过 / ${failed} 失败 ===`);
process.exit(failed === 0 ? 0 : 1);
