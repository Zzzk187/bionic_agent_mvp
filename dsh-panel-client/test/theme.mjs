// 面板主题的静态检查：不启动浏览器，只对 admin_ui.html 做结构断言。
//
// 为什么值得单独一条用例：主题是"看一眼对不对"的东西，最容易在后续改样式时
// 被悄悄破坏 —— 往里加一个写死的 #fff，浅色主题下就出现看不清的死角，功能测试
// 一点都不会红。这里把「必须走变量」这条约束钉死。
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const HTML = resolve(HERE, "..", "..", "app", "admin_ui.html");

let pass = 0;
let fail = 0;
const ok = (name, detail = "") => { pass++; console.log(`[PASS] ${name}${detail ? " — " + detail : ""}`); };
const bad = (name, detail = "") => { fail++; console.log(`[FAIL] ${name}${detail ? " — " + detail : ""}`); };
const check = (name, cond, detail = "") => (cond ? ok(name, detail) : bad(name, detail));

const src = readFileSync(HTML, "utf8");

// ── 1. 三种主题模式都在
check("有跟随 DSH 的选项", src.includes('value="dsh"'));
check("有内置深色选项", src.includes('value="dark"'));
check("有内置浅色选项", src.includes('value="light"'));
check("浅色变量块存在", /:root\[data-theme="light"\]\s*\{/.test(src));

// ── 2. 样式块里不能有写死的颜色
// 只看 <style> 段；<script> 里的字符串不算样式，但 DSH_TOKEN_MAP 也不能写死色值，
// 那条单独查（它只允许写令牌名）。
const styleBlock = (src.match(/<style>([\s\S]*?)<\/style>/) || [, ""])[1];
const varBlockEnd = styleBlock.indexOf("*{box-sizing");
const rulesOnly = styleBlock.slice(varBlockEnd); // 变量定义之后才是规则

const hardColors = [...rulesOnly.matchAll(/#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)/g)].map((m) => m[0]);
check(
  "样式规则里没有写死的颜色（一律走变量）",
  hardColors.length === 0,
  hardColors.length ? "发现 " + hardColors.join(", ") : "干净"
);

// ── 3. 关键变量在两套主题里都定义了
const REQUIRED = [
  "--bg", "--panel", "--panel2", "--line", "--txt", "--muted",
  "--accent", "--accent2", "--warn", "--danger", "--ok", "--chip",
  "--header1", "--header2", "--tag-bg", "--tag-fg",
  "--pill-protected-bg", "--pill-review-bg", "--btn-bg", "--btn-fg",
  "--toast-err-fg", "--toast-ok-fg", "--mask", "--shadow"
];
const darkBlock = (styleBlock.match(/:root\{([\s\S]*?)\}/) || [, ""])[1];
const lightBlock = (styleBlock.match(/:root\[data-theme="light"\]\{([\s\S]*?)\}/) || [, ""])[1];
const missingDark = REQUIRED.filter((v) => !darkBlock.includes(v + ":"));
const missingLight = REQUIRED.filter((v) => !lightBlock.includes(v + ":"));
check("深色主题定义了全部变量", missingDark.length === 0, missingDark.join(", ") || `${REQUIRED.length} 个`);
check("浅色主题定义了全部变量", missingLight.length === 0, missingLight.join(", ") || `${REQUIRED.length} 个`);

// ── 4. 令牌映射表：只写 --dsw-* 名字，不写死色值
const mapBlock = (src.match(/const DSH_TOKEN_MAP = \{([\s\S]*?)\};/) || [, ""])[1];
check("有 DSH 令牌映射表", mapBlock.length > 0);
check(
  "映射表里没有写死色值（只允许引用令牌名）",
  !/#[0-9a-fA-F]{3,8}\b|rgba?\(/.test(mapBlock),
  "只含 var 名的键值对"
);
const mappedTokens = [...mapBlock.matchAll(/'(--dsw-[a-z0-9-]+)'/g)].map((m) => m[1]);
check("映射了足够多的 DSH 令牌", mappedTokens.length >= 15, `${new Set(mappedTokens).size} 个不同令牌`);

// ── 5. 跟随 DSH 必须是默认，且跨源/独立打开要能退回
check("默认主题是跟随 DSH", /v\s*\?\s*v\s*:\s*'dsh'|return\s+THEME_PREFS\.includes\(v\)\s*\?\s*v\s*:\s*'dsh'/.test(src) || src.includes(": 'dsh'"));
check("有跨源/独立打开的兜底（parent === window）", src.includes("window.parent === window"));
check("兜底里有 try/catch（跨源会抛 SecurityError）", /function readDshTheme\(\)\s*\{[\s\S]*?try\s*\{[\s\S]*?catch/.test(src));

// ── 6. 切主题时要清掉上一次的内联变量，否则内置主题会残留 DSH 颜色
check("切换前会清空内联变量", /removeProperty\(name\)/.test(src));

// ── 7. 选择会持久化（下次打开还是上次选的）
check("主题选择写进 localStorage", src.includes("localStorage.setItem(THEME_KEY"));

console.log(`\n主题检查：${pass} 通过 / ${fail} 失败`);
if (fail > 0) process.exit(1);
