# 会话历史补全与上下文压缩设计方案（已实施）

## 1. 背景

### 1.1 问题描述

当 Higo 配置记忆引擎（higo2ov）后，Higo 的 `buildModelMessagesForTurn()` 方法会从 transform 请求中丢弃历史会话消息。引擎只收到 `[system, contextEnv, currentUser]`，导致 LLM 在同一会话中遗忘之前的对话内容。

### 1.2 为什么 Higo 的压缩机制失效了

Higo 有两种上下文压缩机制：

1. **被动截断** — `buildMessages()` 按 token 预算选取历史消息。配置记忆引擎后，这条分支被跳过。
2. **主动摘要** — `memory_summarize_to_section` 工具让 LLM 自己触发摘要生成。这也失效了，因为历史消息不再被传递。

由于 Higo 代码不能被修改，higo2ov 必须自行实现历史补全和上下文压缩。

---

## 2. Higo 有无记忆引擎的处理逻辑对比

### 2.1 无记忆引擎时的完整流程（`buildMessages`）

```
┌─────────────────────────────────────────────────────────────┐
│  1. 拉取历史消息                                              │
│     getMessagesForContextAssembly(sessionId, 100条)           │
│                                                               │
│  2. 构建固定消息                                              │
│     - systemMessage                                           │
│     - contextEnvironmentMessage（客户端上下文信息）            │
│     - currentUserMessage（当前用户输入）                       │
│     - requiredToolCompensation（工具调用补偿）                 │
│                                                               │
│  3. Token 预算分配                                            │
│     targetBudget = modelWindow * 0.85                         │
│     requiredCost = system + contextEnv + toolComp + currentUser│
│     remainingBudget = targetBudget - requiredCost              │
│                                                               │
│  4. 选取历史消息（从新到旧遍历 rounds）                        │
│     for round in rounds.reverse():                            │
│         if round.tokenCost > remainingBudget: break           │
│         selectedHistory.unshift(round.messages)               │
│         remainingBudget -= round.tokenCost                    │
│         if round.hitMemorySummary: break  ← 遇到摘要就停      │
│                                                               │
│  5. 组装最终消息列表                                          │
│     [system, selectedHistory, contextEnv, toolComp, currentUser]│
│                                                               │
│  6. 返回给 LLM                                                │
└─────────────────────────────────────────────────────────────┘
```

**关键点：**
- 历史消息完整参与 token 预算计算
- `buildHistoricalRound()` 处理每轮的历史：如果某 section 有 `memorySummaryText`（通过 `memory_summarize_to_section` 工具生成），用摘要替换原始 assistant 消息
- 遍历遇到第一个带摘要的 round 就停止（`hitMemorySummary` 为 true 时 break），因为摘要已经压缩了更早的历史

### 2.2 有记忆引擎时的流程（`buildModelMessagesForTurn`）

```
┌─────────────────────────────────────────────────────────────┐
│  1. 只构建固定消息（不拉取历史）                               │
│     buildMemoryEngineTransformInput()                         │
│     → systemMessage                                           │
│     → contextEnvironmentMessage                               │
│     → currentUserMessage                                      │
│     【注意：没有历史消息，没有 token 预算分配】                 │
│                                                               │
│  2. 发送给 higo2ov                                            │
│     transformRequest({                                        │
│       requestMessages: [system, contextEnv, currentUser],     │
│       modelContextWindowTokens,                               │
│       ...                                                     │
│     })                                                        │
│                                                               │
│  3. higo2ov 返回 transformed.messages                         │
│                                                               │
│  4. Higo 组装最终消息                                         │
│     [transformed.messages, toolResponseMessages]              │
│     【注意：没有拼接 selectedHistory】                        │
│                                                               │
│  5. 返回给 LLM                                                │
└─────────────────────────────────────────────────────────────┘
```

**关键点：**
- `buildMemoryEngineTransformInput()` 直接调用 `buildTransformFixedMessages()`，跳过了整个 `buildMessages()` 的历史选取逻辑
- 传给 higo2ov 的 `requestMessages` 只有 `[system, contextEnv, currentUser]`，不含任何历史
- `buildModelMessagesForTurn()` 返回时只拼接了 `transformed.messages + toolResponseMessages`，**没有加 `currentMessages.slice(0, currentUserIndex)`（即历史消息）**
- `memory_summarize_to_section` 工具虽然还存在，但由于历史消息不再传入，摘要机制实际上无法触发

### 2.3 核心差异总结

| 维度 | 无记忆引擎 | 有记忆引擎 |
|------|-----------|-----------|
| **历史消息来源** | `getMessagesForContextAssembly()` 从 DB 拉取 | **无** |
| **Token 预算** | `modelWindow * 0.85`，减去固定成本后分配给历史 | 只计算固定消息，**历史预算为 0** |
| **被动截断** | 从新到旧遍历 rounds，超预算停止 | **不做** |
| **主动摘要** | `memorySummaryText` 替换原始消息，遇到摘要停止 | **不触发**（无历史可摘要） |
| **传给 LLM 的消息** | `[system, history, contextEnv, currentUser]` | `[transformed.messages, toolResponses]` |
| **Context 组装** | `buildMessages()` 完整逻辑 | `buildMemoryEngineTransformInput()` 只组装固定消息 |

---

## 3. 主动摘要机制详解（`memory_summarize_to_section`）

### 3.1 工具定义

Higo 内置了一个名为 `memory_summarize_to_section` 的**函数工具**，注册在 LLM 的可用工具列表中：

```typescript
{
  type: 'function',
  function: {
    name: 'memory_summarize_to_section',
    description: '生成并写入目标 section 的记忆摘要...',
    parameters: {
      purpose: '本次调用目的摘要',
      targetSeq: '压缩终点消息的 seq',
      targetSubSeq: '压缩终点 section 的 subSeq',
      summary: '摘要正文（需包含上一摘要已覆盖的信息）',
      reason: '触发摘要的原因说明',
    },
  },
}
```

### 3.2 触发方式：LLM 自主调用

这不是由后端代码定时触发的，而是**由 LLM 在对话过程中自行判断是否需要调用**：

- LLM 看到当前上下文中有大量历史消息
- LLM 判断"这些历史需要压缩一下"
- LLM 输出 `tool_calls`，调用 `memory_summarize_to_section`
- 调用是**同步阻塞执行**的，Higo 会等待工具执行完成后再继续

### 3.3 工具执行流程

```
LLM 输出 tool_call: memory_summarize_to_section
        │
        ▼
┌─────────────────────────────┐
│ 1. 验证参数                  │
│    targetSeq/targetSubSeq 必须存在 │
│    summary/reason/purpose 非空     │
│                               │
│ 2. 查询目标 section          │
│    从 DB 获取 msg + section   │
│                               │
│ 3. 查找上一摘要锚点          │
│    findNearestMemorySummaryBefore() │
│    → 获取上一个摘要的覆盖范围       │
│                               │
│ 4. 写入摘要到 section        │
│    saveSectionMemorySummary({    │
│      summaryText,               │
│      rangeFromSeq: 上一摘要终点,  │
│      rangeToSeq: targetSeq,     │
│      rangeToSubSeq: targetSubSeq,│
│      prevSummarySeq,            │
│      revision: current + 1,     │
│    })                           │
│                               │
│ 5. 返回 tool_result 给 LLM   │
│    包含 coverage 范围、revision 等 │
└─────────────────────────────┘
        │
        ▼
LLM 继续生成回复
```

### 3.4 摘要的覆盖语义：前缀连续覆盖

每个摘要的覆盖范围是**从上一摘要的终点到当前目标 section（含）**：

```
Round 1-5 的消息 → 摘要 A（覆盖 seq 1-5）
Round 6-10 的消息 → 摘要 B（覆盖 seq 6-10，且语义包含摘要 A 的内容）

最终 DB 中：
- Section 5 有 memorySummaryText = "摘要A"
- Section 10 有 memorySummaryText = "摘要B（包含A的内容）"
```

**关键约束**：新的摘要正文必须语义包含上一记忆锚点已覆盖的信息，以保持"截至当前终点的整体前缀摘要语义"。

### 3.5 在 buildMessages 中如何使用摘要

`buildHistoricalRound()` 处理每轮历史消息时：

```typescript
for (const section of sections) {
  if (section.memorySummaryText) {
    // 用摘要替换原始 assistant 消息
    roundMessages.push({
      role: 'assistant',
      content: section.memorySummaryText,
    });
    hitMemorySummary = true;
    continue;
  }
  // ... 普通 content/reasoning/tool 处理
}
```

然后在 `buildMessages()` 的遍历中：

```typescript
for (let i = rounds.length - 1; i >= 0; i--) {
  // ... 累加 token 预算
  selectedHistory.unshift(...round.messages);
  if (round.hitMemorySummary) {
    reachedMemorySummary = true;
    break;  // ← 遇到摘要就停止，因为摘要已覆盖更早历史
  }
}
```

**为什么遇到摘要就停止？**
因为 `memorySummaryText` 已经语义包含了从会话开头到该 section 的所有历史信息。再往前的 round 不需要再带了。

### 3.6 配置记忆引擎后，主动摘要为什么失效了？

因为 `buildModelMessagesForTurn()` 不走 `buildMessages()` 的历史遍历逻辑：

1. **没有历史消息传入** → LLM 看不到需要压缩的历史上下文
2. **没有 `buildHistoricalRound()`** → 即使 LLM 调用了工具，Higo 也不会在后续请求中读取 `memorySummaryText`
3. **工具本身还在**，但失去了触发条件和生效路径

所以配置记忆引擎后，Higo 的**被动截断**和**主动摘要**两条压缩线都断了。

---

## 4. 目标

1. **补全会话历史**：在 transform 响应中携带之前的对话内容，让 LLM 能看到历史上下文。
2. **实现上下文压缩**：防止超出模型的上下文窗口限制。
3. **尽量复刻 Higo 的 token 预算行为**。
4. **利用 OpenViking 的 commit 摘要** 作为"主动压缩"层。

---

## 5. 总体架构

```
Higo Transform 请求
        │
        ▼
┌─────────────────────┐
│  higo2ov transform  │
│     处理器          │
└─────────────────────┘
        │
        ▼
┌─────────────────────┐
│  get_session_context│  ← OpenViking API
│  (ov_session_id)    │
└─────────────────────┘
        │
        ├──► latest_archive_overview  ──┐
        ├──► pre_archive_abstracts     ─┼──► 格式化 & 截断 ──► memory_text
        └──► messages (活跃消息)        ─┘
        │
        ▼
┌─────────────────────┐
│  _recall_memories() │  ← 全局语义搜索（已有逻辑）
└─────────────────────┘
        │
        ▼
┌─────────────────────┐
│  _assemble_memory_  │
│  text()             │
└─────────────────────┘
        │
        ▼
┌─────────────────────┐
│  返回消息列表:      │
│  [system, memory,   │
│   contextEnv,       │
│   currentUser]      │
└─────────────────────┘
```

---

## 6. 详细设计

### 6.1 OpenViking 数据源

`get_session_context()` 返回：

| 字段 | 类型 | 说明 |
|------|------|------|
| `latest_archive_overview` | string | 最近一次 commit 的摘要（主动压缩结果） |
| `pre_archive_abstracts` | list[string] | 更早的 commit 摘要列表，按 旧→新 排序 |
| `messages` | list[dict] | 会话中尚未 commit 的活跃消息 |
| `estimatedTokens` | int | 活跃消息的估算 token 数 |

### 6.2 memory_text 结构

组装后的 `memory_text` 注入到对话中，包含两个区块：

```
<session-history>
  <!-- 归档摘要（旧 → 新）-->
  [pre_archive_abstracts[0]]
  [pre_archive_abstracts[1]]
  ...
  [latest_archive_overview]

  <!-- 活跃消息（旧 → 新，排除当前轮次）-->
  - user: ...
  - assistant: ...
  - user: ...
</session-history>

<relevant-memories>
  - [profile] ...
  - [event] ...
</relevant-memories>
```

**区块顺序的理由**：`session-history` 放在前面，因为它提供了当前会话的时间连续性。`relevant-memories`（全局语义搜索）放在后面，作为补充上下文。

### 6.3 Token 预算分配

参考 Higo 原有的逻辑：

```
total_budget = modelContextWindowTokens * 0.85

fixed_costs =
    system_message_tokens
  + contextEnv_message_tokens
  + currentUser_message_tokens
  + memory_injection_overhead    <!-- <session-history> 标签、格式化开销 -->

history_budget = total_budget - fixed_costs
```

`history_budget` 全部分配给 `<session-history>` 区块。

`<relevant-memories>` 区块继续使用现有的 `recall_token_budget` 配置（默认 2048），与历史预算解耦。

### 6.4 截断策略

在 `<session-history>` 内部，内容按时间顺序排列（旧 → 新）。截断从旧到新遍历，累加 token 数，当达到 `history_budget` 时停止。

**丢弃优先级（先丢什么）：**

1. 最旧的 `pre_archive_abstracts` 条目
2. 然后是旧的活跃 `messages`
3. `latest_archive_overview` 和最新的消息始终保留

这确保了模型始终能看到最近的上下文，与 Higo 原有 `buildMessages()` 的行为一致。

### 6.5 消息格式化

活跃消息（来自 OV 的 `messages`）格式化为：

```
- {role}: {text}
```

**文本提取规则：**
- 如果存在 `content` 字段（字符串），直接使用
- 如果存在 `parts` 数组，拼接所有 `type == "text"` 的 `text` 字段
- 每条消息截断到最多 500 字符，防止单条消息占满整个预算
- 如果 `messages` 中的最后一条与当前轮次消息重复，则排除它（避免与 Higo 的 `currentUser` 重复）

### 6.6 归档摘要格式化

归档摘要格式化为：

```
[归档摘要]
{overview_text}
```

每个摘要截断到最多 800 字符。

---

## 7. 代码改动点

### 7.1 `engine/openviking_engine.py`

#### 7.1.1 `generate_memory()` 方法

在调用 `get_session_context()` 获取 context 后：

1. 提取 `latest_archive_overview`、`pre_archive_abstracts` 和 `messages`
2. 格式化活跃消息（排除当前轮次）
3. 根据 `model_context_tokens` 计算 `history_budget`
4. 调用 `_assemble_session_history()` 生成历史文本块
5. 将历史块传给 `_assemble_memory_text()`

#### 7.1.2 新增方法：`_assemble_session_history()`

```python
def _assemble_session_history(
    self,
    latest_overview: str,
    pre_archive_abstracts: list[str],
    active_messages: list[dict],
    history_budget: int,
) -> str:
    """组装并截断会话历史文本。"""
```

职责：
- 格式化归档摘要和活跃消息
- 从旧到新遍历，累加 token
- 返回截断后的文本，如果无内容则返回空字符串

#### 7.1.3 `_assemble_memory_text()` 方法（修改签名）

```python
def _assemble_memory_text(
    self,
    memories: list[dict],
    token_budget: int,
    session_history: str = "",
) -> str:
```

职责：
- 如果 `session_history` 非空，前置 `<session-history>` 区块
- 追加 `<relevant-memories>` 区块（已有逻辑）
- 返回合并后的文本，如果两者都为空则返回空字符串

### 7.2 `main.py`（无需改动）

`_handle_transform()` 已经调用 `memory_engine.generate_memory()` 并通过 `_build_messages()` 注入结果。组装后的 memory_text（现在同时包含会话历史和全局记忆）走相同的链路。

---

## 8. 边界情况

| 场景 | 行为 |
|------|------|
| 尚未 commit（`latest_overview` 为空，无摘要） | `<session-history>` 中只包含活跃 `messages` |
| 活跃消息为空（刚 commit 完） | `<session-history>` 中只包含 `latest_archive_overview` + `pre_archive_abstracts` |
| 归档和活跃消息都为空 | 完全不输出 `<session-history>` 区块 |
| `history_budget` 很小（如 < 200 token） | 只保留最近一条消息或最新摘要；如果仍装不下，省略整个区块 |
| 当前轮次消息出现在 OV `messages` 中 | 从活跃消息格式化中排除，避免与 Higo 的 `currentUser` 重复 |
| `pre_archive_abstracts` 很长 | 旧的摘要先被截断；新的摘要 + latest overview 保留 |

---

## 9. 与 OV Commit 机制的协作

本设计有意利用 OV 现有的 commit 机制：

- **OV commit** = 主动压缩（异步、LLM 生成摘要、质量高）
- **higo2ov 截断** = 被动压缩（同步、基于 token 预算、保留近期精确消息）

两者互补：
- commit 前：higo2ov 注入完整活跃消息
- commit 后：OV 的 `latest_archive_overview` 提供摘要，higo2ov 注入摘要 + 新的活跃消息
- 多次 commit 后：`pre_archive_abstracts` 确保历史上下文不会彻底丢失

---

## 10. 验证计划

1. **两轮对话测试**
   - 第一轮："我叫小白" → 模型正确回复
   - 第二轮："我叫什么" → 模型应回答 "你叫小白"
   - 检查日志：`[transform] memory_text generated` 应包含带第一轮内容的 `<session-history>` 区块

2. **多 commit 测试**
   - 进行长对话，触发 OV commit 至少两次
   - 验证 `latest_archive_overview` 和 `pre_archive_abstracts` 出现在 memory_text 中
   - 验证预算紧张时，旧的摘要先于新的被截断

3. **Token 预算测试**
   - 设置较小的模型上下文窗口
   - 验证 `<session-history>` 被截断且不超预算
   - 验证 `<relevant-memories>` 仍独立正常工作

4. **空历史测试**
   - 全新会话的第一轮
   - 验证不注入 `<session-history>` 区块（如有全局记忆则只注入 `<relevant-memories>`）
