# Higo2OV 交互时序图

## 场景：一个完整的 Round（用户发消息 → 模型回复）

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant H as Higo
    participant P as Higo2OV
    participant OV as OpenViking

    %% ========== Round 开始：Transform ==========
    U->>H: 发送消息 "你好"

    H->>P: POST /
    Note right of H: mode=transform<br/>protocolVersion: "2026-05-25"<br/>source: "higo"<br/>session: {sessionId, contextPath, userId}<br/>round: {roundId, seq, startedAt}<br/>request.messages: [system, contextEnv, currentUser]<br/>meta: {modelContextWindowTokens}

    P->>OV: POST /api/v1/sessions/{id}/messages
    Note right of P: role="user"<br/>parts: [{type:"text", text:"[system] ...\n\n你好"}]<br/>created_at: ISO8601
    OV-->>P: 200 OK

    P->>OV: GET /api/v1/sessions/{id}/context?token_budget=128000
    Note right of P: 获取归档摘要 + 活跃消息
    OV-->>P: {latest_archive_overview, pre_archive_abstracts, messages, estimatedTokens}

    par 并行搜索记忆
        P->>OV: POST /api/v1/search/find
        Note right of P: query="你好"<br/>targetUri="viking://user/memories"<br/>limit=10<br/>scoreThreshold=0.1<br/>agentId="higo-extension"
        OV-->>P: {memories: [...]}
    and
        P->>OV: POST /api/v1/search/find
        Note right of P: query="你好"<br/>targetUri="viking://agent/memories"
        OV-->>P: {memories: [...]}
    end

    P->>P: 去重 → 过滤叶子 → 阈值过滤 → 重排序 → Token预算截断

    P->>P: create_task(_maybe_commit)
    Note right of P: 异步检查 pending_tokens，不阻塞响应

    P-->>H: 200 OK
    Note left of P: ok: true<br/>summary: "transform ok"<br/>result.request.messages: [system, memory, contextEnv, currentUser]<br/>pluginContext: {memoryRevision: "higo-ov-r1"}

    H->>H: 将 memory 消息注入到 messages 中

    %% ========== 模型调用 ==========
    H->>LLM: 发送修改后的 messages
    LLM-->>H: 返回模型回复（含 <think> 推理过程）

    %% ========== Round 结束：Result ==========
    H->>P: POST /
    Note right of H: mode=result<br/>protocolVersion: "2026-05-25"<br/>source: "higo"<br/>session: {sessionId, contextPath, userId}<br/>round: {roundId, seq, status, finishReason, startedAt, endedAt}<br/>transform: {applied, summary, pluginContext}<br/>message: {seq, usermsg, systemPrompt, contextEnvironment, sections}<br/>errors: []

    Note over P: sections 示例：<br/>[{type:"content", content:"..."},<br/> {type:"tool", toolname:"...", toolrsp:"..."},<br/> {type:"content", content:"..."}]

    loop 遍历 sections
        alt type="content"
            P->>P: sanitize_user_text_for_capture(content)<br/>→ 过滤 <think> / metadata / HEARTBEAT<br/>→ 清理后为空则跳过
            P->>OV: POST /api/v1/sessions/{id}/messages
            Note right of P: role="assistant"<br/>parts: [{type:"text", text:"你好！我是..."}]
            OV-->>P: 200 OK
        else type="tool"
            P->>OV: POST /api/v1/sessions/{id}/messages
            Note right of P: role="user"<br/>parts: [{type:"tool", tool_id, tool_name, tool_input, tool_output}]
            OV-->>P: 200 OK
        end
    end

    P->>P: create_task(_maybe_commit)
    Note right of P: 异步检查 pending_tokens，不阻塞响应

    P-->>H: 200 OK
    Note left of P: ok: true<br/>summary: "result accepted"<br/>ack: {roundId, stored: true}

    H->>U: 返回最终回复给用户
```

---

## 关键说明

### Transform 阶段（Round 开始）

| 步骤 | 动作 | OV API |
|------|------|--------|
| 1 | Capture user 输入 | `POST /api/v1/sessions/{id}/messages` |
| 2 | 获取 session 上下文 | `GET /api/v1/sessions/{id}/context` |
| 3 | 搜索相关记忆（并行） | `POST /api/v1/search/find` × 2 |
| 4 | 异步检查并触发归档 | `GET /api/v1/sessions/{id}` → `POST /api/v1/sessions/{id}/commit` |

**响应关键**：`result.request.messages` 必须保留 system/contextEnv/currentUser 的相对顺序，最后一条 user 必须是 currentUser。

### Result 阶段（Round 结束）

| 步骤 | 动作 | OV API |
|------|------|--------|
| 1 | 解析 sections，提取 assistant 文本 | 本地处理 |
| 2 | 清洗（过滤 think/metadata/HEARTBEAT） | `sanitize_user_text_for_capture()` |
| 3 | 空内容跳过，否则存入 assistant 回复 | `POST /api/v1/sessions/{id}/messages` (role=assistant) |
| 4 | 存入 tool 结果 | `POST /api/v1/sessions/{id}/messages` (role=user, type=tool) |
| 5 | 异步检查并触发归档 | `GET /api/v1/sessions/{id}` → `POST /api/v1/sessions/{id}/commit` |

**幂等性**：`capture_round_result` 以 `roundId` 为键做去重，同一 roundId 重复调用不会重复写入。

### Memory Query 阶段（独立调用）

`mode=memory_query` 提供记忆查询工具能力，当前为**占位实现**。

| action | 说明 | 当前行为 |
|--------|------|---------|
| `help` | 返回查询语法帮助 | 返回 `ok=false, summary="当前功能不可用，暂不支持"` |
| `query` | 执行记忆查询 | 同上 |

### 异步归档（_maybe_commit）

Transform 和 Result 阶段都会异步触发 `_maybe_commit`：
- 获取 session 的 `pending_tokens`
- 如果超过 `commitTokenThreshold`，调用 `commit(wait=false)`
- OV 返回 `task_id`，Phase 2（记忆提取）异步执行
