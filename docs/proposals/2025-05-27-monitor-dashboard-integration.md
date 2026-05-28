# Higo2OV 监控页面集成方案

## 背景

当前 higo2ov 作为 Higo V2 插件运行，处理对话的 `transform`（消息注入）和 `result`（结果回调）两个阶段，但缺乏对对话过程的可视化观测能力。

参考项目 `jiuwenclaw-openviking-observer` 已实现了一套基于 LLM Proxy 拦截的监控 UI，可展示对话历史、消息分块、Token 消耗等信息。higo2ov 不直接代理 LLM 调用，但可通过 Higo V2 协议回调获取足够的数据，以适配方式实现同等能力的监控页面。

## 目标

1. 在 higo2ov 运行时内嵌一个监控页面，访问 `http://localhost:8000/monitor` 即可查看
2. 展示用户与大模型的对话历史（按 Session 分组）
3. 展示每轮对话的消息内容（用户输入、系统提示、AI 回复、工具调用、注入的 Memory）
4. 展示 Token 消耗统计（输入分块饼图、输出统计、总量）
5. 数据持久化到磁盘，重启后自动恢复历史记录

## 非目标

- 不修改 Higo V2 协议
- 不引入额外进程或端口（复用 FastAPI 的 8000 端口）
- 不代理 LLM 调用（与 observer 的 proxy 模式不同）

## 架构设计

### 数据流

```
┌─────────────┐      transform       ┌──────────────┐
│             │ ────────────────────> │              │
│  Higo 客户端 │  (messages/session)   │  higo2ov     │
│             │ <──────────────────── │  main.py     │
│             │      修改后的消息      │              │
│             │                       │  ┌────────┐  │
│             │      result           │  │ Monitor│  │
│             │ ────────────────────> │  │Collector│  │
│             │  (sections/errors)    │  └────────┘  │
│             │                       │       │      │
│             │                       │       ▼      │
│             │                       │  ┌────────┐  │
│             │ <──────────────────── │  │ Server │  │
│   浏览器    │   GET /monitor        │  │(FastAPI)│  │
│             │   GET /monitor/api/*  │  └────────┘  │
└─────────────┘                       └──────────────┘
                                              │
                                              ▼
                                        ┌──────────┐
                                        │  data/   │
                                        │ session_*│
                                        │ .json    │
                                        └──────────┘
```

### 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| 数据收集器 | `monitor/collector.py` | 解析 transform/result 数据，Token 估算，内存管理，持久化 |
| 页面服务 | `monitor/server.py` | 注册 FastAPI 路由，提供 REST API 和静态 HTML |
| 监控页面 | `monitor/static/index.html` | 前端展示（会话列表、饼图、分块表格、消息内容） |
| 入口埋点 | `main.py` | 在 transform 和 result 处理流程中调用收集器 |

## 详细设计

### 1. 数据模型

#### TurnRecord（单轮对话记录）

```python
@dataclass
class TurnRecord:
    turn_id: str              # 唯一标识，格式 turn_{uuid[:12]}
    session_id: str
    round_id: str
    seq: int                  # 轮次序号
    created_at: float         # 时间戳

    # Input（由 transform 阶段记录，基于重组后的消息）
    system_prompt: str = ""
    system_tokens: int = 0
    user_input: str = ""      # 当前用户提问
    user_tokens: int = 0
    history: list = field(default_factory=list)   # 历史消息（不含当前输入）
    history_tokens: int = 0
    memory_injected: str = "" # 本插件注入的 memory（higo2ov 特色字段）
    memory_tokens: int = 0

    # Output（由 result 阶段记录）
    assistant_output: str = ""
    assistant_tokens: int = 0
    reasoning: str = ""       # 思考过程
    tool_calls: list = field(default_factory=list)

    # Errors
    errors: list = field(default_factory=list)

    # Totals
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0

    # UI 分块数据
    chunks: list = field(default_factory=list)
```

#### Session 文件结构（磁盘持久化）

```json
{
  "session_id": "abc123",
  "created_at": 1716792345.123,
  "updated_at": 1716795678.901,
  "turns": [
    {
      "turn_id": "turn_a1b2c3d4e5f6",
      "seq": 1,
      "created_at": 1716792345.123,
      "created_at_iso": "2024-05-27T10:45:45+00:00",
      "input": {
        "system_prompt": "You are a helpful assistant...",
        "system_tokens": 25,
        "user_input": "帮我写一个 Python 快速排序函数",
        "user_tokens": 14,
        "conversation_history": [
          {"role": "user", "content": "你好"},
          {"role": "assistant", "content": "你好！有什么可以帮你的？"}
        ],
        "history_tokens": 45,
        "memory_injected": "## 用户偏好\n- 喜欢用 Python\n- 偏好简洁代码",
        "memory_tokens": 28
      },
      "output": {
        "assistant_output": "好的，这是一个快速排序的实现...",
        "assistant_tokens": 156,
        "reasoning": "用户明确要快速排序...",
        "tool_calls": []
      },
      "errors": [],
      "totals": {
        "input_tokens": 112,
        "output_tokens": 156,
        "total_tokens": 268
      },
      "chunks": [
        {"name": "System Prompt", "category": "system", "tokens": 25, "content_preview": "You are..."},
        {"name": "Injected Memory", "category": "memory", "tokens": 28, "content_preview": "## 用户偏好..."},
        {"name": "Conversation History", "category": "history", "tokens": 45, "content_preview": "[User]..."},
        {"name": "User Input", "category": "user", "tokens": 14, "content_preview": "帮我写一个...", "is_current_query": true}
      ]
    }
  ],
  "session_totals": {
    "total_input_tokens": 112,
    "total_output_tokens": 156,
    "total_tokens": 268,
    "turn_count": 1
  }
}
```

### 2. 目录结构

```
higo2ov/
├── data/                              ← 运行时生成，加入 .gitignore
│   ├── session_abc123.json
│   ├── session_def456.json
│   └── ...
├── monitor/                           ← 新增模块
│   ├── __init__.py
│   ├── collector.py                   ← 数据收集 + Token 估算 + 持久化
│   ├── server.py                      ← FastAPI 路由注册
│   └── static/
│       └── index.html                 ← 监控页面（基于 observer 改编）
├── main.py                            ← 修改：埋点 + 挂载路由
├── models.py
├── requirements.txt                   ← 修改：添加 tiktoken
└── .gitignore                         ← 修改：忽略 data/
```

### 3. 数据收集器（collector.py）

#### 内存结构

```python
class TurnCollector:
    _instance: Optional["TurnCollector"] = None

    def __init__(self, data_dir: str | None = None):
        self._sessions: Dict[str, List[TurnRecord]] = {}   # session_id -> turns
        self._pending: Dict[str, TurnRecord] = {}           # round_id -> TurnRecord（进行中）
        self._data_dir: Path = Path(data_dir or "data")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._load_history()
```

#### 生命周期方法

**`start_turn(session_id, round_id, seq, messages, model_tokens, memory_text)`**

> **重要**：`start_turn` 在 `_build_messages()` **之后**调用，`messages` 参数是**重组后的消息列表**（已包含注入的 memory），这样 Token 计算反映的是实际发给 LLM 的完整输入。

调用时机（在 `_handle_transform` 中）：
1. 生成 `memory_text`
2. 调用 `_build_messages()` 重组消息（注入 memory）
3. **调用 `start_turn(messages=new_messages, memory_text=memory_text)`**
4. 返回 `TransformResponse`

`start_turn` 内部逻辑：
1. 解析 `messages` 列表，提取：
   - `system_prompt`：role="system" 的消息内容
   - `memory_injected`：内容与 `memory_text` 匹配的消息（或包含 memory 标记的 role="user" 消息）
   - `user_input`：最后一个 role="user" 的消息内容（当前提问，排除 memory）
   - `history`：其余 role 为 user/assistant/tool 的消息（排除 memory 和当前输入）

2. Token 分块估算：
   - 调用 `_chunk_input(messages, memory_text)` 生成分块数组
   - 每个分块包含：`name`（显示名称）、`category`（system/memory/history/user/tools）、`tokens`（估算值）、`content_preview`（前 200 字符预览）

3. 创建 `TurnRecord` 存入 `_pending[round_id]`

**`end_turn(round_id, sections, errors)`**

在 `result` 回调处理阶段调用：

1. 从 `_pending` 取出对应的 `TurnRecord`
2. 解析 `sections` 提取：
   - `assistant_output`：type="text" 的 content 拼接
   - `reasoning`：type="reasoning" 的 content
   - `tool_calls`：type="tool" 的 toolname/toolargs/toolrsp
3. 估算 `assistant_tokens`（对 assistant_output 调用 `_count_tokens`）
4. 计算 totals：
   - `total_input_tokens` = 各 input 分块 token 之和（已在 start_turn 计算）
   - `total_output_tokens` = assistant_tokens
   - `total_tokens` = input + output
5. 将 turn 移入 `_sessions[session_id]` 列表
6. 调用 `_persist_session(session_id)` 写入磁盘
7. 内存限制：若某 session 的 turns 超过 50 轮，仅保留最近 50 轮（磁盘保留全部）

#### Token 估算策略

复用 observer 的实现逻辑：

```python
try:
    import tiktoken
    _HAS_TIKTOKEN = True
except ImportError:
    _HAS_TIKTOKEN = False

def _count_tokens(text: str) -> int:
    if not text:
        return 0
    if _HAS_TIKTOKEN:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            pass
    # 回退：CJK ≈ 1 token/字，ASCII ≈ 0.25 token/char
    total = 0
    for ch in text:
        total += 1 if ord(ch) > 127 else 0.25
    return max(1, int(total))
```

#### 消息分块逻辑（`_chunk_input`）

```python
def _chunk_input(messages: list[dict], memory_text: str = "") -> tuple[list[dict], int]:
    """将重组后的消息列表分块并计算 token。
    
    分块顺序：
    1. system（system 消息）
    2. memory（与 memory_text 匹配的消息）
    3. history（历史 user/assistant/tool 消息）
    4. user（最后一个 user 消息，即当前提问）
    """
```

关键区分逻辑：
- `memory_text` 非空时，遍历消息找出 `content` 包含 `memory_text`（或 memory 标记）的 role="user" 消息 → 归类为 `memory`
- 最后一个 role="user" 且不是 memory 的消息 → 归类为 `user`
- 其余 role 为 user/assistant/tool 的消息 → 归类为 `history`
- role="system" → 归类为 `system`

#### 持久化策略

**写入**（`_persist_session(session_id)`）：

```python
def _persist_session(self, session_id: str) -> None:
    turns = self._sessions.get(session_id, [])
    data = {
        "session_id": session_id,
        "created_at": turns[0].created_at if turns else time.time(),
        "updated_at": time.time(),
        "turns": [t.to_dict() for t in turns],
        "session_totals": {
            "total_input_tokens": sum(t.total_input_tokens for t in turns),
            "total_output_tokens": sum(t.total_output_tokens for t in turns),
            "total_tokens": sum(t.total_tokens for t in turns),
            "turn_count": len(turns),
        }
    }
    filepath = self._data_dir / f"session_{session_id}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
```

- 写入时机：每次 `end_turn` 完成后立即写入
- 写入方式：重写整个 session 文件（单进程无并发问题）
- 文件名格式：`session_{session_id}.json`

**加载**（`_load_history()`）：

```python
def _load_history(self) -> None:
    for fp in sorted(self._data_dir.glob("session_*.json"),
                     key=lambda p: p.stat().st_mtime):
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        session_id = data["session_id"]
        for turn_data in data.get("turns", [])[-50:]:  # 每 session 最多加载 50 轮到内存
            turn = TurnRecord.from_dict(turn_data)
            self._sessions.setdefault(session_id, []).append(turn)
```

- 加载时机：`TurnCollector` 初始化时
- 加载策略：加载所有 session 文件，每 session 保留最近 50 轮到内存

#### 查询 API

```python
def list_sessions(self) -> List[dict]:
    """返回所有 session 概览（session_id、turn_count、latest_turn、totals）"""

def get_session(self, session_id: str) -> Optional[dict]:
    """返回指定 session 的完整数据（含所有 turns）"""

def get_latest_turn(self) -> Optional[dict]:
    """返回最新的一轮 turn（跨所有 session）"""
```

### 4. 页面服务（server.py）

```python
def mount_monitor(app: FastAPI, data_dir: str | None = None) -> None:
    from .collector import TurnCollector
    collector = TurnCollector.get_instance(data_dir=data_dir)

    router = APIRouter(prefix="/monitor")

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def monitor_page():
        # 返回 static/index.html

    @router.get("/api/sessions")
    async def api_sessions():
        return {"sessions": collector.list_sessions()}

    @router.get("/api/sessions/{session_id}")
    async def api_session(session_id: str):
        # 返回指定 session 完整数据

    @router.get("/api/sessions/{session_id}/turns")
    async def api_session_turns(session_id: str):
        # 返回指定 session 的所有 turns

    @router.get("/api/turns/latest")
    async def api_latest_turn():
        # 返回最新 turn

    app.include_router(router)
```

### 5. 监控页面（index.html）

基于 observer 的 `static/index.html` 改编，核心变更：

| 变更项 | 说明 |
|--------|------|
| API 路径 | `/api/turns` -> `/monitor/api/sessions`、`/monitor/api/sessions/{id}` |
| 数据结构 | 去掉 `model_calls` 嵌套，顶层展示 `turns` 数组 |
| 分块类别 | 增加 `memory` 类别（展示注入的 memory） |
| 页面标题 | "Claw Observer" -> "Higo2OV Monitor" |
| 新增区域 | 展示 `memory_injected` 内容（折叠面板） |

页面布局保持不变：
- 左侧：Session 列表（时间、最新摘要、总 token）
- 右侧：
  - 统计概览卡片
  - 轮次选择器
  - 输入 Token 分块饼图
  - 分块详情表格
  - LLM 输出内容
  - Memory 注入内容（新增）

自动刷新：每 3 秒轮询 `/monitor/api/sessions`

### 6. main.py 埋点

**Import 增加：**
```python
from monitor.server import mount_monitor
```

**App 初始化后：**
```python
mount_monitor(app)
```

**`_handle_transform` 修改（在 `_build_messages` 之后、return 之前）：**

```python
async def _handle_transform(request: TransformRequest) -> TransformResponse:
    # ... 原有逻辑 ...
    
    # 生成 memory
    memory_text = await memory_engine.generate_memory(sid, [...])
    
    # 重组消息（注入 memory）
    if memory_text and memory_text.strip():
        memory_message = Message(role="user", content=memory_text)
        new_messages = _build_messages(original_messages, memory_message)
    else:
        new_messages = list(original_messages)
    
    # 【埋点】在重组后、返回前记录，传入重组后的消息列表
    from monitor.collector import TurnCollector
    TurnCollector.get_instance().start_turn(
        session_id=sid,
        round_id=request.round.roundId if request.round else f"unknown_{time.time()}",
        seq=request.round.seq if request.round else 0,
        messages=[m.model_dump() for m in new_messages],  # ← 重组后的消息
        model_tokens=model_tokens,
        memory_text=memory_text,
    )
    
    return TransformResponse(...)
```

**`_handle_result` 末尾（return 之前）：**
```python
from monitor.collector import TurnCollector
TurnCollector.get_instance().end_turn(
    round_id=round_id,
    sections=[s.model_dump() for s in request.message.sections],
    errors=[e.model_dump() for e in request.errors],
)
```

### 7. 依赖变更

**`requirements.txt` 新增：**
```
tiktoken>=0.7.0
```

**`.gitignore` 新增：**
```
data/
```

## 关键时序

```
Higo 客户端          higo2ov main.py              Monitor Collector
    |                       |                              |
    |-- transform --------->|                              |
    |   (原始 messages)     |-- _handle_transform()        |
    |                       |   1. generate_memory()       |
    |                       |   2. _build_messages()       |
    |                       |      (重组，注入 memory)      |
    |                       |   3. start_turn(...) ------> |  传入重组后的消息
    |                       |      (基于 new_messages      |  解析、分块、算 token
    |                       |       计算 token)            |  创建 pending[round_id]
    |<-- 修改后 messages----|                              |
    |                       |                              |
    |-- LLM 调用 ---------->| (higo2ov 不感知，客户端直接调) |
    |                       |                              |
    |-- result ------------>|                              |
    |   (sections, etc)     |-- _handle_result()           |
    |                       |   -> capture_round_result()  |
    |                       |   -> end_turn(...) --------> |  提取 assistant 输出
    |                       |                              |  算 output token
    |                       |                              |  移入 _sessions[session_id]
    |                       |                              |  写入 data/session_*.json
    |<-- ack ---------------|                              |
    |                       |                              |
    | (浏览器访问)           |                              |
    |-- GET /monitor ------>|-- server.py 返回 index.html  |
    |-- GET /monitor/api/-->|-- collector.list_sessions()  |
    |                       |   返回 JSON                  |
    |<-- sessions data -----|                              |
```

**关键原则**：`start_turn` 在 `_build_messages` 之后调用，`messages` 参数是**重组后的消息列表**（已包含注入的 memory），Token 计算反映的是**实际发给 LLM 的完整输入**。

## 与 observer 的对比

| 维度 | observer（Proxy 模式） | higo2ov Monitor（Plugin 模式） |
|------|------------------------|--------------------------------|
| 数据获取方式 | 代理拦截 LLM API 调用 | 通过 Higo V2 transform/result 回调 |
| 部署方式 | 独立进程（Proxy + UI 双端口） | 内嵌到 FastAPI（复用 8000 端口） |
| Token 准确性 | 可读取 usage 字段（精确） | tiktoken 估算（近似） |
| 模型信息 | 可直接获取 model 名称 | 无法获取（协议未传递） |
| Memory 可见性 | 看不到插件注入的 memory | 可展示本插件注入的 memory |
| 适用场景 | 需要精确监控的调试环境 | 插件自身的运行观测 |

## 验证方式

1. 安装依赖：`pip install -r requirements.txt`
2. 启动服务：`python main.py`
3. 通过 Higo 客户端进行一次完整对话（触发 transform + result）
4. 浏览器访问 `http://localhost:8000/monitor`
5. 验证页面：
   - 左侧显示 Session 列表（时间、最新摘要、总 token）
   - 右侧显示统计概览、饼图、分块表格、LLM 输出
   - Memory 注入内容在折叠面板中可见
6. 多轮对话后检查：
   - `data/` 目录下生成 `session_*.json` 文件
   - 文件内容包含完整的 turns 数组和 session_totals
7. 重启服务后验证：
   - 历史 Session 自动加载到页面
   - 数据不丢失

## 局限性及后续优化

| 局限 | 说明 | 可能的优化 |
|------|------|-----------|
| Token 为估算值 | Higo V2 协议未返回 usage | 如协议后续扩展 usage 字段，可直接替换 |
| 无模型名称 | 信息在 Higo 客户端侧 | 可从 `meta` 或 `source` 字段推断 |
| Session 文件随长度增长 | 长会话文件可能达数 MB | 超过 100 轮时自动拆分为新文件 |
| 仅内存热缓存 50 轮 | 更早的数据在磁盘，页面加载需优化 | 前端分页加载或虚拟滚动 |

## 附录：Chunk 分类定义

| category | 名称 | 颜色 | 数据来源 |
|----------|------|------|----------|
| system | System Prompt | #dc2626（红） | role="system" 的消息 |
| memory | Injected Memory | #f59e0b（橙） | 本插件注入的 memory 内容 |
| history | Conversation History | #0ea5e9（蓝） | 历史 user/assistant/tool 消息 |
| user | User Input | #16a34a（绿） | 最后一个 role="user" 消息（当前提问） |
| tools | Tools Schema | #ea580c（深橙） | tool 定义（如有） |
