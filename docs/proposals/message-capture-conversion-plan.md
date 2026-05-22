# Higo → OpenViking 消息捕获转换修改计划

## 问题 1：context environment 被误存为"用户发言"

### 当前行为

`_capture_messages()` 遍历 messages，只要 `role == "user"` 就存入 OpenViking。Higo transform 中通常有两条 `user` 消息：context environment（环境信息）和 current user（真实用户发言），两者都被当作"用户发言"存储，导致会话历史被污染。

### 方案对比

| 方案 | 原理 | 优点 | 缺点 | 决策 |
|------|------|------|------|------|
| **A. 位置识别法** | 按消息在列表中的位置判断语义：最后一条 user = current user，其余 user = context env | 可靠、不依赖内容特征、与 Higo 协议固定顺序绑定 | 若 Higo 协议消息顺序变更则失效 | **采用** |
| **B. 内容启发法** | 通过内容特征（如特定前缀 `[Context]`、JSON 元数据块）识别 context env | 不依赖消息顺序 | 可靠性低，Higo 内容格式可能变化 | 不采用 |

---

## 问题 2：system 消息丢失

### 当前行为

`_capture_messages()` 中 `if role not in ("user", "assistant"): continue`，system 被直接跳过，OpenViking 完全不知道对话的 system instruction。

### 方案对比

| 方案 | 原理 | 优点 | 缺点 | 决策 |
|------|------|------|------|------|
| **A. 合并到 user parts** | 将 system content 作为 `[system] ...` 前缀合并到 current user 的 text part 中 | 不增加独立消息、避免重复存储、实现简单、OpenViking 提取记忆时能看到背景 | system 和 user 内容合并为一条 text，不够结构化 | **采用** |
| **B. 作为 resource 导入** | 使用 `POST /api/v1/resources` 将 system 作为 instruction 文件导入 OV | 结构化存储、不污染 session 历史 | system 可能每轮变化需频繁更新、需额外 API 调用、需处理变更检测 | 不采用 |
| **C. role_id="system" 存入** | 以 `role="user"`, `role_id="system"` 存入 session | OpenViking 能识别语义 | OV 的 role 只接受 user/assistant，语义不准确；每轮重复存储；memory extractor 可能误提取 | 不采用 |

---

## 问题 3：没有区分消息语义（role_id）

### 当前行为

调用 `add_session_message()` 时只传了 `role`，没有传 `role_id`。

### 修改方案

修改 `OpenVikingClient.add_session_message()` 增加 `role_id` 可选参数，并在 `_capture_messages()` 中传入：
- assistant → `role_id="assistant"`
- current user → `role_id="user"`

---

## 决策结论

| 问题 | 采用方案 | 说明 |
|------|---------|------|
| 1. context env 被误存 | **方案 A：位置识别法** | 只存最后一条 user，跳过其余 user |
| 2. system 消息丢失 | **方案 A：合并到 user parts** | system 内容作为前缀合并到 current user |
| 3. 缺少 role_id | **添加 role_id 参数** | assistant / current user 分别标记 |

---

## 修改前 vs 修改后

### 修改前（当前代码）

```
Higo transform 消息（4条）:
  system        → 跳过
  assistant     → 存入 OV（role=assistant, 无 role_id）
  user(context) → 存入 OV（role=user, 无 role_id）⚠️ 污染历史
  user(current) → 存入 OV（role=user, 无 role_id）

结果：OV session 3 条消息，含环境信息污染
```

### 修改后（目标）

```
Higo transform 消息（4条）:
  system        → 合并到 current user 的 parts 中
  assistant     → 存入 OV（role=assistant, role_id="assistant"）
  user(context) → 跳过不存
  user(current) → 存入 OV（role=user, role_id="user", parts 包含 system）

结果：OV session 2 条消息，无环境信息污染，有语义标记
```

---

## 需要修改的文件

| 文件 | 修改内容 |
|------|---------|
| `engine/openviking_client.py` | `add_session_message()` 增加 `role_id` 可选参数 |
| `engine/openviking_engine.py` | 重写 `_capture_messages()`：分类消息、跳过 context env、合并 system、添加 role_id |
| `engine/text_utils.py` | 保持不变 |
