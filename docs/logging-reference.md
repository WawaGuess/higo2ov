# 日志前缀参考手册

本文档说明 Higo2OV 服务中所有日志前缀的含义，便于排查问题。

## 日志层级结构

```
main.py                     # Higo 协议入口层
├── [handle]                # 收到 Higo 请求
├── [higo_request]          # 完整 Higo 请求体
├── [higo_response]         # 完整返回体
├── [probe]                 # probe 模式处理
├── [transform]             # transform 模式处理
└── [result]                # result 回调处理

engine/openviking_engine.py # 业务逻辑层
├── [generate_memory]       # 核心记忆生成流程
├── [capture]               # 消息捕获到 OpenViking
├── [capture_result]        # result.sections 捕获到 OpenViking
├── [recall]                # 记忆搜索与后处理
├── [safe_find]             # 单次搜索请求包装
├── [assemble]              # 记忆文本组装
├── [commit_check]          # commit 阈值检查
├── [commit]                # commit 触发执行
└── [compact]               # /compact 强制归档

engine/openviking_client.py # HTTP 客户端层
├── [ov_request]            # 发出 HTTP 请求
├── [ov_response]           # 收到 HTTP 成功响应
└── [ov_error]              # HTTP 请求异常/失败
```

---

## 详细说明

### `main.py` 层

| 前缀 | 来源函数 | 含义 | 典型输出示例 |
|------|---------|------|-------------|
| `[handle]` | `handle()` | 收到 Higo 的 HTTP 请求，记录请求模式；当前只读取顶层 `sessionId`，V2 请求通常显示 `unknown` | `mode=probe sessionId=unknown` |
| `[higo_request]` | `handle()` | 完整请求 JSON | `{...}` |
| `[higo_response]` | `handle()` | 完整响应 JSON | `{...}` |
| `[probe]` | `_handle_probe()` | probe 模式处理过程 | `sessionId=xxx timestamp=...` / `OpenViking health ok: {...}` |
| `[transform]` | `_handle_transform()` | transform 模式处理过程；日志字段名仍叫 `anchor`，实际值为 `round.seq/0` | `sessionId=xxx anchor=1/0 msg_count=N modelTokens=N` |
| `[result]` | `_handle_result()` | round 结束回调处理过程 | `sessionId=xxx roundId=xxx status=completed sections=N errors=0` / `complete ... captured=N` |

**transform 子日志**：
- `original_msg[N]` — 原始输入消息的角色和内容长度
- `memory_text generated, length=N` — 生成记忆文本的长度
- `memory is empty, skipping injection` — 记忆为空，跳过注入
- `returning msg_count=N (added=N)` — 返回消息总数及新增数量
- `result_msg[N]` — 返回消息列表中每条消息的角色和内容长度

---

### `engine/openviking_engine.py` 层

| 前缀 | 来源函数 | 含义 | 典型输出示例 |
|------|---------|------|-------------|
| `[generate_memory]` | `generate_memory()` | 核心记忆生成入口，记录整体耗时 | `start sessionId=xxx msg_count=N` / `capture done in 0.XXXs` / `complete total_time=0.XXXs` |
| `[capture]` | `_capture_messages()` | transform 阶段将当前 user 消息追加到 OpenViking session | `stored current_user ovSessionId=xxx parts=N system_merged=true` / `total stored=N` |
| `[capture_result]` | `capture_round_result()` | result 阶段将 assistant 文本和 tool 结果追加到 OpenViking session | `stored assistant ovSessionId=xxx` / `stored tool result ovSessionId=xxx tool=...` / `total stored=N roundId=...` |
| `[recall]` | `_recall_memories()` | 并行搜索 user/agent 记忆并后处理 | `query='...' limit=N threshold=X.X` / `raw=N after dedup=N after leaf=N after threshold=N after rerank=N` |
| `[safe_find]` | `_safe_find()` | 单次语义搜索请求（带异常捕获） | `uri=viking://user/memories returned=N` / `error for ...: ...` |
| `[assemble]` | `_assemble_memory_text()` | 将上下文和记忆组装为注入文本 | `overview=True/False abstracts=N memories=N text_len=N` |
| `[commit_check]` | `_maybe_commit()` | 检查 pending_tokens 是否超过阈值 | `ovSessionId=xxx pending_tokens=N threshold=N` |
| `[commit]` | `_maybe_commit()` | 触发或跳过 session commit | `triggering ovSessionId=xxx ...` / `skipped for ovSessionId=xxx` |
| `[compact]` | `compact()` | 强制归档处理过程 | `committing ovSessionId=xxx (wait=true)` / `committed ... archived=true memories=N` |

---

### `engine/openviking_client.py` 层

| 前缀 | 来源函数 | 含义 | 典型输出示例 |
|------|---------|------|-------------|
| `[ov_request]` | `_request()` | 向 OpenViking 发出 HTTP 请求 | `GET/POST path agent=xxx body_len=N` |
| `[ov_response]` | `_request()` | 收到 HTTP 成功响应（2xx） | `GET/POST path status=200 time=0.XXXs` |
| `[ov_error]` | `_request()` | HTTP 请求失败（非 2xx、超时、网络异常） | `GET/POST path error=... time=0.XXXs` |

> **注意**：`[ov_request]` 总是成对出现 `[ov_response]`（成功）或 `[ov_error]`（失败），通过 `path` 和 `method` 可关联。

---

## 问题定位速查

### probe 返回 ok=false

查看 `[probe]` 日志：
- 若看到 `[ov_request] GET /health` + `[ov_error]` → OpenViking 服务不可达或异常
- 若看到 `[ov_response] GET /health status=401` → `.env` 中 API Key 配置错误

### transform 未注入记忆

查看 `[transform]` 日志：
- 若看到 `memory is empty, skipping injection` → 继续向上查看 `[generate_memory]` 和 `[recall]`
- 若 `[recall] after rerank=0` → 搜索未返回结果，检查 `[safe_find]` 是否有 `[ov_error]`
- 若 `[generate_memory] failed to get session context` → OpenViking 连接问题

### OpenViking 返回 401 Unauthorized

查看 `[ov_error]` 日志：
- 确认 `.env` 文件中 `OPENVIKING_API_KEY` 已正确填写
- 确认 OpenViking 服务器的认证模式（`api_key` vs `trusted`）

### 记忆未归档 / 未提取

查看 `[commit_check]` 和 `[commit]` 日志：
- `[commit_check] pending_tokens=N threshold=N` — 检查 pending_tokens 是否超过阈值
- `[commit] triggering ...` — commit 已触发，但可能异步执行中
- `[commit] skipped ...` — pending_tokens 未达阈值，不会触发

### result 回调未写入 assistant/tool

查看 `[result]` 和 `[capture_result]` 日志：
- `[result] ... sections=0` — Higo 没有传入 sections，higo2ov 不会写入内容
- `[capture_result] roundId=... already processed` — 同一个 roundId 已处理过，幂等逻辑跳过
- `[capture_result] total stored=0` — sections 可能为空、content 清理后为空，或 OpenViking 写入失败

### 性能问题

查看 `[generate_memory]` 日志中的分段时间：
- `capture done in X.XXXs` — 消息追加耗时
- `context fetched in X.XXXs` — 获取会话上下文耗时
- `recall done in X.XXXs` — 记忆搜索耗时
- `total_time=X.XXXs` — 整体耗时

查看 `[ov_response]` 中的 `time=X.XXXs` — 单个 HTTP 请求耗时。
