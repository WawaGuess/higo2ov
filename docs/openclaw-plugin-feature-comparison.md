# Higo2OV 与 OpenClaw-Plugin 功能对比分析

## 概述

| 维度 | Higo2OV (当前代码) | OpenClaw-Plugin (OpenViking) |
|------|-------------------|------------------------------|
| **定位** | Higo 平台的 Session Memory Plugin | OpenClaw 平台的 Context-Engine Plugin |
| **语言** | Python (FastAPI) | TypeScript (Node.js) |
| **架构** | 独立 HTTP 微服务，被动接收请求 | 嵌入式插件，主动注册 Hook/Tool/Service |
| **后端** | 抽象 MemoryEngine（待实现） | OpenViking 长时记忆系统 |
| **当前状态** | 原型/占位阶段，仅支持基础协议 | 生产级，功能完整 |

---

## 功能点详细对比

### A. 上下文引擎生命周期 (Context Engine Lifecycle)

| # | 功能点 | 具体介绍 | 当前能否实现 | 不能实现的原因 | 如果要实现需要怎么做 |
|---|--------|----------|-------------|---------------|-------------------|
| A1 | `assemble()` 会话组装 | 从 OpenViking 读取会话上下文，按 Token 预算重建消息列表：包含 `latest_archive_overview`（会话历史摘要）、`pre_archive_abstracts`（归档索引）、活跃消息块，并修复 tool-use/result 配对 | **否** | Higo 插件协议仅定义了 `probe` 和 `transform` 两种模式，没有 `assemble` 生命周期接口；当前代码是被动接收 transform 请求，不是主动组装上下文 | 需要扩展 Higo 插件协议，增加 `assemble` 模式；在 `engine/memory.py` 中增加 `assemble_session()` 方法；实现消息分区逻辑（Instruction → Archive → Session → Reserved）；集成 OpenViking 的 `/api/v1/sessions/{id}/context` API |
| A2 | `afterTurn()` 回合追加 | 每轮对话结束后，将新增消息追加到 OpenViking 会话，仅保留 user/assistant 捕获文本，保留 toolCall/toolResult，剥离注入的 `<relevant-memories>` 块，触发异步 `commit()` | **否** | Higo 协议没有 `afterTurn` 回调机制；当前代码只处理单次 transform 请求，无状态保存到外部系统 | 需要 Higo 平台支持回合结束回调或增加 `after_turn` 模式；实现消息切片逻辑（从 `prePromptMessageCount` 开始）；增加异步 commit 触发器和 Phase 2 轮询；实现 `<relevant-memories>` 块剥离逻辑 |
| A3 | `compact()` 压缩同步 | 同步边界：调用 `commit(wait=true)`，阻塞等待完成，重新读取 `latest_archive_overview`，返回更新后的 Token 估算和摘要内容 | **否** | 当前代码无外部存储系统，无压缩概念；Higo 协议无 `compact` 模式 | 需要增加 `compact` 模式到 Higo 协议；实现同步 commit 等待机制；集成 OpenViking `/api/v1/sessions/{id}/commit` API 的轮询等待；实现 Token 重新估算逻辑 |
| A4 | `ingest()` / `ingestBatch()` 摄入 | 批量摄入外部记忆（当前为透传） | **否** | 当前无外部记忆摄入接口 | 增加 `ingest` 模式；实现批量消息处理接口 |
| A5 | 4 层上下文分区 | Instruction（系统提示）→ Archive（历史摘要+归档摘要）→ Session（活跃消息）→ Reserved（模型输出预留） | **否** | 当前 `_build_messages` 只有简单的 5 步重建逻辑，无分层预算概念 | 重写 `_build_messages`，引入 `allocateContextBudget()` 函数；实现各层预算分配（Archive 15% 封顶 8K，Reserved 15% 或 20K 地板） |
| A6 | Token 预算管理 | 将总预算分配到归档记忆、会话上下文和预留头room | **否** | 当前无 Token 计算逻辑 | 引入 Token 计数器（如 `tiktoken`）；实现 `allocateContextBudget()` 函数 |
| A7 | Session-to-OV ID 映射 | 优先使用 UUID sessionId；否则从 sessionKey 派生 SHA-256；清理不安全 Windows 路径字符 | **否** | 当前直接使用传入的 session_id，无映射逻辑 | 增加 `session_id_mapping()` 函数；实现 SHA-256 派生和字符清理 |
| A8 | Tool Call 转录修复 | 修复 tool-use/result 配对，删除孤立 toolResult，移动错位结果，合成缺失结果，编辑 `sessions_spawn` 附件内容 | **否** | 当前 `_build_messages` 仅做简单角色过滤，无 tool call 修复逻辑 | 增加 `session-transcript-repair.py` 模块；实现 tool call ID 清洗和配对修复逻辑 |
| A9 | 诊断信息输出 | 输出结构化的 `openviking: diag {...}` JSON 行（assemble/afterTurn/compact 阶段） | **否** | 当前仅返回简单 JSON 响应，无诊断日志 | 增加诊断日志模块；在关键阶段注入结构化日志输出 |

---

### B. 自动回忆 (Automatic Recall)

| # | 功能点 | 具体介绍 | 当前能否实现 | 不能实现的原因 | 如果要实现需要怎么做 |
|---|--------|----------|-------------|---------------|-------------------|
| B1 | `before_prompt_build` 自动回忆 Hook | 每次构建提示前，提取最新用户文本，运行可用性预检，并行查询 `viking://user/memories` 和 `viking://agent/memories`（如启用 `recallResources` 则同时查询 `viking://resources`） | **否** | Higo 协议不支持 `before_prompt_build` Hook；当前是被动接收 transform 请求，无法在构建提示前主动执行 | 需要 Higo 平台支持 prompt build 前 Hook；或重构为客户端集成而非服务端插件；实现异步并行查询逻辑 |
| B2 | 去重与过滤 | 按 URI 去重，过滤仅叶子记忆（`level == 2`），应用 `recallScoreThreshold` 分数阈值 | **否** | 当前无记忆存储系统，无去重/过滤对象 | 集成记忆存储后端；实现 `memory-ranking.py` 模块；实现按 level 和 score 过滤 |
| B3 | 重排序 (Reranking) | 非纯向量分数：提升叶子记忆 (+0.12)、时间查询的事件记忆 (+0.1)、偏好查询的偏好记忆 (+0.08)、词汇重叠 (+0.2 max) | **否** | 当前无排名逻辑 | 实现 `memory-ranking.py` 中的 `rerank_memories()` 函数；实现查询类型检测和分数 Boost 逻辑 |
| B4 | Token 预算注入 | `buildMemoryLinesWithBudget()` — 在 Token 预算约束下构建记忆行；第一条记忆即使超预算也包含（有界溢出） | **否** | 当前无 Token 预算约束的注入逻辑 | 实现 `build_memory_lines_with_budget()` 函数；集成 Token 计数；实现有界溢出保护 |
| B5 | 注入格式 | 前置 `<relevant-memories>` 块，格式为 `- [category] content (score%)` | **部分可** | 当前 `generate_memory()` 返回的字符串可直接作为 user 消息注入，但无 `<relevant-memories>` 包装格式 | 在 `_build_messages` 或 `MemoryEngine.generate_memory()` 中增加 `<relevant-memories>` 包装和格式化逻辑 |
| B6 | 超时保护 | 自动回忆有 5 秒超时，防止提示构建阻塞 | **否** | 当前无超时保护 | 使用 `asyncio.wait_for()` 包装回忆逻辑；实现超时降级（返回空记忆） |

---

### C. 自动捕获 (Automatic Capture)

| # | 功能点 | 具体介绍 | 当前能否实现 | 不能实现的原因 | 如果要实现需要怎么做 |
|---|--------|----------|-------------|---------------|-------------------|
| C1 | `afterTurn` 自动捕获 | 启用 `autoCapture` 时，每轮结束提取从 `prePromptMessageCount` 开始的新消息 | **否** | 无 `afterTurn` 回调；无消息增量追踪 | 需要 Higo 支持回合结束回调；实现消息增量切片逻辑 |
| C2 | 语义捕获模式 | `semantic` 模式捕获所有符合条件的用户文本 | **否** | 当前无捕获逻辑 | 实现 `get_capture_decision()` 函数；定义语义捕获规则 |
| C3 | 关键词捕获模式 | `keyword` 模式先使用触发正则（"remember", "preference", "decided", 中文触发词, 邮箱/电话模式等） | **否** | 当前无关键词检测 | 实现关键词触发正则集合；支持中英文触发词 |
| C4 | 捕获清洗 | 剥离 `<relevant-memories>` 块、对话元数据、发送者元数据、围栏 JSON 元数据块、前导时间戳、心跳消息、压缩器系统消息 | **否** | 当前无清洗逻辑 | 实现 `sanitize_user_text_for_capture()` 函数；实现多种清洗规则 |
| C5 | 结构化消息部分 | 文本作为 `type: "text"` 部分发送；工具结果作为 `type: "tool"` 部分发送（含 tool_id, tool_name, tool_input, tool_output, tool_status） | **否** | 当前消息为简单 dict 格式 | 扩展消息模型支持结构化部分；在 `models.py` 中增加 `MessagePart` 类型 |
| C6 | 阈值触发 commit | 追加后检查 `pending_tokens`，超过 `commitTokenThreshold` 触发异步 `commit(wait=false)` | **否** | 无 Token 累计和 commit 触发机制 | 实现 `pending_tokens` 计数器；配置 `commitTokenThreshold`；实现异步 commit 触发 |
| C7 | Phase 2 轮询 | 如启用 `logFindRequests`，轮询异步任务以记录 `memories_extracted` 数量 | **否** | 无异步任务系统 | 集成任务轮询系统；实现 `/api/v1/tasks/{taskId}` 轮询 |

---

### D. 记忆管理工具 (Memory Management Tools)

| # | 功能点 | 具体介绍 | 当前能否实现 | 不能实现的原因 | 如果要实现需要怎么做 |
|---|--------|----------|-------------|---------------|-------------------|
| D1 | `memory_recall` 显式回忆 | 显式长期记忆搜索：查询 `viking://user/memories`、`viking://agent/memories` 和可选的 `viking://resources`；去重、过滤叶子、应用分数阈值、格式化结果 | **否** | 当前无记忆存储后端，无搜索能力 | 集成 OpenViking 或自建向量数据库；实现语义搜索 API；实现结果去重/过滤/格式化 |
| D2 | `memory_store` 存储记忆 | 将文本写入 OpenViking 会话并触发 commit（`wait=true`）；如无会话则创建临时会话；返回记忆提取数量 | **否** | 当前无外部存储系统 | 集成存储后端；实现会话创建/写入/commit 链路；返回提取计数 |
| D3 | `memory_forget` 遗忘记忆 | 按精确 URI 删除（带 `isMemoryUri` 守卫），或搜索后删除单个强匹配项（score >= 0.85 且唯一候选），否则返回候选列表 | **否** | 当前无记忆删除能力 | 实现 URI 守卫验证；实现搜索-确认-删除流程 |
| D4 | `ov_archive_expand` 归档展开 | 按归档 ID 将压缩归档展开为原始消息；从 OpenViking 检索完整消息历史 | **否** | 当前无归档系统 | 集成归档存储；实现归档展开接口 |
| D5 | `ov_import` 资源导入 | 将资源或技能导入 OpenViking：支持本地文件/目录、远程 URL、Git URL；目录使用纯 JS zip（`fflate`）本地压缩后上传 | **否** | 当前无资源导入能力 | 实现文件/目录/URL 导入逻辑；集成 zip 压缩；实现上传接口 |
| D6 | `ov_search` 搜索资源技能 | 搜索 OpenViking 资源和技能：默认同时搜索 `viking://resources` 和 `viking://agent/skills`；格式化为对齐表格 | **否** | 当前无资源/技能索引 | 集成搜索后端；实现结果表格格式化 |

---

### E. 资源与技能导入 (Resource & Skill Import)

| # | 功能点 | 具体介绍 | 当前能否实现 | 不能实现的原因 | 如果要实现需要怎么做 |
|---|--------|----------|-------------|---------------|-------------------|
| E1 | 资源导入 | POST `/api/v1/resources`：支持 `pathOrUrl`（远程 URL、本地文件、本地目录）；本地文件通过 `/api/v1/resources/temp_upload` 上传；目录使用 `fflate` zip 后上传 | **否** | 当前无资源管理 API | 增加资源上传接口；集成 zip 压缩库（如 `zipfile`）；实现临时上传和正式导入两步流程 |
| E2 | 技能导入 | POST `/api/v1/skills`：支持 `path`（文件/目录）或 `data`（原始内容） | **否** | 当前无技能管理 API | 增加技能导入接口；支持文件和原始内容两种模式 |
| E3 | 等待模式 | 两者都支持 `wait=true`，客户端轮询直到处理完成 | **否** | 当前无异步任务轮询 | 实现任务状态轮询系统；定义完成状态判断 |
| E4 | 斜杠命令 | 注册 `/ov-import` 和 `/ov-search` CLI 命令供手动使用 | **否** | 当前无 CLI 命令系统 | 增加 CLI 框架（如 `typer`）；注册命令处理器 |

---

### F. 本地/远程运行时模式 (Local/Remote Runtime)

| # | 功能点 | 具体介绍 | 当前能否实现 | 不能实现的原因 | 如果要实现需要怎么做 |
|---|--------|----------|-------------|---------------|-------------------|
| F1 | 本地模式 | 插件管理 OpenViking 子进程：解析 Python 环境、准备端口（杀掉残留 OpenViking、自动寻找空闲端口）、等待 `/health`、缓存本地客户端、处理 stderr 日志 | **否** | 当前是独立服务，不管理子进程 | 增加进程管理模块（`process_manager.py`）；实现子进程启动/监控/重启；实现端口冲突检测和自动寻找 |
| F2 | 远程模式 | 纯 HTTP 客户端连接到现有 OpenViking 服务器，无子进程管理 | **部分可** | 当前本身就是 HTTP 服务，但只暴露简单接口，不是作为客户端连接外部服务 | 增加 `OpenVikingClient` 类；封装所有 OpenViking HTTP API；支持配置远程服务器地址和认证 |
| F3 | 防御性重新生成 | 如非指定生成者但无有效本地进程，触发新生成以恢复网关强制重启场景 | **否** | 当前无进程状态监控 | 增加进程健康检查；实现防御性重新生成逻辑 |
| F4 | 端口管理 | `prepareLocalPort()` — 杀掉目标端口上的残留 OpenViking，如被非 OpenViking 进程占用则寻找下一个空闲端口 | **否** | 当前无端口管理 | 实现端口扫描和进程检测逻辑；集成 `psutil` 或类似库 |
| F5 | 运行时预检 | `checkLocalRuntime()` — 验证 Python >= 3.10 和 `openviking` 包可导入；从不自动安装 | **否** | 当前无运行时检查 | 增加环境检查脚本；验证 Python 版本和依赖可用性 |

---

### G. 身份与路由 (Identity & Routing)

| # | 功能点 | 具体介绍 | 当前能否实现 | 不能实现的原因 | 如果要实现需要怎么做 |
|---|--------|----------|-------------|---------------|-------------------|
| G1 | 会话 Agent 解析 | `createSessionAgentResolver()` — 记住每会话的显式 Agent 观察；从 sessionKey/sessionId/config 解析 `X-OpenViking-Agent` | **否** | 当前无 Agent 概念 | 增加 Agent 解析器；实现会话级 Agent 记忆 |
| G2 | Agent ID 清洗 | `sanitizeOpenVikingAgentIdHeader()` — 将不安全字符替换为 `_`，折叠多个下划线，裁剪为 `[a-zA-Z0-9_-]` | **否** | 当前无 Agent ID 处理 | 增加 Agent ID 清洗函数 |
| G3 | Config Agent 前缀 | 当 `agentId` 不为 `default` 时，前缀会话 Agent 为 `<configAgentId>_<sessionAgent>` | **否** | 当前无多 Agent 配置 | 增加 Agent 前缀逻辑；支持层级 Agent 命名 |
| G4 | 规范命名空间扩展 | `buildCanonicalRoot()` + `normalizeTargetUri()` — 基于 `isolateUserScopeByAgent` / `isolateAgentScopeByUser` 标志将 `viking://user/memories` 和 `viking://agent/memories` 别名扩展为规范 URI | **否** | 当前无命名空间系统 | 实现命名空间规范化模块；支持作用域隔离配置 |
| G5 | 路由调试日志 | `logFindRequests` 输出 `X-OpenViking-Agent`、`X-OpenViking-Account`、`X-OpenViking-User`、解析的用户 ID、目标 URI、查询等；从不记录 API Key | **否** | 当前无结构化调试日志 | 增加路由调试日志模块；确保 API Key 脱敏 |

---

### H. 设置与安装 (Setup & Installation)

| # | 功能点 | 具体介绍 | 当前能否实现 | 不能实现的原因 | 如果要实现需要怎么做 |
|---|--------|----------|-------------|---------------|-------------------|
| H1 | 交互式设置向导 | `openclaw openviking setup` — 双语（EN/ZH）、检测现有配置、验证 `ov.conf`、测试服务健康、写入 `openclaw.json` | **否** | 当前无 CLI 和设置向导 | 增加 CLI 框架（如 `typer` 或 `click`）；实现交互式设置流程；增加配置验证逻辑 |
| H2 | 跨平台安装器 | `ov-install` / `install.js` — 一键安装/升级/回滚：支持 `--version`、`--plugin-version`、`--openviking-version`、`--workdir`、`--update`、`--rollback`、`--current-version` | **否** | 当前无安装器 | 创建安装脚本（Python + shell）；实现版本解析、下载、安装、回滚 |
| H3 | 版本解析 | 未指定版本时自动解析最新 GitHub Tag；检查 OpenClaw/OpenViking 兼容性 | **否** | 当前无版本管理 | 集成 GitHub API 查询最新版本；实现兼容性矩阵检查 |
| H4 | 插件升级与回滚 | 备份旧插件目录和 `openclaw.json`，写入审计文件，支持 `--rollback` 恢复先前状态 | **否** | 当前无升级/回滚机制 | 实现备份/恢复逻辑；写入审计日志 |
| H5 | 环境文件 | 生成 `openviking.env` / `openviking.env.bat` / `openviking.env.ps1`，包含 `OPENVIKING_PYTHON` 和 `OPENCLAW_STATE_DIR` | **否** | 当前无环境文件生成 | 实现多平台环境文件模板生成 |
| H6 | 健康检查脚本 | `ov-healthcheck.py` — 5 阶段端到端流水线测试：对话注入 → 捕获验证 → commit/记忆验证 → 回忆验证 → 清理 | **否** | 当前无端到端测试 | 实现 5 阶段健康检查脚本；实现自动化验证流程 |

---

### I. 会话绕过模式 (Session Bypass Patterns)

| # | 功能点 | 具体介绍 | 当前能否实现 | 不能实现的原因 | 如果要实现需要怎么做 |
|---|--------|----------|-------------|---------------|-------------------|
| I1 | `bypassSessionPatterns` | Glob-like 模式（如 `agent:*:cron:**`）完全绕过 OpenViking；使用 `*` 匹配单段，`**` 跨段匹配；应用于 Hook 和工具 | **否** | 当前无会话过滤逻辑 | 实现模式编译和匹配函数（`compile_session_patterns`、`should_bypass_session`）；在请求入口处添加过滤 |

---

### J. 健康检查与诊断 (Health Check & Diagnostics)

| # | 功能点 | 具体介绍 | 当前能否实现 | 不能实现的原因 | 如果要实现需要怎么做 |
|---|--------|----------|-------------|---------------|-------------------|
| J1 | `ov-healthcheck.py` | Python 脚本（仅标准库），注入真实对话通过 Gateway，验证会话捕获、commit、归档生成、记忆提取、同会话连续性、新会话回忆 | **否** | 当前无端到端验证 | 实现健康检查脚本；集成所有组件进行端到端验证 |
| J2 | `HEALTHCHECK.md` | 健康检查工具的完整文档 | **可立即实现** | 文档无技术依赖 | 编写 `docs/HEALTHCHECK.md` |

---

## 架构差异总结

| 差异维度 | Higo2OV (当前) | OpenClaw-Plugin (目标) |
|---------|---------------|----------------------|
| **运行时模型** | 被动服务（请求-响应） | 主动插件（Hook + 工具 + 服务） |
| **生命周期** | 单次 `transform` 调用 | 完整的 assemble → afterTurn → compact 循环 |
| **状态管理** | 无状态（每次请求独立） | 有状态（会话级 Token 计数、归档追踪） |
| **存储后端** | 抽象接口（待实现） | 完整的 OpenViking HTTP API 集成 |
| **记忆策略** | 单次生成记忆摘要 | 自动回忆 + 自动捕获 + 显式工具 |
| **消息处理** | 简单角色过滤和重排序 | 完整的转录修复、结构化管理 |
| **部署模式** | 单一服务部署 | 本地子进程 + 远程客户端双模式 |
| **配置系统** | 无（硬编码） | 完整的配置解析、验证、迁移 |
| **CLI 界面** | 无 | 安装器、设置向导、斜杠命令 |
| **诊断能力** | 基础 JSON 响应 | 结构化诊断日志、健康检查脚本 |

---

## 实现优先级建议

### P0 — 基础协议扩展（必须先做）
1. 扩展 Higo 插件协议，增加 `after_turn` / `compact` 回调模式
2. 实现 `OpenVikingClient` 类，封装所有 OpenViking HTTP API
3. 配置系统：支持 `ov.conf` 解析和环境变量注入

### P1 — 核心记忆能力
4. 实现 `assemble()` 和 `afterTurn()` 生命周期方法
5. 实现自动回忆（`before_prompt_build` Hook 等价物）
6. 实现自动捕获（语义 + 关键词模式）
7. 实现 `memory_recall`、`memory_store`、`memory_forget` 工具

### P2 — 高级功能
8. 4 层上下文分区和 Token 预算管理
9. Tool Call 转录修复
10. 资源/技能导入工具
11. 归档展开和搜索

### P3 — 运维和部署
12. 本地/远程双模式运行时
13. 进程管理和端口自动分配
14. 交互式设置向导
15. 健康检查脚本
