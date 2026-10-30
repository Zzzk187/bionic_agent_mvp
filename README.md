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
cd bionic_agent_mvp
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
    command: 'C:/YOUR/PATH/bionic_agent_mvp/.venv/Scripts/python.exe'
    args: ['-m', 'app.mcp_server']
    cwd: 'C:/YOUR/PATH/bionic_agent_mvp'

- id: bionic-agent-governance
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: bionic-agent-governance
    transport: stdio
    command: 'C:/YOUR/PATH/bionic_agent_mvp/.venv/Scripts/python.exe'
    args: ['-m', 'app.mcp_server_gov']
    cwd: 'C:/YOUR/PATH/bionic_agent_mvp'
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

1. 先让 8899 面板跑起来（见上文 `bionic-agent-admin`）。
2. 把插件链接进 desktop profile 并登记到 bundles：
   ```bash
   # 用管理员/有权限的终端（需改写 profile 的 package.json，会触发 DSH 重启）
   python dsh-panel-client/install.py
   ```
   脚本做的事：① 在 desktop profile 的 `node_modules/@local/bionic-agent-panel`
   建**符号链接**指向本仓库的 `dsh-panel-client/`；② 把
   `"@local/bionic-agent-panel"` 加进 `package.json` 的 `dependencies` 与 `dsh.profile.bundles`。
   插件自带的 `cordis.patch.yml` 会被宿主**自动层叠**，无需手工改 profile 的 patch。
3. **完全退出并重启 DeepSeek Harness**（含托盘）。重启后侧边栏出现「记忆/任务」图标，
   点开即是管理面板。

> 这种形态依赖 8899 服务常驻。若不常开面板，用纯浏览器开 `http://127.0.0.1:8899`
> 同样可用，且零安装。要卸载：`python dsh-panel-client/install.py --uninstall`。

---

## 项目结构

```
bionic_agent_mvp/
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
- **数据库随项目走**：`data/agent.db` 在首次启动时自动建库并执行幂等迁移；新机器拿到代码即可跑，无需手工建表。
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
