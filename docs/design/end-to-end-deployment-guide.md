# Higo2OV 端到端部署与对接指南

本文档描述如何从 0 到 1 完成 Higo、Higo2OV（本代码仓）、OpenViking 三方的部署与对接，覆盖每个环节的配置项、启动命令和验证方法。

---

## 1. 架构与数据流

```
┌─────────────┐      HTTP POST       ┌──────────────────────┐      HTTP      ┌─────────────────┐
│    Higo     │ ───────────────────► │    higo2ov           │ ─────────────► │   OpenViking    │
│  (Server)   │  mode=probe          │  (FastAPI Plugin)    │                │    (Server)     │
│             │  mode=transform      │  Port: 8000          │                │  Port: 1933     │
│             │  mode=result         │                      │                │                 │
└─────────────┘                      └──────────────────────┘                └─────────────────┘
       ▲                                                                            ▲
       │                                                                            │
       └─────────────────────────── 对话消息流 ─────────────────────────────────────┘
```

**请求流向**:
1. Higo 将用户消息发送给 higo2ov (`POST /`, `mode=transform`)
2. higo2ov 调用 OpenViking API 进行记忆召回和消息捕获
3. higo2ov 将注入记忆后的消息返回给 Higo
4. Higo 调用 LLM 生成回复
5. Round 结束时 Higo 将结果回调给 higo2ov (`POST /`, `mode=result`)
6. higo2ov 将 assistant 回复和 tool 结果写入 OpenViking

---

## 2. 第一步：部署 OpenViking

### 2.1 环境要求

- Python 3.10+
- OpenViking 服务端已安装并运行
- 当前使用Openviking版本为0.3.12版本

### 2.2 启动 OpenViking 服务

参考 OpenViking 项目文档启动服务，确保监听在可访问地址：

```bash
# 示例：OpenViking 默认监听端口 1933
python -m openviking.server.bootstrap --host 127.0.0.1 --port 1933
```

验证服务是否启动：

```bash
curl http://127.0.0.1:1933/health
# 期望返回: {"status": "ok"}
```

### 2.3 OpenViking 配置项

OpenViking 服务端通常需要以下配置（具体参考 OpenViking 官方文档）：

| 配置项 | 说明    | 建议值                                |
|--------|-------|------------------------------------|
| `host` | 监听地址  | `0.0.0.0`（允许远程访问）或 `127.0.0.1`（仅本地） |
| `port` | 监听端口  | `1933`                             |
| `data_dir` | 数据存储目录 | `./data`                           |
| `vlm` | vlm模型 | 根据 OpenViking 配置，不涉及图片可以配置llm模型    |
| `embedding_model` | 嵌入模型  | 根据 OpenViking 配置                   |

> **注意**: 如果 higo2ov 和 OpenViking 不在同一台机器上，需要配置 `host=0.0.0.0` 并确保网络可达。

---

## 3. 第二步：部署 higo2ov

### 3.1 环境要求

- Python 3.10+
- pip

### 3.2 安装依赖

```bash
# 进入项目目录
cd /path/to/higo2ov

# 创建虚拟环境（推荐）
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# 或 .venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

`requirements.txt` 应包含：

```
fastapi
uvicorn
httpx
pydantic>=2.0
python-dotenv
```

### 3.3 配置环境变量

在项目根目录创建 `.env` 文件：

```bash
# ─── OpenViking 连接配置 ───
OPENVIKING_BASE_URL=http://127.0.0.1:1933
OPENVIKING_API_KEY=your-api-key-if-needed
OPENVIKING_AGENT_ID=default
OPENVIKING_ACCOUNT_ID=your-account-id
OPENVIKING_USER_ID=your-user-id

# ─── HTTP 超时 ───
OPENVIKING_TIMEOUT_MS=30000

# ─── 自动归档阈值 ───
OPENVIKING_COMMIT_TOKEN_THRESHOLD=8000

# ─── 记忆召回配置 ───
OPENVIKING_RECALL_LIMIT=10
OPENVIKING_RECALL_SCORE_THRESHOLD=0.1
OPENVIKING_RECALL_TOKEN_BUDGET=2000
OPENVIKING_RECALL_RESOURCES=false

# ─── 捕获与召回开关 ───
OPENVIKING_AUTO_CAPTURE=true
OPENVIKING_AUTO_RECALL=true

# ─── 捕获模式 ───
OPENVIKING_CAPTURE_MODE=semantic
OPENVIKING_CAPTURE_MAX_LENGTH=8192

# ─── Session 隔离配置 ───
OPENVIKING_ISOLATE_USER_SCOPE_BY_AGENT=false
OPENVIKING_ISOLATE_AGENT_SCOPE_BY_USER=true

# ─── Bypass 配置 ───
OPENVIKING_BYPASS_SESSION_PATTERNS=""

# ─── 诊断日志 ───
OPENVIKING_EMIT_DIAGNOSTICS=true
```

#### 配置项详细说明

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `OPENVIKING_BASE_URL` | 是 | `http://127.0.0.1:1933` | OpenViking 服务地址 |
| `OPENVIKING_API_KEY` | 否 | `""` | API 认证密钥（如 OpenViking 未启用认证可留空） |
| `OPENVIKING_AGENT_ID` | 否 | `default` | 默认 Agent ID，用于 OpenViking 多租户路由 |
| `OPENVIKING_ACCOUNT_ID` | 否 | `""` | 账户 ID，传递给 OpenViking 的 `X-OpenViking-Account` 头 |
| `OPENVIKING_USER_ID` | 否 | `""` | 用户 ID，传递给 OpenViking 的 `X-OpenViking-User` 头 |
| `OPENVIKING_TIMEOUT_MS` | 否 | `30000` | 调用 OpenViking API 的超时时间（毫秒）。**注意**：Higo 对 higo2ov 的 transform 超时是 10000ms，建议此值小于 10000ms 以留出处理余量 |
| `OPENVIKING_COMMIT_TOKEN_THRESHOLD` | 否 | `8000` | **自动归档触发阈值**。当 session 的 `pending_tokens` 超过此值时，自动触发 commit |
| `OPENVIKING_RECALL_LIMIT` | 否 | `10` | 每次召回返回的最大记忆条数 |
| `OPENVIKING_RECALL_SCORE_THRESHOLD` | 否 | `0.1` | 召回最低相似度阈值（0~1） |
| `OPENVIKING_RECALL_TOKEN_BUDGET` | 否 | `2000` | 注入记忆的 token 预算上限 |
| `OPENVIKING_RECALL_RESOURCES` | 否 | `false` | 是否同时搜索 `viking://resources` |
| `OPENVIKING_AUTO_CAPTURE` | 否 | `true` | 是否自动将对话消息捕获到 OpenViking |
| `OPENVIKING_AUTO_RECALL` | 否 | `true` | 是否在 transform 时自动召回相关记忆 |
| `OPENVIKING_CAPTURE_MODE` | 否 | `semantic` | 消息捕获模式：`semantic`（全部捕获）或 `keyword`（仅匹配关键词） |
| `OPENVIKING_CAPTURE_MAX_LENGTH` | 否 | `8192` | 单条消息最大捕获长度（字符） |
| `OPENVIKING_BYPASS_SESSION_PATTERNS` | 否 | `""` | 跳过处理的 session 模式，逗号分隔的 glob 表达式，如 `agent:*:cron:**` |
| `OPENVIKING_EMIT_DIAGNOSTICS` | 否 | `true` | 是否输出结构化诊断日志 |

### 3.4 启动 higo2ov

#### 方式一：开发模式（带热重载）

```bash
python main.py
```

或：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

#### 方式二：生产模式

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

> **注意**：`--host 0.0.0.0` 允许外部访问。如果 Higo 和 higo2ov 在同一台机器上，可使用 `--host 127.0.0.1`。

### 3.5 验证 higo2ov 启动

```bash
# 健康检查（模拟 Higo probe）
curl -X POST http://127.0.0.1:8000/ \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "probe",
    "protocolVersion": "2026-05-25",
    "source": "higo",
    "session": {"sessionId": "test-session"},
    "timestamp": "2026-05-26T10:00:00Z"
  }'

# 期望返回:
# {"ok": true, "summary": "probe ok", "engine": {"name": "higo-openviking-bridge", "version": "1.1.0"}}
```

---

## 4. 第三步：在 Higo 中配置接入 higo2ov

### 4.1 前置条件：开启前端配置面板和后端功能

Higo 前端的自定义记忆引擎配置面板默认关闭，需要通过环境变量开启：

```bash
# Higo 前端构建时设置
VITE_SESSION_MEMORY_ENGINE_SETTINGS_ENABLED=true
```

设置后重新构建并部署 Higo 前端，右侧边栏会出现「会话配置」Tab。

在后端的.env中也需要添加
```bash
# 会话配置入口开关
SESSION_MEMORY_ENGINE_SETTINGS_ENABLED=true
```

### 4.2 Higo 配置流程（按 Session 配置）

Higo 的自定义记忆引擎配置是**按 Session 级别**管理的，每个会话独立配置。配置流程如下：

#### 步骤 1：进入会话配置面板

1. 打开 Higo 前端，进入任意 Session
2. 点击右侧边栏的「会话配置」Tab（齿轮图标）

#### 步骤 2：填写 Endpoint

在「Endpoint」输入框中填写 higo2ov 的服务地址：

```
http://192.168.1.100:8000
```

> **注意**：
> - 如果 Higo 和 higo2ov 在同一台机器上，使用 `http://127.0.0.1:8000`
> - 如果在不同机器上，使用 higo2ov 所在机器的内网 IP 或域名
> - 不需要填写路径后缀（如 `/probe`），Higo 会自动发送 `POST /`

#### 步骤 3：测试连通性

点击「测试 endpoint」按钮，Higo 会发送 probe 请求：

```json
{
  "mode": "probe",
  "protocolVersion": "2026-05-25",
  "source": "higo",
  "session": {"sessionId": "<当前sessionId>"},
  "timestamp": "2026-05-26T10:00:00.000Z"
}
```

- **测试超时**：3000ms
- **测试成功条件**：`response.ok == true` 且 `payload.ok == true`
- 测试成功后，界面显示「最近测试：成功」

#### 步骤 4：启用插件

开启「启用自定义记忆引擎」开关，然后点击「保存配置」。

> **重要**：Higo 后端强制要求**启用前必须先测试成功**。如果 endpoint 变更，也需要重新测试后才能保存启用。

#### 配置数据存储

配置持久化在 Higo 数据库的 `session_memory_engines` 表中：

| 字段 | 说明 |
|------|------|
| `session_id` | 会话 ID（主键） |
| `enabled` | 是否启用 |
| `endpoint` | higo2ov 服务地址 |
| `last_test_status` | 最近测试状态：`unknown` / `success` / `error` |
| `last_test_message` | 最近测试结果消息 |
| `last_tested_at` | 最近测试时间 |

---

### 4.3 Higo 配置流程（按全局配置引擎后端）

除上述的按 Session 配置外，Higo 还支持全局配置记忆引擎后端。全局配置会覆盖单 Session 配置，适用于大多数 Session 使用同一记忆引擎的场景。

在 Higo 后端 `.env` 中添加：

```bash
GLOBAL_MEMORY_ENGINE_ENDPOINT=http://192.168.1.100:8000
```

## 5. Higo 与 higo2ov 的协议约定

### 5.1 Higo 调用 higo2ov 的接口规范

| 调用类型 | HTTP 方法 | 路径 | 超时 | 说明 |
|----------|-----------|------|------|------|
| Probe | POST | `/` | 3000ms | 连通性测试 |
| Transform | POST | `/` | 10000ms | 消息转换（每轮用户消息触发一次） |
| Result | POST | `/` | 5000ms | Round 结束回调 |

### 5.2 Higo 对 Transform 响应的严格校验

Higo 后端会严格校验 transform 响应，不满足以下任一条件将抛出 `SESSION_MEMORY_ENGINE_INVALID_RESPONSE` 错误：

1. **`result.request` 只能包含 `messages` 一个字段**，不能有多余字段
2. **必须完整保留以下三条消息**（通过 JSON 序列化匹配）：
   - `system` 消息
   - `contextEnvironment` 消息
   - `currentUser` 消息
3. **最后一条 `role="user"` 的消息必须是 `currentUser`**

当前 higo2ov 的 `_build_messages` 实现（在第一个 user 前插入 memory）满足以上所有校验：

```
system → user(memory) → user(context env) → user(current)
                        ↑                    ↑
                   保留原消息            最后一条 user
```

### 5.3 Higo 对 Result 回调的处理

Higo 对 result 回调采用**异步 fire-and-forget** 策略：
- result 失败仅记录 warn 日志，**不影响用户已收到的回复**
- 不会阻塞后续流程
- 因此 higo2ov 的 result 处理也应尽量轻量，不阻塞响应

---

## 6. 端到端验证流程

### 6.1 验证 OpenViking 连通性

```bash
curl http://<openviking-host>:1933/health
```

### 6.2 验证 higo2ov 独立运行

```bash
# Probe
curl -X POST http://<higo2ov-host>:8000/ \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "probe",
    "protocolVersion": "2026-05-25",
    "source": "higo",
    "session": {"sessionId": "s1"},
    "timestamp": "2026-05-26T10:00:00Z"
  }'

# Transform（模拟一轮对话）
curl -X POST http://<higo2ov-host>:8000/ \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "transform",
    "protocolVersion": "2026-05-25",
    "source": "higo",
    "session": {"sessionId": "s1", "contextPath": "/"},
    "round": {"roundId": "r1", "seq": 1, "startedAt": "2026-05-26T10:00:00Z"},
    "request": {
      "messages": [
        {"role": "system", "content": "你是一个助手"},
        {"role": "user", "content": "Context environment: local test"},
        {"role": "user", "content": "我喜欢用 TypeScript 编程"}
      ]
    },
    "meta": {"modelContextWindowTokens": 128000}
  }'

# Result（模拟 round 结束回调）
curl -X POST http://<higo2ov-host>:8000/ \
  -H "Content-Type: application/json" \
  -d '{
    "mode": "result",
    "protocolVersion": "2026-05-25",
    "source": "higo",
    "session": {"sessionId": "s1", "contextPath": "/"},
    "round": {"roundId": "r1", "seq": 1, "status": "completed", "finishReason": "stop", "startedAt": "2026-05-26T10:00:00Z", "endedAt": "2026-05-26T10:00:08Z"},
    "transform": {"applied": true},
    "message": {
      "seq": 1,
      "usermsg": "我喜欢用 TypeScript 编程",
      "systemPrompt": "你是一个助手",
      "contextEnvironment": "",
      "sections": [
        {"subSeq": 0, "type": "content", "content": "好的，我记住了你喜欢 TypeScript。"}
      ]
    },
    "errors": []
  }'
```

### 6.3 验证 Compact 端点

```bash
curl -X POST http://<higo2ov-host>:8000/compact \
  -H "Content-Type: application/json" \
  -d '{"sessionId": "s1"}'
```

### 6.4 通过 Higo 前端验证

1. 在 Higo 前端进入目标 Session
2. 打开「会话配置」→ 填写 higo2ov endpoint → 点击「测试 endpoint」
3. 测试成功后，开启「启用自定义记忆引擎」→ 点击「保存配置」
4. 发送一条用户消息
5. 观察 higo2ov 日志应出现 `[transform] sessionId=...` 和 `[result] sessionId=...`

### 6.5 日志检查清单

在 higo2ov 日志中应看到以下关键日志：

| 阶段 | 期望日志 |
|------|---------|
| Transform | `[transform] sessionId=... msg_count=...` |
| Capture | `[capture] stored current_user ovSessionId=...` |
| Context | `[generate_memory] context fetched... overview=...` |
| Recall | `[recall] query=...` → `[recall] after rerank=N` |
| Assemble | `[assemble] overview=True abstracts=N memories=N text_len=N` |
| Result | `[result] sessionId=... roundId=... captured=N` |
| Commit Check | `[commit_check] ovSessionId=... pending_tokens=N threshold=N` |

---

## 7. 常见问题排查

### 7.1 Higo 前端没有「会话配置」Tab

**原因**: 前端环境变量 `VITE_SESSION_MEMORY_ENGINE_SETTINGS_ENABLED` 未设置为 `true`

**解决**: 在 Higo 前端 `.env` 文件中添加：

```bash
VITE_SESSION_MEMORY_ENGINE_SETTINGS_ENABLED=true
```

然后重新构建部署。

### 7.2 Higo 测试 endpoint 失败

**现象**: 点击「测试 endpoint」后显示「最近测试：失败」

**排查步骤**:
1. 确认 higo2ov 已启动：使用 6.2 中的 Probe 请求应返回 `{"ok": true, ...}`
2. 确认 Higo 前端填写的 endpoint 正确（IP 和端口）
3. 从 Higo 后端服务器直接测试：`curl -X POST <endpoint> -d '{"mode":"probe",...}'`
4. 检查 higo2ov 日志是否有 `[handle] mode=probe` 记录
5. 检查防火墙和网络安全组

### 7.3 higo2ov 无法连接到 OpenViking

**现象**: higo2ov 日志报错 `[probe] OpenViking health check failed`

**排查步骤**:
1. 确认 OpenViking 已启动：`curl http://<openviking-host>:1933/health`
2. 检查 `.env` 中的 `OPENVIKING_BASE_URL` 是否正确
3. 如果 OpenViking 需要认证，确认 `OPENVIKING_API_KEY` 已配置
4. 检查网络连通性：从 higo2ov 所在机器执行 `curl http://<openviking-host>:1933/health`

### 7.4 Higo 报 `SESSION_MEMORY_ENGINE_INVALID_RESPONSE`

**现象**: Higo 后端返回 transform 响应无效

**排查步骤**:
1. 检查 higo2ov 返回的 `result.request` 是否只包含 `messages` 一个字段
2. 检查是否完整保留了 system、context_env、current_user 三条消息
3. 检查最后一条 `role="user"` 是否是 current_user
4. 对比 Higo 发送的原始消息和 higo2ov 返回的消息，确认没有意外修改

### 7.5 记忆没有被注入

**现象**: Transform 返回的消息列表没有额外的 user(memory) 消息

**排查步骤**:
1. 检查 `OPENVIKING_AUTO_RECALL=true`
2. 检查用户输入是否被 `prepare_recall_query()` 过滤为空（太短、全是噪音）
3. 检查 OpenViking 中是否已有记忆数据（新 session 首次调用时通常无记忆）
4. 查看日志 `[recall] after rerank=N`，如果为 0 说明搜索无结果
5. 检查是否被 bypass：`[generate_memory] session bypassed`

### 7.6 记忆没有被捕获

**现象**: OpenViking 中看不到对话历史

**排查步骤**:
1. 检查 `OPENVIKING_AUTO_CAPTURE=true`
2. 检查文本是否被 `sanitize_user_text_for_capture()` 清理为空（例如 HEARTBEAT、只包含已注入 memory 块等）
3. 查看日志 `[capture] stored current_user...` 或 `[capture_result] stored assistant...`
4. 检查 `OPENVIKING_COMMIT_TOKEN_THRESHOLD`：如果 `pending_tokens` 未超过阈值，数据只在 OV session 中，尚未归档为记忆

### 7.7 commit_token_threshold 如何调整

`commit_token_threshold` 控制自动归档的触发时机：

| 场景 | 建议值 | 说明 |
|------|--------|------|
| 高频对话 | `5000` | 更快归档，减少单 session 数据量 |
| 低频对话 | `15000` | 减少归档频率，降低 overhead |
| 调试阶段 | `1000` | 更容易触发归档，方便验证记忆提取 |
| 生产环境 | `8000`（默认） | 平衡归档频率和记忆新鲜度 |

修改 `.env` 后重启 higo2ov 生效：

```bash
OPENVIKING_COMMIT_TOKEN_THRESHOLD=5000
```

---

## 8. 配置速查表

### 8.1 三方配置对照

| 组件 | 配置位置 | 关键配置项 | 说明 |
|------|---------|-----------|------|
| Higo 前端 | 构建环境变量 | `VITE_SESSION_MEMORY_ENGINE_SETTINGS_ENABLED` | 是否显示配置面板 |
| Higo 前端 | 会话配置面板 | `endpoint` | higo2ov 服务地址 |
| Higo 前端 | 会话配置面板 | `enabled` | 是否启用该 session 的插件 |
| Higo 后端 | 数据库表 `session_memory_engines` | `endpoint` / `enabled` | 按 session 持久化 |
| higo2ov | `.env` | `OPENVIKING_BASE_URL` | OpenViking 地址 |
| higo2ov | `.env` | `OPENVIKING_COMMIT_TOKEN_THRESHOLD` | 自动归档阈值 |
| higo2ov | `.env` | `OPENVIKING_AUTO_CAPTURE` | 是否捕获对话 |
| higo2ov | `.env` | `OPENVIKING_AUTO_RECALL` | 是否召回记忆 |
| OpenViking | 配置文件 | `host` / `port` | 监听地址 |
| OpenViking | 配置文件 | `data_dir` | 数据目录 |

### 8.2 启动命令速查

```bash
# 1. 启动 OpenViking
python -m openviking.server.bootstrap --host 0.0.0.0 --port 1933

# 2. 启动 higo2ov（开发）
uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# 3. 启动 higo2ov（生产）
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4

# 4. 启动 Higo（具体命令参考 Higo 官方文档）
higo server --config higo.config.yaml
```

### 8.3 验证命令速查

```bash
# OpenViking 健康检查
curl http://localhost:1933/health

# higo2ov 健康检查（probe）
curl -X POST http://localhost:8000/ -H "Content-Type: application/json" \
  -d '{"mode":"probe","protocolVersion":"2026-05-25","source":"higo","session":{"sessionId":"s1"},"timestamp":"2026-05-26T10:00:00Z"}'

# higo2ov 强制归档
curl -X POST http://localhost:8000/compact -H "Content-Type: application/json" \
  -d '{"sessionId":"s1"}'
```
