# Higo2OV 与 OpenClaw-Plugin 功能对比

本文档按当前代码状态对比 higo2ov 与 OpenClaw-Plugin（OpenViking）的能力差异。

---

## 概览

| 维度 | higo2ov 当前实现 | OpenClaw-Plugin |
|------|------------------|-----------------|
| 定位 | Higo 的 Session Memory HTTP 插件 | OpenClaw 的 Context Engine 插件 |
| 运行方式 | FastAPI 独立服务，Higo 通过 HTTP 调用 | Node.js 插件，嵌入 OpenClaw 生命周期 |
| 协议 | Higo V2：`probe` / `transform` / `result`，另有 `/compact` | OpenClaw hooks/tools/context engine |
| 后端 | 已集成 OpenViking HTTP API | OpenViking |
| 主要能力 | 自动捕获、自动召回、记忆注入、异步/强制归档 | 自动捕获、自动召回、显式工具、本地运行时管理 |

---

## 已实现能力

| 能力 | higo2ov 实现方式 | 与 OpenClaw-Plugin 的差异 |
|------|------------------|----------------------------|
| 健康检查 | `mode=probe` 调用 OpenViking `GET /health` | 通过 Higo 插件协议暴露，不是 OpenClaw 注册式健康检查 |
| 当前 user 捕获 | `transform` 中识别最后一条 user，合并 system 前缀后写入 OV session | 不捕获 transform 中的 assistant；assistant 在 `result` 阶段捕获 |
| assistant/tool 捕获 | `mode=result` 读取 `message.sections`，`content` 写为 assistant，`tool` 写为 tool part | 依赖 Higo V2 result 回调；不是 OpenClaw `afterTurn` hook |
| 自动召回 | `transform` 中搜索 `viking://user/memories`、`viking://agent/memories`，可选 `viking://resources` | 作为 HTTP transform 的一部分执行 |
| 召回后处理 | URI 去重、叶子记忆过滤、score 阈值过滤、query-aware rerank | 没有完整上下文分区预算，只对注入记忆做预算 |
| 记忆注入 | 将 memory 作为独立 `user` 消息插入到第一个 user 前 | 不改 system prompt；必须满足 Higo 对原始消息保留和 current user 位置校验 |
| Session ID 映射 | UUID 直接使用，非 UUID 使用 sha256 | 与 OpenViking session 存储兼容，旧原始 sessionId 数据需迁移 |
| 异步归档 | transform/result 后调度 `_maybe_commit()`，超过阈值时 `commit(wait=false)` | 不阻塞 Higo 响应 |
| 强制归档 | `POST /compact` 调用 `commit(wait=true)` 并返回归档摘要和 token 估算 | 不是 Higo V2 标准 mode，是额外 HTTP 端点 |
| Bypass | `OPENVIKING_BYPASS_SESSION_PATTERNS` glob-like 匹配原始 sessionId | 只作用于当前服务内部处理 |
| 诊断日志 | `openviking: diag {...}` 和分层日志前缀 | 没有独立健康检查脚本 |
| 配置 | `.env` + `OpenVikingConfig` | 没有 OpenClaw 风格安装器/交互式配置向导 |

---

## 部分实现或行为不同

| 能力 | 当前状态 | 说明 |
|------|----------|------|
| Context assemble | 部分实现 | higo2ov 获取 OV session context，并把 summary/archive/memories 组装为 memory 文本；但不会像 OpenClaw 那样重建完整四层上下文。 |
| Token 预算 | 部分实现 | `recall_token_budget` 只限制 `<relevant-memories>` 行；没有完整 prompt budget allocator。 |
| Capture decision | 工具函数已存在，捕获链路未接入 | `get_capture_decision()`、`capture_mode`、`capture_max_length` 已在代码中，但当前 transform/result 捕获只做文本清理后写入。 |
| Result 幂等 | 基础实现 | 以进程内 `roundId` set 去重；服务重启后去重状态会丢失。 |
| Tool 结构化捕获 | 部分实现 | result sections 的 `toolCallId/toolname/toolargs/toolrsp` 会写入 tool part；没有 OpenClaw 的完整 tool-use/result 转录修复。 |
| Agent/用户作用域 | 部分实现 | 支持 AgentResolver 和 target URI 规范化；召回和写入仍受当前 HTTP client header 行为限制。 |

---

## 尚未实现能力

| 能力 | 缺口 |
|------|------|
| 显式记忆工具 | 未提供 `memory_recall`、`memory_store`、`memory_forget`、`ov_archive_expand` 等用户可调用工具。 |
| 资源/技能导入 | 未提供资源导入、技能导入、`ov_search`、`ov_import` 等工具能力。 |
| 本地 OpenViking 进程管理 | 不负责安装、启动、守护或端口管理 OpenViking；需要外部先启动 OV 服务。 |
| 交互式设置向导 | 没有 OpenClaw 风格 setup wizard、安装器、升级/回滚机制。 |
| 端到端健康检查脚本 | 没有独立脚本自动验证注入、捕获、commit、记忆提取和回忆闭环。 |
| 转录修复 | 没有完整 tool-call/result 配对修复、缺失结果合成、错位消息移动等能力。 |
| 超时降级包装 | OpenViking HTTP client 有请求超时配置，但召回链路没有单独的 5s 降级包装。 |

---

## 当前优先级建议

| 优先级 | 建议 | 理由 |
|--------|------|------|
| P0 | 为 transform/result 捕获链路接入 `get_capture_decision()` 或明确移除相关配置 | 当前配置项存在但不生效，容易让部署者误判行为。 |
| P1 | 增加端到端健康检查脚本 | 能验证 Higo2OV/OpenViking/Higo 三方闭环，比单独 probe 更有价值。 |
| P2 | 补充显式记忆管理接口 | 方便调试和人工修正记忆，但不影响自动记忆主流程。 |
| P3 | 增加完整上下文预算与转录修复 | 复杂度较高，适合在核心协议稳定后推进。 |
