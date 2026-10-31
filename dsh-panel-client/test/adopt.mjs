#!/usr/bin/env node
/**
 * 复用别的实例时，那个实例停掉后要能自己接管。
 *
 * 背景（这条 bug 是实测撞出来的）：插件发现端口上已有「形状对得上」的管理服务时
 * 会复用而不重复启动 —— 因为它手上没有 child 句柄，watch() 挂不上。
 * 于是那个服务一旦停掉（用户手动起的、上次 DSH 留下的孤儿进程被清理），
 * 面板就永远白屏到下次重启 DSH，而且日志里什么都看不到。
 *
 * 用例流程：
 *   1. 先手动起一个管理服务（模拟"别人的实例"）
 *   2. 装插件 → 走复用分支
 *   3. 杀掉那个服务 → 看护应在几秒内发现自己接管
 *   4. 端口恢复可用，且是插件自己拉起的（dispose 后会被收掉）
 */
process.env.BIONIC_PANEL_NO_TRACE = "0";

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const TEST_DIR = dirname(fileURLToPath(import.meta.url));
const PROJECT_DIR = dirname(dirname(TEST_DIR));
const PORT = 8887;

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

const probe = async () => {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/api/stats`, { signal: AbortSignal.timeout(1200) });
    const b = await r.json();
    return b?.ok === true ? b.data?.boot ?? "up" : null;
  } catch { return null; }
};

const waitBoot = async (timeoutMs) => {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const b = await probe();
    if (b) return b;
    await new Promise((r) => setTimeout(r, 500));
  }
  return null;
};

const tmpDbDir = mkdtempSync(join(tmpdir(), "bionic-adopt-test-"));
const py = findPython();

// 1. 先起一个「别人的」服务
const manual = spawn(py, ["-m", "app.admin_ui", "--port", String(PORT)], {
  cwd: PROJECT_DIR,
  env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1", BIONIC_DB_PATH: join(tmpDbDir, "agent.db") },
  stdio: "ignore",
});

const ctx = {
  logger: undefined,
  get: () => undefined,
  effect(fn) { this.__dispose = fn(); },
};
const REAL_BOOT_LOG = join(PROJECT_DIR, "data", "panel-boot.log");
const readBootAll = () => { try { return readFileSync(REAL_BOOT_LOG, "utf8"); } catch { return ""; } };
const bootBaseline = readBootAll().length;
// 只看本次跑出来的新增行，避免被 DSH 自己写的痕迹干扰。
const bootLog = () => readBootAll().slice(bootBaseline);

let disposePlugin = null;
try {
  const b0 = await waitBoot(25000);
  check("手动起的服务已就绪（模拟别人的实例）", b0 !== null, `boot=${b0}`);

  const plugin = (await import(`file:///${join(PROJECT_DIR, "dsh-panel-client", "lib", "index.js").replace(/\\/g, "/")}`)).default
    ?? (await import(`file:///${join(PROJECT_DIR, "dsh-panel-client", "lib", "index.js").replace(/\\/g, "/")}`));
  const apply = plugin.apply ?? plugin.default?.apply;
  disposePlugin = apply(ctx, {
    adminUrl: `http://127.0.0.1:${PORT}`,
    autoStart: true,
    mountPath: "",
    projectDir: PROJECT_DIR,
    logDir: tmpDbDir,
    pythonPath: py,
    startTimeoutMs: 25000,
  });

  await new Promise((r) => setTimeout(r, 2500));
  const log1 = bootLog();
  check("插件走了复用分支（而不是重复启动）", /复用现有服务/.test(log1), log1.trim().split("\n").slice(-1)[0] ?? "");
  check("复用后会盯着它", /盯着它/.test(log1));
  check("复用期间没有自己起服务（boot 未变）", (await probe()) === b0, `boot=${await probe()}`);

  // 3. 杀掉「别人的」服务
  manual.kill();
  await new Promise((r) => setTimeout(r, 1200));
  check("别人的服务确实停了", (await probe()) === null);

  // 4. 看护应接管
  const b1 = await waitBoot(30000);
  check("看护发现了，自己接管拉起", b1 !== null, `boot=${b1}`);
  check("接管的是新实例（boot 变了）", b1 !== null && b1 !== b0, `${b0} -> ${b1}`);
  check("日志里记下了接管原因", /已经不在了/.test(bootLog()), (bootLog().match(/复用中的管理服务[^\n]*/)?.[0] ?? "").slice(0, 80));

  // 5. dispose 后应当收掉自己起的那个
  disposePlugin?.();
  await new Promise((r) => setTimeout(r, 2500));
  check("dispose 后自己起的那份被收掉", (await probe()) === null);
} catch (err) {
  console.log(`[FAIL] 用例异常：${err?.message ?? err}`);
  failed++;
} finally {
  if (manual.exitCode === null && manual.signalCode === null) {
    const done = new Promise((r) => manual.once("exit", r));
    manual.kill();
    await Promise.race([done, new Promise((r) => setTimeout(r, 3000))]);
  }
  try { rmSync(tmpDbDir, { recursive: true, force: true }); } catch {}
  // 把测试写进去的痕迹截掉，保持真实日志干净（与「测试不污染真实日志」的约定一致）
  try {
    const all = readBootAll();
    if (all.length > bootBaseline) writeFileSync(REAL_BOOT_LOG, all.slice(0, bootBaseline), "utf8");
  } catch {}
}

console.log(`\n=== 复用实例的看护：${passed} 通过 / ${failed} 失败 ===`);
process.exit(failed === 0 ? 0 : 1);
