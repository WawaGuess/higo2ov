# Token 统计 API 技术文档

## 1. 需求概述

在 Higo Session Memory Plugin 的运行过程中，每次对话轮次（turn）都会产生大量的 token 消耗。为了让外部系统或管理后台能够监控和查询整体的 token 使用情况，我们开发了独立的 Token 统计 API。

**核心目标：**
- 提供所有 session 的 token 消耗聚合统计
- 支持按时间范围过滤
- 提供完整版（含明细）和轻量版（仅聚合值）两种接口

## 2. 数据收集流程

Token 统计的数据来源于 Higo V2 协议的两个核心回调阶段：

### 2.1 Transform 阶段 — 记录 Input Tokens

当 Higo 平台发送 `mode=transform` 请求时，插件会：
1. 生成记忆文本（memory）
2. 重构消息列表（在 system 后、第一个 user 前注入 memory）
3. 调用 `TurnCollector.start_turn()` 记录本轮的 input tokens

**代码路径：** `main.py:181`

```python
TurnCollector.get_instance().start_turn(
    session_id=sid,
    round_id=request.round.roundId,
    seq=request.round.seq,
    messages=[m.model_dump() for m in new_messages],
    model_tokens=model_tokens,
    memory_text=memory_text,
)
```

### 2.2 Result 阶段 — 记录 Output Tokens

当 Higo 平台发送 `mode=result` 回调时，插件会：
1. 将 assistant 回复和 tool 结果存入 OpenViking 会话
2. 调用 `TurnCollector.end_turn()` 记录本轮的 output tokens

**代码路径：** `main.py:237`

```python
TurnCollector.get_instance().end_turn(
    round_id=round_id,
    sections=[s.model_dump() for s in request.message.sections],
    errors=[e.model_dump() for e in request.errors],
)
```

### 2.3 数据流转图

```
Higo Platform          Plugin Service              TurnCollector
     |                       |                           |
     |--- transform -------->|                           |
     |                       |-- generate_memory() ----->|
     |                       |-- _build_messages()       |
     |                       |-- start_turn() ---------->|- 记录 input tokens
     |                       |                           |
     |<-- modified msgs -----|                           |
     |                       |                           |
     |--- result ----------->|                           |
     |                       |-- capture_round_result()  |
     |                       |-- end_turn() ------------>|- 记录 output tokens
     |                       |                           |
     |<-- ack ---------------|                           |
```

## 3. Token 计算策略

Token 计数采用双策略实现，优先使用 `tiktoken`，失败时回退到字符估算。

**代码路径：** `monitor/collector.py:21-45`

```python
try:
    import tiktoken
    _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _TIKTOKEN_ENC = None

def _count_tokens(text: str) -> int:
    if not text:
        return 0
    if _TIKTOKEN_ENC is not None:
        try:
            return len(_TIKTOKEN_ENC.encode(text))
        except Exception:
            pass
    # Fallback: CJK ≈ 1 token/char, ASCII ≈ 0.25 token/char
    total = 0
    for ch in text:
        total += 1 if ord(ch) > 127 else 0.25
    return max(1, int(total))
```

### Input Token 的构成

Input token 通过 `_chunk_input()` 函数将消息列表拆分为 4 个类别。每条消息的 token 计算方式为：`内容本身的 token 数 + 3`。

其中 `+3` 是**消息格式标记（role label、分隔符、特殊 token）的粗略估算值**，来源于 ChatML / OpenAI 消息格式中每条消息的包装开销（如 `<|im_start|>role\n...<|im_end|>\n`）。这是一个经验值，并非精确计算——不同模型的实际格式开销会有差异。

| 类别 | 来源 | token 计算 |
|---|---|---|
| `system` | system 角色的消息内容 | `_count_tokens(content) + 3` |
| `memory` | 注入的记忆文本 | `_count_tokens(content) + 3` |
| `history` | 历史对话消息（非当前 user） | 每条消息分别 `+ 3` 后求和 |
| `user` | 当前用户输入 | `_count_tokens(content) + 3` |

**代码路径：** `monitor/collector.py:56-146`

### Output Token 的构成

Output token 包含两部分：

| 来源 | 计算方式 |
|---|---|
| `assistant_output` | 对 assistant 回复文本调用 `_count_tokens()` |
| `tool_calls` | 对每个 tool 的 args + rsp 分别调用 `_count_tokens()` 后求和 |

**代码路径：** `monitor/collector.py:351-409`

## 4. 数据模型

### 4.1 TurnRecord

每轮对话的数据存储在 `TurnRecord` 数据类中：

**代码路径：** `monitor/collector.py:153-246`

```python
@dataclass
class TurnRecord:
    turn_id: str          # 唯一标识（uuid 前12位）
    session_id: str       # 所属会话
    round_id: str         # Higo 轮次 ID
    seq: int              # 轮次序号
    created_at: float     # 创建时间（Unix 时间戳）

    # Input 字段
    system_prompt: str = ""
    system_tokens: int = 0
    user_input: str = ""
    user_tokens: int = 0
    history: List[dict] = field(default_factory=list)
    history_tokens: int = 0
    memory_injected: str = ""
    memory_tokens: int = 0

    # Output 字段
    assistant_output: str = ""
    assistant_tokens: int = 0
    reasoning: str = ""
    tool_calls: List[dict] = field(default_factory=list)

    # 错误信息
    errors: List[Any] = field(default_factory=list)

    # 总计
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0

    # 分块详情（用于监控面板可视化）
    chunks: List[dict] = field(default_factory=list)
```

### 4.2 Session 聚合结构

当调用 `list_sessions()` 时（默认 `include_latest_turn=True`），系统按 session_id 聚合所有 turn，生成如下结构：

```json
{
  "session_id": "xxx",
  "turn_count": 5,
  "latest_turn": { /* TurnRecord.to_dict() */ },
  "created_at": 1716883200.0,
  "updated_at": 1716886800.0,
  "totals": {
    "total_input_tokens": 1000,
    "total_output_tokens": 500,
    "total_tokens": 1500
  }
}
```

## 5. 数据持久化

### 5.1 写入时机

每次 `end_turn()` 完成后，会自动触发 `_persist_session()` 将数据写入磁盘：

**代码路径：** `monitor/collector.py:481-502`

```python
def _persist_session(self, session_id: str) -> None:
    turns = self._sessions.get(session_id, [])
    data = {
        "session_id": session_id,
        "created_at": turns[0].created_at,
        "updated_at": max(t.created_at for t in turns),
        "turns": [t.to_dict() for t in turns],
        "session_totals": {
            "total_input_tokens": sum(t.total_input_tokens for t in turns),
            "total_output_tokens": sum(t.total_output_tokens for t in turns),
            "total_tokens": sum(t.total_tokens for t in turns),
            "turn_count": len(turns),
        },
    }
    filepath = self._data_dir / f"session_{session_id}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
```

### 5.2 文件位置

```
data/
├── session_{session_id_1}.json
├── session_{session_id_2}.json
└── ...
```

### 5.3 加载时机

服务启动时，`TurnCollector.__init__()` 会调用 `_load_history()` 自动加载所有历史数据到内存：

**代码路径：** `monitor/collector.py:504-526`

```python
def _load_history(self) -> None:
    files = sorted(self._data_dir.glob("session_*.json"), key=lambda p: p.stat().st_mtime)
    for fp in files:
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 加载最近 50 轮 turn
        for turn_data in data.get("turns", [])[-_MAX_TURNS_PER_SESSION:]:
            turn = TurnRecord.from_dict(turn_data)
            self._sessions.setdefault(session_id, []).append(turn)
```

**注意：** 每个 session 最多保留最近 50 轮 turn（`_MAX_TURNS_PER_SESSION = 50`）。

## 6. 聚合查询实现

### 6.1 get_global_stats()

**代码路径：** `monitor/collector.py:477-496`

```python
def get_global_stats(self, since: float | None = None, detail: bool = True) -> dict:
    sessions = self.list_sessions(include_latest_turn=detail)
    if since is not None:
        sessions = [s for s in sessions if s.get("updated_at", 0.0) >= since]

    result = {
        "total_sessions": len(sessions),
        "total_turns": sum(s.get("turn_count", 0) for s in sessions),
        "total_input_tokens": sum(
            s.get("totals", {}).get("total_input_tokens", 0) for s in sessions
        ),
        "total_output_tokens": sum(
            s.get("totals", {}).get("total_output_tokens", 0) for s in sessions
        ),
        "total_tokens": sum(
            s.get("totals", {}).get("total_tokens", 0) for s in sessions
        ),
        "since": since,
    }
    if detail:
        result["sessions"] = sessions
    return result
```

### 6.2 聚合逻辑

| 聚合字段 | 计算方式 |
|---|---|
| `total_sessions` | 符合条件的 session 数量 |
| `total_turns` | 所有 session 的 `turn_count` 之和 |
| `total_input_tokens` | 所有 session `totals.total_input_tokens` 之和 |
| `total_output_tokens` | 所有 session `totals.total_output_tokens` 之和 |
| `total_tokens` | 所有 session `totals.total_tokens` 之和 |

## 7. 接口说明

### 7.1 GET /api/stats

返回完整的聚合统计，包含每个 session 的明细。

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `since` | float | 否 | Unix 时间戳（秒），只返回 `updated_at >= since` 的 session |

**响应示例：**

```json
{
  "total_sessions": 13,
  "total_turns": 21,
  "total_input_tokens": 5883,
  "total_output_tokens": 7267,
  "total_tokens": 13150,
  "since": null,
  "sessions": [
    {
      "session_id": "20c31129-0103-489e-bc9e-07d15c1ef2b9",
      "turn_count": 1,
      "latest_turn": { /* ... */ },
      "created_at": 1779960097.771935,
      "updated_at": 1779960097.771935,
      "totals": {
        "total_input_tokens": 265,
        "total_output_tokens": 162,
        "total_tokens": 427
      }
    }
  ]
}
```

### 7.2 GET /api/stats/summary

返回轻量级聚合统计，**不包含** `sessions` 明细数组。适用于数据量较大时快速获取总体数据。

**请求参数：**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `since` | float | 否 | Unix 时间戳（秒），只返回 `updated_at >= since` 的 session |

**响应示例：**

```json
{
  "total_sessions": 13,
  "total_turns": 21,
  "total_input_tokens": 5883,
  "total_output_tokens": 7267,
  "total_tokens": 13150,
  "since": null
}
```

### 7.3 Postman 调用示例

```
GET http://localhost:8000/api/stats
GET http://localhost:8000/api/stats?since=1779960000
GET http://localhost:8000/api/stats/summary
GET http://localhost:8000/api/stats/summary?since=1779960000
```

## 8. 关键代码路径索引

| 功能 | 文件 | 行号 |
|---|---|---|
| Token 计数（双策略） | `monitor/collector.py` | 21-45 |
| Input 消息分块 | `monitor/collector.py` | 56-146 |
| TurnRecord 数据模型 | `monitor/collector.py` | 153-246 |
| start_turn() — 记录 input | `monitor/collector.py` | 282-349 |
| end_turn() — 记录 output | `monitor/collector.py` | 351-425 |
| list_sessions() — Session 列表 | `monitor/collector.py` | 431-452 |
| get_session() — 单个 Session | `monitor/collector.py` | 454-469 |
| get_global_stats() — 全局聚合 | `monitor/collector.py` | 479-502 |
| 数据持久化（写入） | `monitor/collector.py` | 507-527 |
| 数据加载（启动时） | `monitor/collector.py` | 529-551 |
| Transform 回调中调用 start_turn | `main.py` | 200-208 |
| Result 回调中调用 end_turn | `main.py` | 256-261 |
| /api/stats 端点 | `main.py` | 32-38 |
| /api/stats/summary 端点 | `main.py` | 41-47 |
| 监控面板路由挂载 | `monitor/server.py` | 15-58 |

## 9. 扩展考虑

### 9.1 数据膨胀

当前每个 session 最多保留 50 轮 turn，超过的会自动丢弃。如果需要长期保留完整历史，可以考虑：
- 增加 `_MAX_TURNS_PER_SESSION` 限制
- 接入数据库（如 SQLite / PostgreSQL）替代 JSON 文件

### 9.2 用户维度聚合

当前系统只有 `session_id` 维度，没有 `user_id` 概念。如果需要按用户统计，需要：
- 在 Higo V2 请求中新增用户标识字段
- 在 `TurnRecord` 中增加 `user_id` 字段
- 修改聚合逻辑支持按 `user_id` 分组

### 9.3 时间粒度统计

当前仅支持 `since` 按时间戳过滤。如果需要按天/周/月统计，可以：
- 在 `_persist_session()` 中增加时间索引
- 新增按时间分组的聚合接口
