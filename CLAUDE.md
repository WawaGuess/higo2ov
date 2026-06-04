# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 提供本仓库的代码协作指引。

## 项目概述

本项目是 **Higo Session Memory Plugin** —— 一个 FastAPI 服务，用于拦截会话消息并在当前用户消息前注入一段由记忆引擎生成的摘要。它实现了 **Higo V2 插件协议**，包含四种模式：`probe`（健康检查）、`transform`（消息改写）、`result`（轮次结果回调）和 `memory_query`（记忆查询）。

## 开发命令

```bash
# 安装依赖
pip install -r requirements.txt

# 启动开发服务器（端口 8000，自动重载）
python main.py

# 或直接用 uvicorn 启动
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

本仓库未配置测试套件、lint 规则或构建工具。

## 架构

### 请求流转

1. **入口**：`main.py` 暴露唯一的 `POST /` 端点，根据 `mode` 字段路由请求。
2. **模型**：`models.py` 定义了 Higo V2 插件协议的 Pydantic v2 模型（`ProbeRequest`、`TransformRequest`、`ResultRequest` 及对应的响应模型）。
3. **记忆引擎**：`engine/openviking_engine.py` 实现了 `OpenVikingMemoryEngine`。引擎的 `generate_memory(session_id, messages)` 异步方法生成被注入会话的记忆文本。

### 消息重建（`main.py` 中的 `_build_messages`）

Transform 端点按照 V2 协议重建消息列表，输出顺序为：

1. `system` 消息（保留原始内容）
2. `user` —— 注入的记忆消息（来自引擎）
3. `user` —— 上下文/环境信息（保留原始内容）
4. `user` —— 当前用户消息（始终在最后）

修改 `_build_messages` 时，必须保持上述顺序不变 —— Higo 客户端依赖最后一条消息为当前用户输入。

### V2 协议字段

**Transform 请求关键字段：**
- `session.sessionId` —— 会话标识符
- `round.roundId` / `round.seq` / `round.startedAt` —— 轮次信息
- `request.messages` —— 消息列表 `[system, user(context), user(current)]`
- `meta.modelContextWindowTokens` —— 模型上下文窗口大小

**Transform 响应结构：**
- `ok` —— 始终为 `true`
- `summary` —— 状态描述
- `result.request.messages` —— 修改后的消息列表
- `result.pluginContext` —— 可选的插件上下文（例如 `memoryRevision`）

注意：V2 协议不使用 `anchor`、顶层 `sessionId` 或 `debug` 字段。
