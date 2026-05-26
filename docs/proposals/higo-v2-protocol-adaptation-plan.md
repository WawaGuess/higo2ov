# Higo V2 协议适配方案：实现 OpenClaw-Plugin 同等功能

## 1. 背景与问题

### 1.1 Higo 协议 V1 → V2 的变更

Higo 今天在 `db9b12d` 提交了记忆引擎协议的重大更新（`protocolVersion: '2026-05-25'`），核心变化：

| 变更项 | V1（旧） | V2（新） |
|--------|---------|---------|
| **transform 请求结构** | `sessionId/contextPath/anchor` 在顶层 | 包装为 `session` / `round` 对象 |
| **transform messages** | 自动附加 `previousAssistantMessage` | **不再附加**，只含 `system + context_env + current_user` |
| **transform 粒度** | 每次模型调用（turn）都可触发 | 每个用户消息（round）只触发一次 |
| **新增 result 模式** | 无 | round 结束时回调，含完整的 `message.sections` |
| **transform 响应** | `result: { request, debug }` | 新增 `pluginContext`、`summary` |

### 1.2 核心问题：Capture 来源变了

在 V1 中，higo2ov 可以在 `transform` 时同时看到上一轮 assistant 回复和本轮 user 输入，因此可以在一次请求中完成 **recall + capture(user + assistant)**。

在 V2 中：
- `transform` 请求只包含本轮 user 输入 → 只能 capture user
- `result` 回调才包含本轮 assistant 完整回复（含 tool 调用）→ 需要在此处 capture assistant

这意味着 **原先"一次 transform 搞定 capture"的模式不再成立**，必须拆分为：
- `transform`：recall + capture user
- `result`：capture assistant（从 sections 中提取）

## 2. 目标

在 higo V2 协议下，让 higo2ov 实现与 `/Users/xueyandong/Desktop/0-XYD-Mac/5-Code/0-github/OpenViking/examples/openclaw-plugin` 同等的核心功能：

1. **自动召回（Auto Recall）**：round 开始时基于用户输入搜索记忆并注入
2. **自动捕获（Auto Capture）**：round 结束时将完整的对话内容（user + assistant + tool）写入 OpenViking
3. **自动归档（Auto Commit）**：pending tokens 超过阈值时异步触发归档
4. **强制归档（Compact）**：同步归档并返回摘要（已有 `/compact` 端点）

## 3. 方案设计

### 3.1 整体架构

```
Round 开始
    │
    ▼
higo ──POST / (mode=transform)──► higo2ov
    │   request.messages = [system, context_env, current_user]
    │   ├─► recall: 基于 current_user 搜索 OV 记忆 → 注入 memory
    │   ├─► capture: 只将 current_user（含 system 前缀）存入 OV
    │   └─► 返回修改后的 messages + 可选 pluginContext
    │
    ▼
模型调用 → 生成回复（可能含多轮 tool 调用）
    │
    ▼
Round 结束
    │
    ▼
higo ──POST / (mode=result)──► higo2ov
    │   message.sections = [content, tool, content, ...]
    │   ├─► capture: 从 sections 提取 assistant 文本回复 → 存 OV
    │   ├─► capture: 从 sections 提取 tool 调用结果 → 存 OV（tool part）
    │   ├─► 检查 pending_tokens，必要时触发异步 commit
    │   └─► 返回 ack
```

### 3.2 与 OpenClaw-Plugin 的功能对照

| OpenClaw-Plugin | Higo2OV V2 对应实现 | 状态 |
|-----------------|---------------------|------|
| `before_prompt_build` 自动召回 | `transform` 中 recall + 注入 memory | 已部分实现，需调整模型字段 |
| `afterTurn` 捕获 user + assistant | `transform` 捕获 user + `result` 捕获 assistant | **需新增 result 处理** |
| `compact()` 强制归档 | 已有 `/compact` 端点 | 已实现 |
| `getSessionContext` 获取归档摘要 | `transform` 中调用 `get_session_context` | 已实现 |
| `commitSession` 自动归档 | `_maybe_commit` 中异步触发 | 已实现 |
| `bypassSessionPatterns` | 已有 `bypass.py` | 已实现 |
| `agentResolver` | 已有 `agent_resolver.py` | 已实现 |
| Session-to-OV ID 映射 | 已有 `session_utils.py` | 已实现 |

### 3.3 文件级改动清单

#### A. models.py — 新增 V2 协议模型

**变更内容：**

1. **ProbeRequest**：新增可选的 `protocolVersion`、`session` 字段，保持对旧字段的兼容
2. **TransformRequest**：
   - 新增 `protocolVersion: str`
   - 新增 `session: dict`（含 `sessionId`, `contextPath`, `userId`）
   - 新增 `round: dict`（含 `roundId`, `seq`, `startedAt`）
   - `anchor` 标记为 `Optional`（向后兼容）
   - `meta` 中的 `historyDefaultCount/historySource/requestKind/source` 标记为 `Optional`
3. **TransformResult**：新增可选的 `pluginContext: dict`
4. **新增 ResultRequest / ResultResponse 模型**

```python
class Round(BaseModel):
    roundId: str
    seq: int
    startedAt: str

class Session(BaseModel):
    sessionId: str
    contextPath: str
    userId: str | None = None

class Section(BaseModel):
    subSeq: int
    type: str  # "content" | "tool"
    content: str | None = None
    reasoning: str | None = None
    toolname: str | None = None
    toolCallId: str | None = None
    toolargs: str | None = None
    toolrsp: str | None = None
    argsReady: bool | None = None
    toolExecutionState: str | None = None
    error: str | None = None

class ResultMessage(BaseModel):
    seq: int
    usermsg: str
    systemPrompt: str
    contextEnvironment: str
    sections: list[Section]

class ResultTransform(BaseModel):
    applied: bool
    summary: str | None = None
    pluginContext: dict | None = None
    systemPrompt: str | None = None
    contextEnvironment: str | None = None

class ResultRequest(BaseModel):
    mode: Literal["result"] = "result"
    protocolVersion: str
    source: str
    session: Session
    round: dict
    transform: ResultTransform
    message: ResultMessage
    errors: list[dict]

class ResultAck(BaseModel):
    roundId: str
    stored: bool
    memoryRevision: str | None = None

class ResultResponse(BaseModel):
    ok: bool = True
    summary: str
    ack: ResultAck
```

#### B. main.py — 新增 result 路由处理

**变更内容：**

1. 在 `handle()` 中增加 `elif mode == "result"` 分支
2. 新增 `_handle_result()` 函数
3. `transform` 响应中返回 `pluginContext`

```python
async def _handle_result(request: ResultRequest) -> ResultResponse:
    session_id = request.session.sessionId
    ov_session_id = session_to_ov_id(session_id)
    round_id = request.round.get("roundId", "unknown")

    logger.info(
        "[result] sessionId=%s ovSessionId=%s roundId=%s status=%s sections=%s",
        session_id, ov_session_id, round_id,
        request.round.get("status"), len(request.message.sections)
    )

    # 1. Capture assistant reply and tool results from sections
    captured = await memory_engine.capture_round_result(
        session_id=session_id,
        sections=[s.model_dump() for s in request.message.sections],
    )

    # 2. Async commit if threshold exceeded
    asyncio.create_task(memory_engine._maybe_commit(ov_session_id))

    logger.info("[result] complete sessionId=%s roundId=%s captured=%s", session_id, round_id, captured)

    return ResultResponse(
        ok=True,
        summary="result accepted",
        ack=ResultAck(
            roundId=round_id,
            stored=True,
            memoryRevision="higo-ov-r1",
        ),
    )
```

`transform` 响应调整：

```python
return TransformResponse(
    ok=True,
    result=TransformResult(
        request=ResultRequest(messages=new_messages),
        debug=DebugInfo(source=ENGINE_NAME),
        pluginContext={"memoryRevision": "higo-ov-r1"},  # 新增
    ),
    summary="transform ok",
)
```

#### C. engine/openviking_engine.py — 新增 capture_round_result

**变更内容：**

1. 修改 `_capture_messages`：移除 assistant capture 逻辑，只 capture user
2. 新增 `capture_round_result()` 方法：从 sections 提取 assistant 和 tool 内容

```python
async def capture_round_result(
    self, session_id: str, sections: list[dict]
) -> int:
    """Capture assistant reply and tool results from round sections.

    Called by the result callback at the end of a round.
    Returns the number of messages captured.
    """
    ov_session_id = session_to_ov_id(session_id)
    agent_id = self._resolve_agent_id(session_id)
    captured = 0

    self._diag("capture_round_result_entry", ov_session_id, {
        "sessionId": session_id,
        "section_count": len(sections),
    })

    for section in sections:
        sec_type = section.get("type", "")

        if sec_type == "content" and section.get("content"):
            text = sanitize_user_text_for_capture(section["content"])
            if not text:
                continue
            decision = get_capture_decision(
                text, mode=self.config.capture_mode,
                capture_max_length=self.config.capture_max_length,
            )
            if not decision["should_capture"]:
                logger.info("[capture_result] assistant rejected: reason=%s", decision["reason"])
                continue
            try:
                await self.client.add_session_message(
                    ov_session_id,
                    role="assistant",
                    role_id="assistant",
                    parts=[{"type": "text", "text": text}],
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                captured += 1
                logger.info("[capture_result] stored assistant ovSessionId=%s", ov_session_id)
            except Exception as e:
                logger.warning("[capture_result] failed to store assistant: %s", e)

        elif sec_type == "tool":
            parts = [{
                "type": "tool",
                "tool_id": section.get("toolCallId"),
                "tool_name": section.get("toolname"),
                "tool_input": section.get("toolargs"),
                "tool_output": section.get("toolrsp"),
            }]
            try:
                await self.client.add_session_message(
                    ov_session_id,
                    role="user",  # OV 中 tool 结果作为 user 角色的 tool part
                    role_id="user",
                    parts=parts,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                captured += 1
                logger.info("[capture_result] stored tool result ovSessionId=%s", ov_session_id)
            except Exception as e:
                logger.warning("[capture_result] failed to store tool: %s", e)

    self._diag("capture_round_result_complete", ov_session_id, {
        "captured": captured,
        "agent_id": agent_id,
    })
    return captured
```

同时修改 `_capture_messages`，**删除 assistant capture 部分**，只保留 user capture：

```python
# 删除以下逻辑：
# current_assistant_msg = assistant_msgs[-1] if assistant_msgs else None
# ... assistant capture block ...

# 只保留：
# 1. system_msg 收集（用于 merge 到 user）
# 2. current_user_msg 的 capture
```

#### D. engine/openviking_client.py — 确认 add_session_message 签名

当前 `add_session_message` 方法签名需要支持：

```python
async def add_session_message(
    self, session_id: str, role: str, role_id: str,
    parts: list[dict], created_at: str, agent_id: str | None = None,
) -> None:
```

需确保支持 `agent_id` 参数（用于 tenant 路由），与 `agent_resolver` 集成。

#### E. engine/__init__.py — 无需改动

`capture_round_result` 是 `OpenVikingMemoryEngine` 的实例方法，已在类定义中。

### 3.4 数据流对比

#### V1 时的数据流（当前实现）

```
transform 请求
  messages = [system, assistant(上一轮), context_env, user(当前)]
  │
  ├─► generate_memory()
  │     ├─► _capture_messages() → 存 assistant + user 到 OV
  │     ├─► get_session_context() → 获取归档摘要
  │     ├─► _recall_memories() → 搜索记忆
  │     └─► _assemble_memory_text() → 组装注入文本
  │
  └─► 返回注入 memory 后的 messages
```

#### V2 时的数据流（目标实现）

```
transform 请求
  messages = [system, context_env, user(当前)]  ← 无 assistant
  │
  ├─► generate_memory()
  │     ├─► _capture_messages() → 只存 user 到 OV
  │     ├─► get_session_context() → 获取归档摘要
  │     ├─► _recall_memories() → 搜索记忆
  │     └─► _assemble_memory_text() → 组装注入文本
  │
  └─► 返回注入 memory 后的 messages + pluginContext

[模型调用 → 生成回复]

result 请求
  message.sections = [
    {type:"content", content:"我先查一下日志"},
    {type:"tool", toolname:"ssh_execute", toolrsp:"..."},
    {type:"content", content:"根因是..."}
  ]
  │
  ├─► capture_round_result()
  │     ├─► content → 存为 assistant text part
  │     ├─► tool → 存为 user tool part
  │     └─► _maybe_commit() → 异步归档检查
  │
  └─► 返回 ack
```

## 4. 边界情况与风险

### 4.1 向后兼容

Higo V2 的 transform 请求仍然携带旧字段（`sessionId` 在顶层、`anchor` 等），因此：
- **短期**：当前代码无需修改也能运行（旧字段仍在）
- **中期**：建议更新模型以同时接受新旧字段，防止 higo 未来清理旧字段
- **长期**：完全迁移到新字段，移除对旧字段的依赖

### 4.2 result 回调失败的影响

从 higo 代码看，result 回调失败仅记录 warn 日志，**不影响用户已完成的响应**：

```typescript
if (!resultResponse.response.ok || resultResponse.payload?.ok !== true) {
    this.logger.warn(`[memory-engine-result] callback failed: ${summary}`);
}
```

但如果 result 频繁失败，会导致：
- assistant 回复丢失，无法被 OpenViking capture
- 记忆库中只剩 user 输入，没有模型回复
- 长期影响记忆质量

**缓解措施**：higo 会对 result 进行异步重试（`delivery: { attempt, maxAttempts }`），higo2ov 应保证 result 处理是幂等的（以 `roundId` 为键）。

### 4.3 幂等性

`roundId` 是每个 round 的唯一标识，higo 的重试会携带相同的 `roundId`。higo2ov 应：
- 在 `capture_round_result` 中记录已处理的 `roundId`
- 重复收到相同 `roundId` 时跳过 capture，直接返回 ack

**建议实现**：在 `OpenVikingMemoryEngine` 中增加 `_processed_round_ids: set[str]`，最多保留最近 1000 个 roundId。

### 4.4 tool 调用的 capture

从 `result` 的 sections 中提取 tool 结果时：
- `type: "tool"` 的 section 包含 `toolname`, `toolCallId`, `toolargs`, `toolrsp`
- 这些需要转换为 OV 的 `type: "tool"` part（含 `tool_id`, `tool_name`, `tool_input`, `tool_output`）
- 注意：OV 中 tool 结果作为 **user 角色**的消息发送（与 openclaw-plugin 一致）

### 4.5 消息中的 reasoning 字段

sections 中的 `reasoning` 字段（模型的推理过程）是否 capture？

**建议**：不 capture reasoning，只 capture `content`。因为：
- reasoning 是模型内部思考过程，通常不适合作为长期记忆
- openclaw-plugin 的 `afterTurn` 中也没有专门处理 reasoning
- 如果用户需要保留 reasoning，可以在未来版本中增加配置项

## 5. 验收标准

1. **transform 正常**：higo 发送 V2 transform，higo2ov 正确解析 `session`/`round`，返回注入 memory 的 messages
2. **result 正常**：higo 发送 V2 result，higo2ov 正确解析 sections，将 assistant 和 tool 内容存入 OpenViking
3. **capture 完整**：一个 round 结束后，OpenViking 中同时存在 user 输入和 assistant 回复
4. **commit 正常**：pending_tokens 超过阈值时自动触发异步归档
5. **幂等正确**：相同 roundId 重复调用 result 不会重复 capture
6. **向后兼容**：旧字段（`sessionId` 顶层、`anchor`）仍然存在时，代码正常工作
7. **pluginContext 返回**：transform 响应包含 `pluginContext`，可被 higo 在 result 中回传

## 6. 实施顺序建议

```
Phase 1: 模型更新
  └─► 更新 models.py（新增 ResultRequest/ResultResponse，调整 TransformRequest）

Phase 2: result 处理
  └─► main.py 新增 result 分支
  └─► engine/openviking_engine.py 新增 capture_round_result()

Phase 3: transform 调整
  └─► 修改 _capture_messages：删除 assistant capture，只保留 user
  └─► transform 响应返回 pluginContext

Phase 4: 幂等性
  └─► OpenVikingMemoryEngine 增加 roundId 去重缓存

Phase 5: 验证
  └─► 本地启动 higo2ov + memory-plugin-stub 联调
  └─► 与 higo 真实环境联调
```
