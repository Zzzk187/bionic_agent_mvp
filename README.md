# Bionic Agent MCP — 本地语义记忆 + 任务 + 治理

一个**零外部 API、可离线运行**的 MCP 服务器：给你（或任意 MCP 宿主里的 AI 助手）提供
**持久记忆**（关键词 + 本地多语言向量语义检索）、**多阶段任务交接**、以及一套
**自主记忆治理**（评分、冲突/重复检测、保护、归档恢复、审计、向量一致性）。

- 数据全部落在本地 SQLite（`data/agent.db`），无云端调用、无 API Key。
- 向量模型用 FastEmbed / ONNX 本地推理，首次使用自动下载一次后离线可用。
- 提供两个可分开挂载的 MCP 实例：**核心 21 工具**（日常）+ **治理 33 工具**（按需），同一个数据库、同一套后端，无重复逻辑。

---

## 功能总览

| 能力 | 工具（核心 / 治理） | 说明 |
|---|---|---|
| 记忆 CRUD | `save_memory` / `get_memory` / `update_memory` / `archive_memory` / `restore_memory` / `supersede_memory` | 软归档、纠正保留历史，默认禁止物理删除 |
| 检索 | `search_memories`（关键词）/ `find_related_memories`（关联）/ `semantic_search_memories`（向量语义）/ `get_memory_links` / `link_memories` | 关键词、关联、真实向量三种检索并存 |
| 任务 | `create_task` / `list_tasks` / `get_task` / `save_stage_snapshot` / `handoff_task` / `resume_task` / `complete_task` / `list_events` | 多阶段交接，自动生成初始 plan |
| 嵌入 | `embedding_status` / `index_memories` | 查看模型、建立/刷新向量索引 |
| 治理（治理实例） | 33 个：评分 `score_memories`、冲突 `detect_memory_conflicts`、保护 `protect_memory`、整理闭环 `run_memory_maintenance`、向量一致性 `get_vector_consistency`、融合召回 `recall_memories`、激活扩散 `spread_activation`、生命周期 `memory_lifecycle`、关系子图 `get_related_memories` 等 | 见下方"治理实例"章节 |

**核心实例 `bionic-agent`：21 个工具。** 日常使用只挂它即可，固定工具清单 token 成本低。
**治理实例 `bionic-agent-governance`：33 个工具。** 需要记忆治理/整理/融合召回时再挂。

> 注：因 handoff_task 历史 bug 已修复——现在 handoff 会一并返回展开后的 `task.plan`（结构化对象），不再是 `plan_json` 字符串。

---

## 环境要求

- Python ≥ 3.10（开发用 3.13 验证通过）
- 操作系统：Windows / macOS / Linux 均可（路径全部基于 `__file__` 解析，无硬编码）
- 依赖：`mcp` (1.30.x) + `fastembed` (0.7.x)，见 `requirements.txt`

---

## 快速开始（三步走）

### 1. 装依赖（建议用虚拟环境）

```bash
cd bionic-agent-mcp
python -m venv .venv
# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt
# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt
```

首次调用语义检索时，FastEmbed 会下载模型
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`（约 100MB，一次即可，之后离线）。
可在代码或环境变量里换模型：`BIONIC_EMBEDDING_MODEL=...`（换模型后需重新 `index_memories`）。

### 2. 跑通自检（不连 MCP 宿主也能验证）

```bash
.venv/Scripts/python -m compileall app
.venv/Scripts/python tests/smoke_test.py
.venv/Scripts/python tests/verify_mcp_split_test.py   # 应打印 Core=21 Gov=33 VERIFY_OK
```

### 3. 挂到 DeepSeek Harness（或任意 MCP 宿主）

把下面两段写进你的 MCP 配置（路径换成你自己的绝对路径）。**推荐用 `-m` 入口**
（最稳，自动处理 `sys.path`）：

```yaml
# DeepSeek Harness: ~/.dsh/skill-mcp-manager/registry.json 样式（节选）
- id: bionic-agent
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: bionic-agent
    transport: stdio
    command: 'C:/YOUR/PATH/bionic-agent-mcp/.venv/Scripts/python.exe'
    args: ['-m', 'app.mcp_server']
    cwd: 'C:/YOUR/PATH/bionic-agent-mcp'

- id: bionic-agent-governance
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: bionic-agent-governance
    transport: stdio
    command: 'C:/YOUR/PATH/bionic-agent-mcp/.venv/Scripts/python.exe'
    args: ['-m', 'app.mcp_server_gov']
    cwd: 'C:/YOUR/PATH/bionic-agent-mcp'
```

> ⚠️ **不要**用 `args: ['.../app/mcp_server_gov.py']`（脚本式直接跑）来启动治理实例——
> 那样 `sys.path[0]` 会是 `app/` 而非项目根，`from app import ...` 会失败。
> 治理入口已内置 sys.path 兜底，但**统一用 `-m app.mcp_server_gov` 最省心**。
> 参考文件：`deepseek-harness-mcp.example.yml`。

重启宿主后，核心实例应可立即调用；治理实例挂载后即可用上 33 个治理工具。

---

## 可视化管理面板（Web UI）

不想只走 MCP 工具调用？仓库自带一个**自包含 Web 管理面板**，直连 `data/agent.db`，
零外部 CDN、可离线打开，覆盖记忆 / 任务 / 治理三大模块：记忆与任务 CRUD、语义检索、
关系图、治理评分 / 冲突检测 / 重复识别 / 巩固候选、嵌入模型预热与状态。

```bash
# 装好依赖后（含新建的 fastapi / uvicorn）
.venv/Scripts/python -m app.admin_ui                # 默认 http://127.0.0.1:8899
.venv/Scripts/python -m app.admin_ui --host 0.0.0.0 --port 8900   # 自定义
# 或走安装好的入口
bionic-agent-admin
```

浏览器打开 `http://127.0.0.1:8899` 即可。面板直接用 `app.*` 的现成函数读写与 MCP 同一份
数据库，因此**和 MCP 工具完全等价、实时同步**——在面板里改的记忆，MCP 工具立刻能读到。

> 设计取舍：面板是**独立进程**（FastAPI），不侵入 DeepSeek Harness 的 Electron/asar，
> 因此宿主更新、热重载都不会影响它。

### 可选：把面板嵌进 DeepSeek Harness（侧边栏）

仓库自带一个 Cordis client 插件 `dsh-panel-client/`，它只在 DSH 侧边栏加一个
「记忆/任务」图标，**点开后整块是 iframe 承载上面的 8899 面板**——不改 asar、不碰
宿主源码、下次 DSH 更新自动失效也不影响其他功能。

1. 把插件链接进 desktop profile 并登记到 bundles（**不需要**先手动跑 `bionic-agent-admin`，
   插件的宿主半会在 DSH 启动时自己拉起 8899 服务）：
   ```bash
   # 用管理员/有权限的终端（需改写 profile 的 package.json，会触发 DSH 重启）
   python dsh-panel-client/install.py
   ```
   脚本做的事：① 在 desktop profile 的 `node_modules/@local/bionic-agent-panel`
   建**符号链接**指向本仓库的 `dsh-panel-client/`；② 把
   `"@local/bionic-agent-panel"` 加进 `package.json` 的 `dependencies` 与 `dsh.profile.bundles`。
   插件自带的 `cordis.patch.yml` 会被宿主**自动层叠**，无需手工改 profile 的 patch。
2. **完全退出并重启 DeepSeek Harness**（含托盘）。重启后侧边栏出现「记忆/任务」图标，
   点开即是管理面板。

> **服务生命周期**：插件宿主半（`lib/index.js`）在 `apply()` 里先探测 8899 端口——
> 已有服务就直接复用（绝不 kill 用户自己起的那个），没有才 `spawn` 一个
> `python -m app.admin_ui --port 8899`，日志写到 `data/admin_ui.log`；DSH 退出时
> 只回收它自己起的进程。Python 解释器按 `.venv` → miniforge → anaconda → 系统 python
> 的顺序自动探测，也可在 `cordis.patch.yml` 的 `config.pythonPath` 里指定。
> 这种形态下不再需要手动常驻 `bionic-agent-admin`。若不想装插件，用纯浏览器开
> `http://127.0.0.1:8899` 同样可用，且零安装。要卸载：
> `python dsh-panel-client/install.py --uninstall`。

#### 装完但侧边栏没图标？先跑自检，别靠猜

```bash
node dsh-panel-client/verify-install.mjs
```

它会逐项验证宿主的判定链（profile 目录、`dependencies`、`dsh.profile.bundles`、
node_modules 可解析、patch 内容、`dsh.engines.dsh` 与真实运行时版本、8899 是否在
监听），并且**用两份日志把故障定位到具体一半**：

| `data/panel-boot.log` | `data/panel-client.log` | 说明 |
| --- | --- | --- |
| 有 | 有 | 通路全通 |
| 有 | 无 | Node 侧加载成功，卡在浏览器端（client.js 求值失败 / 图标没挂上） |
| 无 | — | 宿主根本没加载插件，多半是 `dsh.engines.dsh` 不满足被**静默跳过** |

- `panel-boot.log`：宿主半 `apply()` 一被调用就落一行，证明 Node 侧加载了。
- `panel-client.log`：面板页面在 iframe 里跑起来后 `fetch('/api/ping')` 报到，
  证明浏览器侧真的渲染了。脚本探测和直接打开 8899 都不算，只认宿主 iframe
  （UA 带 `@deepseek-ai/dsh-desktop`）。
- 两份日志都会写进项目 `data/` 目录，已在 `.gitignore` 里。
- **报到要属于「当前这个服务实例」才算数**：`/api/ping` 每行带一个 `boot` 标识
  （`<pid>-<随机>`，每次启动都不同），`/api/stats` 也返回同一个值。自检拿当前实例的
  `boot` 去比对，上一批 DSH 留下的旧报到会被判为 `[WAIT]` 而不是 `[PASS]` —— 否则
  "我重启了但没点开面板"会显示成"浏览器端已经通了"（这个假信号踩过）。
- **跑完自检若这两项显示 `[PASS]`，但你还没重启过 DSH，那是假信号**——多半是有人
  手动起了服务或用脚本代理过。先删掉 `data/panel-boot.log`、`data/panel-client.log`
  再跑一次，两项应回到 `[WAIT]`。

> 一个容易忽略的坑：面板服务在 8899，宿主页面在随机端口，**这是跨源**。如果
> 管理服务没给 CORS 头，跨源 `fetch` 会被浏览器拦掉——服务明明正常，探测也会
> 判成"没响应"。`app/admin_ui.py` 用 `CORSMiddleware` 限死本机来源
> （`127.0.0.1`/`localhost`/`[::1]` 任意端口）来解决，不放开 `*`。

#### 为什么面板走同源反代，而不是直连 8899

插件默认把面板挂到 **DSH 自己服务器的 `/bionic-panel` 前缀**上（`ctx.webServer`
反代到 8899），iframe 打开的是同源的 `/bionic-panel/`。这不是绕远路，是因为跨源
方案要同时赌三件事，任何一件不成立都表现为「白屏 / 空列表 / 网络错误」，而报错
只出现在用户浏览器的 console 里：

1. 浏览器允许 iframe 嵌入 8899（`X-Frame-Options` / `frame-ancestors`）；
2. 浏览器允许页面跨源 XHR 到 8899（CORS 头对不对）；
3. `client.js` 里硬编码的端口与 `cordis.patch.yml` 里的 `adminUrl` 一致
   （client 半拿不到 config，只能硬编码）。

同源反代把这三条都变成不变量，顺带解决了第 3 条。实现分两层：

- 宿主半 `lib/index.js`：`ctx.inject(["webServer"], …)` 注册 `{kind:"prefix",
  path:"/bionic-panel"}`，逐请求反代到 `adminUrl`；上游不可达时返回**可读的 502**
  （带 `message` 与排查提示），而不是挂住。`webServer` 不存在（非 Web 宿主）或
  注册冲突时只降级、不报错。
- 浏览器半 `lib/client.js`：iframe 先走 `/bionic-panel/?base=%2Fbionic-panel`
  （`base` 让面板页把自身的 `/api/...` 补上前缀）；同源探不通再退回跨源
  `http://127.0.0.1:8899`。**iframe 一律渲染**，探测失败只叠加一条说明条。

**写操作要过 DSH 的 Host/Origin fence。** 同源意味着任何能打到 DSH 端口的网页
都能对我们的写接口发请求（读响应被同源策略挡住，但 `POST`/`DELETE` 会被执行），
而记忆库的删除/归档不可逆。所以反代对**非 GET/HEAD/OPTIONS** 请求先调
`ctx.connection.requestRejection(req)`：被拒就返回 401/403 JSON，不转发上游。
只拦写方法是有意的——GET 没有跨站后果，把它也卡在 cookie 上反而会在 authority
变化的边缘情况把能用的面板判死；fence 自身抛错时也放行，安全组件故障不该让本地
面板变砖。

**端口被别人占着时会自动换端口，而不是复用。** 只探测「端口有没有人听」是不够的：
8899 是常见端口，可能被别的服务、上一次没退干净的进程占着。直接复用会把面板接到
别人的页面上——表现是打开后一片乱码，且不报任何错，最难查。所以宿主半在发现端口
有人听时，先 `GET /api/stats` 认一下「是不是我们自己」（要 `{"ok":true,"data":{...memories_total}}`
这个独有的形状）；形状不对就顺延到下一个空闲端口，并在日志里写明换了哪个。
反代的目标地址是逐请求现取的，所以换端口后路由跟着走，不用重挂。

> 副作用：直接用 `curl` 往 `/bionic-panel/api/...` 发写请求会被拦（没有 DSH 的
> cookie）。面板自己在 iframe 里跑是同源的，会自动带 cookie，不受影响。

想在面板页里改用别的地址，改 `cordis.patch.yml` 的 `adminUrl` 即可，前端不用动；
把 `mountPath` 设为 `''` 可关掉反代、退回纯跨源。

> 自检覆盖了这条链路，分两层：
>
> - 「同源反代挂载」（`test/proxy-mount.mjs`）用手写假 `webServer`，快速验证页面、
>   API、查询串透传、上游断开的 502、写方法 fence（401/403）、以及 dispose 后路由
>   被摘除。
> - 「真宿主 WebServer 反代」（`test/host-webserver.mjs`）把 DSH 安装目录里真的
>   `@deepseek-ai/dsh-host-webserver` 装进一个真 cordis `Context`，走完全真实的
>   `ctx.inject(["webServer"], ...)` 路径。这一层是必要的：假 `webServer` 复刻了
>   `register()` 的契约，但证明不了「服务就绪时才回调」这条路径通不通——它不通时
>   插件会安静地什么都不挂，而假 webServer 的用例永远是绿的。
>
> 两条都需要宿主源码（默认 `C:/tmp/dsh_asar_extract/app/dsh/node_modules/@deepseek-ai`，
> 可用 `DEPS_DIR` 覆盖），拿不到时 SKIP 而不是 FAIL。

---

## 项目结构

```
bionic-agent-mcp/
├── app/
│   ├── mcp_server.py        # 双实例定义（mcp + mcp_gov）+ 21 核心工具
│   ├── mcp_server_gov.py    # 治理实例启动入口（python -m app.mcp_server_gov）
│   ├── admin_ui.py          # 可视化管理面板（python -m app.admin_ui，FastAPI）
│   ├── admin_ui.html        # 面板前端（内联 CSS+JS，零外部依赖）
│   ├── db.py                # SQLite 初始化 + 幂等迁移 + 事务
│   ├── embeddings.py        # 本地 FastEmbed 向量（预热/就绪探针/降级）
│   ├── governance_*.py      # 评分 / 冲突 / 维护 / 规则 / 服务 / 生命周期
│   ├── activation_spreading.py
│   └── memory_lifecycle.py
├── tests/                   # 离线单测 + MCP 工具注册验证
├── data/                    # agent.db（首次运行自动建库 + 迁移；勿手删）
├── requirements.txt
├── README.md
└── build_zip.py             # 生成可分发的 dist/bionic-agent-mcp.zip
```

---

## 记忆模型

- `memories` 表：`status` 是唯一检索过滤字段（`active` / `archived`），全程零物理删除。
- 治理视图字段 `memory_status`（`active/archived/superseded/pending_review`）与 `protected` 标志，不污染旧工具返回。
- 向量独立存 `memory_embeddings` 表；内容变更按哈希识别并自动重新索引。
- 冲突/关系/反馈/治理动作均有独立表 + 审计（`events`），可回滚。

---

## 可移植 / 传播要点

- **路径零硬编码**：所有路径基于 `Path(__file__).resolve()`，换机器只改 MCP 配置里的绝对路径。
- **数据库随项目走**：`data/agent.db` 在首次启动时自动建库并执行幂等迁移；新机器拿到代码即可跑，无需手工建表。MCP 入口在每次工具调用前 `_ensure_db()`，管理面板在 FastAPI `lifespan` 里做同一件事——所以空目录直接起面板也不会出现 `no such table`。
- **库位置可覆盖**：`BIONIC_DB_PATH=/path/to/agent.db` 能换掉默认的 `data/agent.db`。跑第二实例、做测试、或想把库放到同步盘时用它，别去改 `app/db.py`（自检脚本 `dsh-panel-client/test/proxy-mount.mjs` 就是靠它把测试写入隔离到临时目录的）。
- **迁移安全**：`app/db.py` 的 `_migrate_phase5` 仅幂等加列/建表，从不 DROP/重建；重复运行无副作用。
- **打包**：`python build_zip.py` 生成可分发的 ZIP（自动排除 `data/agent.db`、`__pycache__` 等私有/临时文件）。

### 跨机迁移步骤

1. 在源机：`cp data/agent.db` 备份（或信任自动备份 `data/backups/`）。
2. 在目标机：克隆/解压代码 → 建 venv → `pip install -r requirements.txt`。
3. 把 `agent.db` 拷到目标机 `data/`，或直接启动让它自动建空库。
4. 挂载 MCP 配置（见上），重启宿主。

---

## 测试

| 套件 | 覆盖 |
|---|---|
| `tests/smoke_test.py` | DB 迁移与记忆读写（离线，临时库） |
| `tests/embedding_test.py` | 向量持久化兼容 |
| `tests/verify_mcp_split_test.py` | 双实例工具注册验证（核心 21 + 治理 33 = 54） |

全部基于真实 SQLite，无 mock。示例用法：

```bash
.venv/Scripts/python tests/smoke_test.py
.venv/Scripts/python tests/verify_mcp_split_test.py
```

> 仅保留上述可长期维护的核心回归测试。数据库迁移逻辑已内联进 `app/db.py`，
> 首次启动 `init_db()` 自动幂等执行，无需单独迁移脚本。

---

## 已知限制

- 默认向量检索是「SQLite 存向量 + Python 余弦扫描当前活跃记忆」，适合中小规模；超大规模可换 ANN 索引。
- 治理检测是确定性规则（不用 LLM），零外部成本，但语义冲突需人工裁决（`review_memory_conflict`）。
