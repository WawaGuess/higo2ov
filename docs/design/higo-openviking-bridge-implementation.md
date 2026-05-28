# Higo OpenViking Bridge — 代码逻辑实现文档

> 本文档描述当前代码的实际实现逻辑，对应 `main` 分支最新状态。

---

## 1. 项目概述

本项目是一个 **Higo Session Memory Plugin**，基于 FastAPI 构建，实现 **Higo V2 插件协议**。核心功能分布在 `transform` 和 `result` 两个阶段：

1. `transform` 阶段捕获当前 user 输入，并从 OpenViking 召回相关记忆
2. 将记忆文本注入到消息列表中，供 LLM 参考
3. `result` 阶段捕获 assistant 回复和 tool 结果
4. `memory_query` 阶段提供记忆查询工具能力（占位实现）

同时提供 `probe` 健康检查和一个独立的 `/compact` 强制归档端点。

---

## 2. 模块架构

```
main.py                          ← FastAPI 入口，路由分发
models.py                        ← Higo 协议 Pydantic 模型
engine/
├── config.py                    ← 配置定义与环境变量加载
├── memory.py                    ← 记忆引擎抽象基类（可扩展）
├── openviking_engine.py         ← 核心引擎（OpenViking 实现）
├── openviking_client.py         ← OpenViking HTTP API 客户端
├── text_utils.py                ← 文本清理、Capture Decision、Query 预处理
├── memory_ranking.py            ← 记忆后处理：去重、过滤、重排序、预算管理
├── session_utils.py             ← Session ID 映射（UUID / sha256）
├── bypass.py                    ← Session Bypass 模式匹配
├── diagnostics.py               ← 结构化诊断日志
└── agent_resolver.py            ← Session-Agent ID 解析与缓存
```

### 依赖关系

- `main.py` 依赖 `models.py` + `engine` 包
- `openviking_engine.py` 依赖 `config`, `client`, `text_utils`, `memory_ranking`, `session_utils`, `bypass`, `diagnostics`, `agent_resolver`
- `openviking_client.py` 依赖 `config`
- 其余模块相互独立，无循环依赖

---

## 3. 请求处理流程

### 3.1 入口路由 `POST /`

`main.py:handle()` 接收所有 Higo 请求，按 `mode` 字段分发：

| mode | 处理函数 | 说明 |
|------|---------|------|
| `probe` | `_handle_probe()` | 健康检查 |
| `transform` | `_handle_transform()` | 消息转换（核心） |
| `result` | `_handle_result()` | Round 结束结果回调 |
| `memory_query` | `_handle_memory_query()` | 记忆查询工具（占位） |
| 其他 | 返回 400 | 未知 mode |

所有请求和响应都会记录完整 JSON 到日志（`[higo_request]` / `[higo_response]`）。

### 3.2 Probe 模式

`_handle_probe()`（`main.py:81-102`）：

1. 调用 `_ov_client.health_check()` 检查 OpenViking 服务可达性
2. 返回 `ProbeResponse { ok, summary, engine { name, version } }`
3. 异常时 `ok=false`，summary 包含错误信息

### 3.3 Transform 模式

`_handle_transform()`（`main.py:105-164`）是核心流程：

```
1. 记录原始消息列表（role、content_len）
2. 调用 memory_engine.generate_memory(sessionId, messages)
   └─ 返回 memory_text（可能为空）
3. 若 memory_text 非空：
   └─ 构造 memory_message(role="user", content=memory_text)
   └─ 调用 _build_messages(original, memory_message) 插入到第一个 user 之前
4. 若 memory_text 为空：
   └─ 直接返回原始消息列表（不注入）
5. 返回 TransformResponse { ok, result { request { messages }, pluginContext }, summary }
```

#### 消息插入规则 `_build_messages()`

记忆消息插入到**第一个 user 消息之前**。在 Higo V2 的消息结构中，第一个 user 通常是 context environment，最后一个 user 是 current user，因此该插入方式既能让记忆位于上下文前，也能确保最后一条 user 仍然是当前用户输入。

```
Original:  [0]system [1]user(context) [2]user(current)
After:     [0]system [1]user(memory) [2]user(context) [3]user(current)
```

实现逻辑：先扫描找到第一个 `role="user"` 的索引，再遍历插入。若没有 user 消息，则将 memory 追加到末尾。

### 3.4 Result 模式

`_handle_result()` 负责处理 Higo 在 round 结束后的回调：

```
1. 从 request.session.sessionId 读取 session ID
2. 从 request.round 读取 roundId/status
3. 将 message.sections 交给 memory_engine.capture_round_result()
   └─ type="content" 保存为 assistant 文本消息
   └─ type="tool" 保存为 tool part
4. 异步触发 _maybe_commit()
5. 返回 ResultResponse { ok, summary, ack { roundId, stored, memoryRevision } }
```

`roundId` 在 `capture_round_result()` 中用于幂等去重，避免同一 round 重复回调时重复写入 OpenViking。

### 3.5 Memory Query 模式

`_handle_memory_query()` 是记忆查询工具端点，目前为**占位实现**。

支持两种 action：

| action | 说明 |
|--------|------|
| `help` | 返回查询语法帮助信息 |
| `query` | 执行记忆查询 |

**当前行为**：无论 action 为何值，均返回：

```json
{
  "ok": false,
  "summary": "当前功能不可用，暂不支持",
  "result": {},
  "engine": {
    "name": "higo-openviking-bridge",
    "version": "1.1.0"
  }
}
```

**请求模型**（`models.py:151-158`）：

```python
class MemoryQueryRequest(BaseModel):
    mode: Literal["memory_query"] = "memory_query"
    action: Literal["help", "query"]
    protocolVersion: str | None = None
    source: str | None = None
    session: Session | None = None
    anchor: Anchor | None = None      # { seq, subSeq }
    query: dict | None = None         # action=query 时携带
```

**响应模型**（`models.py:161-165`）：

```python
class MemoryQueryResponse(BaseModel):
    ok: bool = True
    summary: str
    result: dict | None = None
    engine: EngineInfo | None = None
```

---

## 4. 核心引擎：`OpenVikingMemoryEngine`

`engine/openviking_engine.py:32-747`

### 4.1 初始化

```python
self.config = config                    # OpenVikingConfig
self.client = client                    # OpenVikingClient
self._agent_resolver = AgentResolver(config.agent_id)
self._bypass_patterns = compile_session_patterns(
    [p.strip() for p in config.bypass_session_patterns.split(",") if p.strip()]
)
```

### 4.2 主入口 `generate_memory()`

整体流程（5 步）：

```
Step 0: Session ID 映射 + Bypass 检查
Step 1: Capture messages（消息捕获）
Step 2: Get session context（获取会话上下文）
Step 3: Recall memories（记忆召回）
Step 4: Assemble memory text（组装记忆文本）
Step 5: Async commit（异步提交）
```

#### Step 0: Session ID 映射与 Bypass

- 调用 `session_to_ov_id(session_id)` 将 Higo sessionId 映射为 OpenViking 存储 ID
  - UUID 格式 → 直接使用（小写）
  - 非 UUID → `sha256(session_id).hexdigest`
- 检查 `should_bypass_session(session_id, self._bypass_patterns)`
  - 若匹配 bypass 模式 → 直接返回空字符串（跳过所有处理）

#### Step 1: Capture Messages（`_capture_messages`）

仅在 `auto_capture=True` 时执行。

**核心策略：transform 只捕获当前 user 输入**，assistant 回复和 tool 结果由 `result` 回调捕获。

消息分类：

| 消息角色 | 处理方式 |
|---------|---------|
| `system` | 不单独保存，合并到最后一条 user 的首个 text part |
| `assistant` | 跳过，交由 `result` 回调捕获 |
| `user`（倒数第2条及以前） | 跳过（context env） |
| `user`（最后一条） | 保存为 current user |

当前 `_capture_messages()` 不调用 `get_capture_decision()`；捕获前只对 text part 执行 `sanitize_user_text_for_capture()`，若清理后仍有文本则写入 OpenViking。`capture_mode`、`capture_max_length` 和 `get_capture_decision()` 目前是可复用的工具能力，但不在当前 transform 捕获链路中生效。

**User 消息存储**：
- 通过 `message_to_ov_parts()` 转为 OV parts
- 若存在 system 消息，将其内容合并到首个 text part：`[system] {system_text}\n\n{user_text}`
- text part 经过 `sanitize_user_text_for_capture()`
- 调用 `client.add_session_message(role="user", ...)`

**Result 消息存储**：
- `type="content"`：清理后调用 `client.add_session_message(role="assistant", role_id="assistant", ...)`
- `type="tool"`：构造 `{type:"tool", tool_id, tool_name, tool_input, tool_output}`，以 `role="user", role_id="user"` 写入

**Agent ID**：调用 `self._resolve_agent_id(session_id)`，通过 `AgentResolver` 解析：
- 从 `sessionId` 中提取 `agent:xxx:` 前缀中的 agentId
- 若配置了 `agent_id` 且不为 `default`，前缀格式为 `{config_agent_id}_{raw_agent_id}`

#### Step 2: Get Session Context

调用 `client.get_session_context(ov_session_id)` 获取：
- `latest_archive_overview` — 最新归档摘要
- `pre_archive_abstracts` — 历史归档索引
- `messages` — 当前活跃消息（引擎内部不直接使用，由 OV 服务端管理）
- `estimatedTokens` — 预估 token 数

异常时记录 warning，context 为空 dict。

#### Step 3-4: Recall + Assemble（仅在 `auto_recall=True` 时执行）

**Query 提取**：
1. 从 messages 中提取最后一条 user 消息的 `content`
2. 经过 `prepare_recall_query()` 预处理：
   - `sanitize_user_text_for_capture()` 清理噪音
   - 截断到 500 字符（尽可能保留完整单词）

**召回搜索**（`_recall_memories`）：

单次全局搜索，不指定 `target_uri`，参数为：
- `limit: 20`
- `mode: "auto"`

返回结果先过滤掉目录描述文件（URI 以 `.abstract.md` 或 `.overview.md` 结尾），再进入后处理。

**后处理**（`pick_memories_for_injection`）：

1. **Query-aware 排序** — 综合 base score + leaf boost(0.12) + event temporal boost(0.1) + preference boost(0.08) + lexical overlap boost(0~0.2)
2. **去重** — event/case 按 URI 去重，其他按 `abstract:category:normalized_abstract` 去重
3. **Leaf 优先** — 先取所有 `level == 2` 的 leaf；若 leaf 数量 ≥ `recall_inject_limit`（默认 6），直接返回前 limit 条
4. **Fallback 补充** — leaf 不足时，从去重后的结果中补充非 leaf，直到达到 limit；补充时检查 score ≥ `recall_score_threshold`

**文本组装**（`_assemble_memory_text`）：

只输出 `<relevant-memories>` 块：

```
<relevant-memories>
- [{category}] {content} ({score})
...
</relevant-memories>
```

记忆行通过 `build_memory_lines_with_budget()` 构建，受 `recall_token_budget` 限制：
- 第一条记忆**强制包含**（即使超出预算）
- 后续记忆使用 `_count_tokens()`（tiktoken 精确估算，不可用则字符回退）检查预算，超出时停止追加

#### Step 5: Async Commit（`_maybe_commit`）

在 `generate_memory` 末尾通过 `asyncio.create_task()` 异步触发：

1. 调用 `client.get_session(ov_session_id)` 获取 `pending_tokens`
2. 若 `pending_tokens > commit_token_threshold`（默认 8000）：
   - 调用 `client.commit_session(ov_session_id, wait=False)`
   - 返回 `task_id` 用于 Phase 2 异步记忆提取
3. 否则跳过

### 4.3 强制归档 `compact()`

独立方法，供 `POST /compact` 调用：

1. 映射 session_id → ov_session_id
2. Bypass 检查
3. 预获取 session context 记录 `tokensBefore`
4. 调用 `commit_session(ov_session_id, wait=True)`
   - `wait=True` 会轮询 Phase 2 直到完成或超时（默认 300s）
5. 根据 commit 结果状态返回：
   - `failed` / `timeout` → `ok=false`
   - `archived=false` → `ok=true, compacted=false`
   - `archived=true` → 获取 post-compact context，返回 summary + tokensBefore/After

---

## 5. 文本处理模块：`text_utils.py`

### 5.1 文本清理 `sanitize_user_text_for_capture()`

清理顺序（严格按此顺序执行）：

| 步骤 | 正则/逻辑 | 说明 |
|------|----------|------|
| 1 | `\bHEARTBEAT(?:\.md\|_OK)\b` | HEARTBEAT 健康检查消息 → 整段清空 |
| 2 | `^System:\s*\[.*?\]\s*Compacted\s*(.+)$` | Compactor 系统消息 → 提取实际内容 |
| 3 | `<relevant-memories>[\s\S]*?</relevant-memories>` | 移除已注入的记忆块 |
| 4 | `Conversation info/metadata/会话信息/对话信息` + fenced code | 移除对话元数据块 |
| 5 | `Sender (...) : ```...``` ` | 移除 Sender 元数据块 |
| 6 | ````json ... ````（若含 ≥3 个 metadata keys） | 条件移除 fenced JSON |
| 7 | `^\[(Mon\|Tue\|...)?\s*日期时间\]\s*` | 移除 leading timestamp |
| 8 | `\u0000` | 移除 null bytes |
| 9 | `\s+ → " "` | 折叠多余空白 |

Metadata keys 检测集合：`session, sessionid, sessionkey, conversationid, channel, sender, userid, agentid, timestamp, timezone`

### 5.2 Capture Decision `get_capture_decision()`

该函数位于 `text_utils.py`，当前实现可用，但 `OpenVikingMemoryEngine` 的 transform/result 捕获链路没有调用它。下面规则用于后续接入捕获决策时参考。

返回结构：`{ should_capture: bool, reason: str, normalized_text: str }`

**过滤规则执行顺序**：

1. **Empty** → `empty_text` / `injected_memory_context_only`
2. **Length** → `length_out_of_range`
   - CJK 文本最小长度为 0，Latin 文本最小长度为 10
   - 原始长度 ≤ `capture_max_length`
3. **Command** → `command_text`
4. **Non-content** → `non_content_text`
   - 使用 `unicodedata.category(ch)` 判断，仅 P/S/Z 类别通过
5. **Subagent** → `subagent_context`
6. **Question-only** → `question_text`
   - 含疑问词/问号，但不含记忆意图关键词
   - 多说话者（≥2 个 speaker tags）或长度 > 280 则豁免
7. **Mode 分支**：
   - `semantic` → `semantic_candidate`
   - `keyword` → 匹配 7 个 `MEMORY_TRIGGERS` 正则

### 5.3 Memory Triggers（Keyword 模式）

| # | 正则 | 匹配内容 |
|---|------|---------|
| 1 | `remember\|preference\|prefer\|important\|decision\|decided\|always\|never` | 英文记忆关键词 |
| 2 | `记住\|偏好\|喜欢\|...\|不喜欢` | 中文记忆关键词 |
| 3 | `[\w.-]+@[\w.-]+\.\w+` | 邮箱地址 |
| 4 | `\+\d{10,}` | 电话号码 |
| 5 | `(我\|my)\s*(是\|叫\|名字\|...\|邮箱\|email)` | 自我介绍模式 |
| 6 | `(我\|i)\s*(喜欢\|崇拜\|讨厌\|...\|相信)` | 偏好/情感表达 |
| 7 | `favorite\|favourite\|love\|hate\|...\|fan of` | 英文偏好关键词 |

### 5.4 Query 预处理 `prepare_recall_query()`

1. `sanitize_user_text_for_capture()` 清理
2. 截断到 500 字符
3. 尽量在单词边界截断（寻找最后一个空格，若位置 > 80% 长度则从此截断）

---

## 6. 记忆排序与预算：`memory_ranking.py`

### 6.1 `pick_memories_for_injection`

参考代码对齐实现，单函数完成排序、去重、leaf 优先、limit 截断：

```
输入: raw results, limit, query_text, score_threshold
  ↓
Query-aware 排序（rankForInjection）
  ↓
去重（getMemoryDedupeKey）
  ↓
分离 leaf（level == 2）与非 leaf
  ↓
leaf ≥ limit ? 返回 leaf[:limit]
  : 补充非 leaf（检查 score ≥ threshold，检查 URI 未使用）
```

**Query-aware 排序因子**：

| 因子 | 触发条件 | 值 |
|------|---------|-----|
| Leaf Boost | `level == 2` | +0.12 |
| Event Temporal Boost | query 含时间词 **且** `category == "events"` | +0.1 |
| Preference Boost | query 含偏好词 **且** `category == "preferences"` | +0.08 |
| Lexical Overlap | query 词在 `uri + abstract` 中出现 | +0.05/词，上限 0.2 |

Temporal 关键词：`when, time, date, yesterday, today, tomorrow, last week, before, after, 之前, 之后, 昨天, 今天, 明天, 上周`

Preference 关键词：`prefer, like, want, 喜欢, 偏好, 习惯, preference`

### 6.2 Token Budget 注入

`build_memory_lines_with_budget(results, token_budget)`：

- 第 1 条记忆**强制包含**（即使超出预算）
- 第 2 条起：使用 `_count_tokens()`（tiktoken `cl100k_base` 精确编码，不可用时字符回退）检查累计 token 数
- 若累计 token ≤ `token_budget` 则追加，否则停止

行格式：`- [{category}] {abstract/overview} ({score:.0%})`

---

## 7. Session 管理

### 7.1 Session ID 映射 `session_utils.py`

```python
def session_to_ov_id(session_id: str) -> str:
    # UUID → 直接使用（小写）
    # 非 UUID → sha256(session_id).hexdigest
```

目的：
- 将任意 Higo sessionId 映射为稳定的 OV 存储 ID
- 非 UUID sessionId（如包含特殊字符）通过 sha256 哈希确保文件系统安全

### 7.2 Bypass 模式 `bypass.py`

Glob-like 语法编译为 regex：

| Glob | Regex | 说明 |
|------|-------|------|
| `*` | `[^:]*` | 匹配单段（不含 `:`） |
| `**` | `.*` | 匹配任意字符（含 `:`） |

配置方式：`OPENVIKING_BYPASS_SESSION_PATTERNS=agent:*:cron:**,test:**`

匹配对象：原始 `sessionId`（非映射后的 ov_session_id）。

### 7.3 Agent 解析 `agent_resolver.py`

从 `sessionId` 提取 `agent:xxx:` 前缀中的 agentId，支持配置前缀：

| config.agent_id | raw_agent_id | 结果 |
|-----------------|-------------|------|
| `default` | `myagent` | `myagent` |
| `prefix` | `myagent` | `prefix_myagent` |
| `default` | 无 | `default` |

解析结果缓存于 `dict[str, str]` 中。

---

## 8. 诊断日志 `diagnostics.py`

统一格式：

```
openviking: diag {"ts": 1716633600000, "stage": "...", "sessionId": "...", "data": {...}}
```

通过 `emit_diag(stage, session_id, data, enabled)` 输出，受 `emit_diagnostics` 配置开关控制。

当前使用的 stage：

| Stage | 位置 | 数据内容 |
|-------|------|---------|
| `generate_memory_entry` | `generate_memory` 开头 | sessionId, ovSessionId, msg_count, auto_capture, auto_recall |
| `generate_memory_skip` | bypass 时 | reason: session_bypassed |
| `generate_memory_complete` | `generate_memory` 结尾 | total_time, memory_text_length |
| `capture_classified` | `_capture_messages` 分类后 | system, user_total, current_user, context_env |
| `capture_result` | `_capture_messages` 结尾 | total_stored, agent_id |
| `capture_round_result_entry` | `capture_round_result` 开头 | sessionId, roundId, section_count |
| `capture_round_result_complete` | `capture_round_result` 结尾 | captured, agent_id, roundId |
| `commit_triggered` | `_maybe_commit` 触发 commit 时 | pending_tokens, threshold, status, archived, task_id |
| `commit_skipped` | `_maybe_commit` 跳过时 | pending_tokens, threshold, reason |
| `commit_error` | `_maybe_commit` 异常时 | error |
| `compact_entry` | `compact` 开头 | sessionId, ovSessionId |
| `compact_result` | `compact` 各种结果 | ok, compacted, reason, memories, tokensBefore/After |
| `compact_error` | `compact` 异常时 | error |

---

## 9. OpenViking HTTP 客户端

`engine/openviking_client.py`

基于 `httpx.AsyncClient` 的异步 HTTP 客户端，封装了 OpenViking 的所有 API。

### 9.1 认证与路由头

```
X-API-Key          ← config.api_key
X-OpenViking-Account ← config.account_id
X-OpenViking-User  ← config.user_id
X-OpenViking-Agent ← config.agent_id (或被调用方覆盖)
```

### 9.2 核心 API 方法

| 方法 | HTTP | 路径 | 说明 |
|------|------|------|------|
| `health_check()` | GET | `/health` | 健康检查 |
| `find()` | POST | `/api/v1/search/find` | 语义搜索 |
| `add_session_message()` | POST | `/api/v1/sessions/{id}/messages` | 追加消息到 session |
| `get_session()` | GET | `/api/v1/sessions/{id}` | 获取 session 元数据（含 pending_tokens） |
| `get_session_context()` | GET | `/api/v1/sessions/{id}/context?token_budget=` | 获取组装后的 session 上下文 |
| `commit_session()` | POST | `/api/v1/sessions/{id}/commit` | 触发归档 + 记忆提取 |
| `get_task()` | GET | `/api/v1/tasks/{task_id}` | 查询后台任务状态 |
| `get_session_archive()` | GET | `/api/v1/sessions/{id}/archives/{archiveId}` | 获取归档详情 |
| `delete_session()` | DELETE | `/api/v1/sessions/{id}` | 删除 session |
| `delete_uri()` | DELETE | `/api/v1/fs?uri=` | 删除 URI |

### 9.3 Commit 的 Phase 2 轮询

`commit_session(wait=True)` 时：

1. 先发起 `POST /api/v1/sessions/{id}/commit` 获取 `task_id`
2. 轮询 `GET /api/v1/tasks/{task_id}` 直到：
   - `status == "completed"` → 返回合并结果（含 `memories_extracted`）
   - `status == "failed"` → 返回失败结果
   - 超时（默认 300s）→ 返回 `status="timeout"`
3. 轮询间隔 0.5s

---

## 10. 配置项完整说明

| 环境变量 | 默认值 | 类型 | 说明 |
|---------|--------|------|------|
| `OPENVIKING_BASE_URL` | `http://127.0.0.1:1933` | str | OpenViking 服务地址 |
| `OPENVIKING_API_KEY` | `""` | str | API Key |
| `OPENVIKING_AGENT_ID` | `default` | str | 默认 Agent ID |
| `OPENVIKING_ACCOUNT_ID` | `""` | str | 账户 ID |
| `OPENVIKING_USER_ID` | `""` | str | 用户 ID |
| `OPENVIKING_TIMEOUT_MS` | `30000` | int | HTTP 请求超时（毫秒） |
| `OPENVIKING_COMMIT_TOKEN_THRESHOLD` | `8000` | int | 自动提交 token 阈值 |
| `OPENVIKING_RECALL_LIMIT` | `10` | int | 单次搜索返回数量上限（搜索层） |
| `OPENVIKING_RECALL_SCORE_THRESHOLD` | `0.1` | float | 召回最低相似度阈值（fallback 过滤） |
| `OPENVIKING_RECALL_INJECT_LIMIT` | `6` | int | 最终注入记忆数量上限（默认 6） |
| `OPENVIKING_ISOLATE_USER_SCOPE_BY_AGENT` | `false` | bool | 按 Agent 隔离用户作用域 |
| `OPENVIKING_ISOLATE_AGENT_SCOPE_BY_USER` | `true` | bool | 按用户隔离 Agent 作用域 |
| `OPENVIKING_AUTO_CAPTURE` | `true` | bool | 是否自动捕获消息 |
| `OPENVIKING_AUTO_RECALL` | `true` | bool | 是否自动召回记忆 |
| `OPENVIKING_CAPTURE_MODE` | `semantic` | str | 捕获模式：`semantic` 或 `keyword` |
| `OPENVIKING_CAPTURE_MAX_LENGTH` | `8192` | int | 单条消息最大捕获长度 |
| `OPENVIKING_BYPASS_SESSION_PATTERNS` | `""` | str | Bypass 模式列表，逗号分隔 |
| `OPENVIKING_RECALL_TOKEN_BUDGET` | `2000` | int | 注入记忆的 token 预算 |
| `OPENVIKING_RECALL_RESOURCES` | `false` | bool | 是否同时搜索 resources |
| `OPENVIKING_EMIT_DIAGNOSTICS` | `true` | bool | 是否输出结构化诊断日志 |

---

## 11. 独立 API：`POST /compact`

非 Higo 协议端点，用于外部触发强制归档。

**请求**：
```json
{ "sessionId": "abc-123" }
```

**响应**：
```json
{
  "ok": true,
  "compacted": true,
  "reason": "commit_completed",
  "result": {
    "summary": "用户讨论了...",
    "firstKeptEntryId": "archive_003",
    "tokensBefore": 15000,
    "tokensAfter": 3200
  }
}
```

状态说明：

| `compacted` | `reason` | 含义 |
|------------|----------|------|
| true | `commit_completed` | 归档成功 |
| false | `session_bypassed` | Session 被 bypass 跳过 |
| false | `commit_no_archive` | 提交成功但无内容可归档 |
| false | `commit_failed` | Phase 2 执行失败 |
| false | `commit_timeout` | Phase 2 轮询超时 |
| false | `commit_error` | 提交过程异常 |

---

## 12. 关键边界与注意事项

### 12.1 Transform/Result 捕获边界

Higo V2 的 `transform` 请求只包含当前 round 构建 LLM 请求所需的消息，因此当前实现：
- 在 `transform` 中只捕获最后一条 user 作为真实用户输入
- 在 `result` 中捕获 assistant 文本和 tool 结果
- 用 `roundId` 对 result 回调做幂等去重

### 12.2 Session ID 映射变更影响

启用 `session_to_ov_id()` 后，非 UUID 的 sessionId 会映射为 sha256 哈希值。已有 OV session 数据（使用原始 sessionId 作为 key）将**不可见**。如需迁移，需手动将旧 session 数据关联到新 ID。

### 12.3 结构化 Tool 消息来源

Transform 阶段的 `Message` 模型只有 `{role, content}` 字符串字段，不承载 tool 元数据。Tool 信息来自 V2 `result.message.sections`，当前实现会读取 `toolCallId`、`toolname`、`toolargs`、`toolrsp` 并写入 OpenViking tool part。

### 12.4 Capture Decision 尚未接入

`get_capture_decision()` 和 `capture_mode` 已存在，但当前捕获链路尚未调用。生产行为以 `sanitize_user_text_for_capture()` 后是否仍有可写入 parts 为准。

### 12.5 Token 预算估算

`build_memory_lines_with_budget()` 使用 `~4 chars/token` 的粗略估算。实际 token 数取决于模型 tokenizer，估算值仅供参考。

---

## 13. 扩展接口

### 13.1 替换 Memory Engine

如需替换 OpenViking 实现：

1. 在 `engine/memory.py` 中已有抽象基类 `MemoryEngine`：
   ```python
   class MemoryEngine(ABC):
       @abstractmethod
       async def generate_memory(self, session_id: str, messages: list[dict]) -> str:
           ...
   ```
2. 新建子类实现 `generate_memory()`
3. 在 `engine/__init__.py` 中导出
4. 在 `main.py` 中替换 `OpenVikingMemoryEngine()` 为新实例

`PlaceholderMemoryEngine` 是示例占位实现，返回固定格式字符串。
